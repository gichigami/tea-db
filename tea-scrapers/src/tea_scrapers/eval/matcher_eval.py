"""Pairwise evaluation harness for the canonical matchers (ROADMAP H.1 / H.5).

Deterministic and offline: the only external dependency is a Postgres with
``pg_trgm`` (for the trigram scorer). No network, no LLM, no model download.

Two pieces are pluggable so H.5 can reuse this for the hybrid matcher:

- :class:`PairScorer` — turns ``(left_name, right_name)`` into a float. The V1
  baseline is :class:`TrigramScorer` (``pg_trgm.similarity``). A hybrid
  scorer (trigram ⊕ embedding cosine) implements the same protocol.
- :class:`MatchPolicy` — the non-score part of the decision: a *gate* that
  can veto a pair (V1: producer / year / weight blocking; hybrid: the
  attribute guard) and an *exact* shortcut that matches without a score.

Decision per pair, for a given threshold ``t``:
``gate vetoes → non_match``; else ``exact → match``; else ``score ≥ t → match``.

**V1 mirror.** :func:`v1_product_policy` / :func:`v1_producer_policy` mirror
``normalize/canonical.py`` as of commit ``ef1a0f6`` (H.0). They are
re-implemented here rather than imported so the *baseline* stays frozen while
H.2 edits the live matcher; ``tests/unit/test_matcher_pair_fixtures.py`` pins
the threshold against ``canonical.TRIGRAM_THRESHOLD`` to catch drift.

**Pairwise caveats (documented in the fixture README):**

- *AMBIGUOUS gap.* The product matcher over-creates when ≥2 trigram
  candidates sit within ``AMBIGUOUS_GAP`` below ``AMBIGUOUS_TOP_CEILING``. A
  pair has exactly one candidate, so the gap never fires here. In corpus
  context it only ever converts a predicted match into an over-create (never
  the reverse), so pairwise recall is an upper bound on corpus recall;
  precision can move either way (it drops TPs and FPs alike).
- *Producer resolution inside product pairs.* The product step blocks on
  ``producer_id``. Pairwise we resolve "same producer" by running the V1
  producer policy (normalized-equality ∪ trigram ≥ 0.85) on the two producer
  strings — so producer-matcher errors propagate into product results the
  same way they would in silver.
- *Producer alias learning.* ``producer.aliases`` grows over a run; pairwise we
  model only the first encounter (alias hit ≡ normalized equality).

CLI::

    python -m tea_scrapers.eval.matcher_eval [--database-url URL] [--json OUT]
        [--confidence all|high] [--fixtures-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from tea_scrapers.eval.pairs import LabeledPair, load_pairs
from tea_scrapers.normalize.shopify_mapper import normalize_name

# Mirror of canonical.TRIGRAM_THRESHOLD @ ef1a0f6 (drift-tested).
V1_TRIGRAM_THRESHOLD = 0.85
SWEEP_THRESHOLDS: tuple[float, ...] = tuple(round(0.50 + 0.05 * i, 2) for i in range(10))
PRECISION_TARGET = 0.98  # spec §12 hybrid entry: auto-match ≥ ~0.98 precision

DEFAULT_FIXTURES_DIR = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "matching"
)


# ----------------------------------------------------------------------
# Pluggable pieces
# ----------------------------------------------------------------------


class PairScorer(Protocol):
    """Scores name pairs. Higher = more similar. Must be deterministic."""

    name: str

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]: ...


@dataclass(frozen=True)
class MatchPolicy:
    """Non-score part of a matcher decision.

    ``gate(pair) -> veto reason | None``; ``exact(pair) -> bool``.
    """

    name: str
    gate: Callable[[LabeledPair], str | None]
    exact: Callable[[LabeledPair], bool]


class TrigramScorer:
    """``pg_trgm.similarity(left, right)`` — the V1 matcher's score.

    Batched through ``unnest(... ) WITH ORDINALITY`` so order is preserved.
    """

    name = "trigram"

    def __init__(self, database_url: str) -> None:
        from sqlalchemy import create_engine

        self._engine = create_engine(database_url)

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        from sqlalchemy import text

        if not pairs:
            return []
        lefts = [a for a, _ in pairs]
        rights = [b for _, b in pairs]
        sql = text(
            "SELECT similarity(t.a, t.b) AS sim FROM "
            "unnest(CAST(:a AS text[]), CAST(:b AS text[])) WITH ORDINALITY AS t(a, b, i) "
            "ORDER BY t.i"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"a": lefts, "b": rights}).all()
        return [float(r.sim) for r in rows]


class CachedScorer:
    """Memoizes another scorer (producer sims are reused across policies)."""

    def __init__(self, inner: PairScorer) -> None:
        self._inner = inner
        self.name = inner.name
        self._cache: dict[tuple[str, str], float] = {}

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        missing = sorted({p for p in pairs if p not in self._cache})
        if missing:
            for p, s in zip(missing, self._inner.score(missing), strict=True):
                self._cache[p] = s
        return [self._cache[p] for p in pairs]


# ----------------------------------------------------------------------
# V1 baseline policies (mirror canonical.py @ ef1a0f6)
# ----------------------------------------------------------------------


def producer_norm(name: str) -> str:
    """Mirror of ``canonical._producer_norm``: NFC + lower + collapse ws."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", name).lower()).strip()


def v1_producer_same(
    scorer: PairScorer, threshold: float = V1_TRIGRAM_THRESHOLD
) -> Callable[[str, str], bool]:
    """V1 producer ladder as a pairwise predicate: alias/exact → trigram ≥ t."""

    def same(a: str, b: str) -> bool:
        if producer_norm(a) == producer_norm(b):
            return True
        (sim,) = scorer.score([(a.strip(), b.strip())])
        return sim >= threshold

    return same


def v1_producer_policy() -> MatchPolicy:
    return MatchPolicy(
        name="v1_producer",
        gate=lambda p: None,
        exact=lambda p: producer_norm(p.left.name) == producer_norm(p.right.name),
    )


def v1_product_policy(producer_same: Callable[[str, str], bool]) -> MatchPolicy:
    """V1 product ladder: block on producer / year / weight, then exact name."""

    def gate(p: LabeledPair) -> str | None:
        lp, rp = p.left.producer, p.right.producer
        if (lp is None) != (rp is None) or (lp and rp and not producer_same(lp, rp)):
            return "producer"
        # ``IS NOT DISTINCT FROM`` semantics: None only equals None.
        if p.left.harvest_year != p.right.harvest_year:
            return "harvest_year"
        if p.left.weight_grams != p.right.weight_grams:
            return "weight_grams"
        return None

    def exact(p: LabeledPair) -> bool:
        return normalize_name(p.left.name) == normalize_name(p.right.name)

    return MatchPolicy(name="v1_product", gate=gate, exact=exact)


# ----------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------


@dataclass
class Counts:
    n: int = 0
    positives: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, gold: bool, pred: bool) -> None:
        self.n += 1
        self.positives += int(gold)
        if gold and pred:
            self.tp += 1
        elif pred:
            self.fp += 1
        elif gold:
            self.fn += 1
        else:
            self.tn += 1

    @property
    def precision(self) -> float | None:
        d = self.tp + self.fp
        return self.tp / d if d else None

    @property
    def recall(self) -> float | None:
        return self.tp / self.positives if self.positives else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None if (p is None or r is None) else 0.0
        return 2 * p * r / (p + r)

    @property
    def accuracy(self) -> float | None:
        return (self.tp + self.tn) / self.n if self.n else None

    def as_dict(self) -> dict[str, float | int | None]:
        d: dict[str, float | int | None] = asdict(self)
        d.update(
            precision=_r(self.precision),
            recall=_r(self.recall),
            f1=_r(self.f1),
            accuracy=_r(self.accuracy),
        )
        return d


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


@dataclass
class PairOutcome:
    pair_id: str
    category: str
    gold: bool
    pred: bool
    score: float
    veto: str | None
    exact: bool


@dataclass
class EvalReport:
    entity: str
    scorer: str
    policy: str
    threshold: float
    overall: Counts
    by_category: dict[str, Counts]
    by_confidence: dict[str, Counts]
    veto_reasons: dict[str, int]
    outcomes: list[PairOutcome] = field(default_factory=list)

    def errors(self) -> list[PairOutcome]:
        return [o for o in self.outcomes if o.gold != o.pred]

    def as_dict(self, include_outcomes: bool = False) -> dict[str, object]:
        d: dict[str, object] = {
            "entity": self.entity,
            "scorer": self.scorer,
            "policy": self.policy,
            "threshold": self.threshold,
            "overall": self.overall.as_dict(),
            "by_category": {k: v.as_dict() for k, v in sorted(self.by_category.items())},
            "by_confidence": {k: v.as_dict() for k, v in sorted(self.by_confidence.items())},
            "veto_reasons": dict(sorted(self.veto_reasons.items())),
        }
        if include_outcomes:
            d["outcomes"] = [asdict(o) for o in self.outcomes]
        return d


def score_pairs(pairs: Sequence[LabeledPair], scorer: PairScorer) -> list[float]:
    return scorer.score([(p.left.name, p.right.name) for p in pairs])


def evaluate(
    pairs: Sequence[LabeledPair],
    scores: Sequence[float],
    policy: MatchPolicy,
    threshold: float,
    *,
    entity: str,
    scorer_name: str,
) -> EvalReport:
    """Apply ``policy`` + ``threshold`` to pre-computed ``scores``."""
    if len(pairs) != len(scores):
        raise ValueError("pairs / scores length mismatch")
    overall = Counts()
    by_cat: dict[str, Counts] = {}
    by_conf: dict[str, Counts] = {}
    vetoes: dict[str, int] = {}
    outcomes: list[PairOutcome] = []
    for p, s in zip(pairs, scores, strict=True):
        veto = policy.gate(p)
        is_exact = veto is None and policy.exact(p)
        pred = veto is None and (is_exact or s >= threshold)
        gold = p.label == "match"
        overall.add(gold, pred)
        by_cat.setdefault(p.category, Counts()).add(gold, pred)
        by_conf.setdefault(p.label_confidence, Counts()).add(gold, pred)
        if veto is not None:
            vetoes[veto] = vetoes.get(veto, 0) + 1
        outcomes.append(PairOutcome(p.pair_id, p.category, gold, pred, round(s, 4), veto, is_exact))
    return EvalReport(
        entity=entity,
        scorer=scorer_name,
        policy=policy.name,
        threshold=threshold,
        overall=overall,
        by_category=by_cat,
        by_confidence=by_conf,
        veto_reasons=vetoes,
        outcomes=outcomes,
    )


def sweep(
    pairs: Sequence[LabeledPair],
    scores: Sequence[float],
    policy: MatchPolicy,
    thresholds: Sequence[float] = SWEEP_THRESHOLDS,
    *,
    entity: str,
    scorer_name: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for t in thresholds:
        rep = evaluate(pairs, scores, policy, t, entity=entity, scorer_name=scorer_name)
        rows.append({"threshold": t, **rep.overall.as_dict()})
    return rows


def best_thresholds(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    """Best-F1 threshold (ties → higher t) + lowest t meeting the precision target."""

    def f(row: dict[str, object], k: str) -> float:
        v = row.get(k)
        return float(v) if isinstance(v, (int, float)) else -1.0

    best = max(rows, key=lambda r: (f(r, "f1"), f(r, "threshold")))
    meeting = [r for r in rows if f(r, "precision") >= PRECISION_TARGET]
    at_target = min(meeting, key=lambda r: f(r, "threshold")) if meeting else None
    return {
        "best_f1": best,
        f"lowest_threshold_with_precision_ge_{PRECISION_TARGET}": at_target,
    }


# ----------------------------------------------------------------------
# Baseline runner
# ----------------------------------------------------------------------


def run_v1_baseline(
    scorer: PairScorer,
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR,
    *,
    confidence: str = "all",
    threshold: float = V1_TRIGRAM_THRESHOLD,
) -> dict[str, object]:
    cached = CachedScorer(scorer)
    out: dict[str, object] = {"scorer": scorer.name, "confidence_filter": confidence}
    producer_same = v1_producer_same(cached)
    for entity, fname, policy in (
        ("product", "product_pairs.jsonl", v1_product_policy(producer_same)),
        ("producer", "producer_pairs.jsonl", v1_producer_policy()),
    ):
        pairs = load_pairs(fixtures_dir / fname)
        if confidence != "all":
            pairs = [p for p in pairs if p.label_confidence == confidence]
        scores = score_pairs(pairs, cached)
        rep = evaluate(pairs, scores, policy, threshold, entity=entity, scorer_name=cached.name)
        rows = sweep(pairs, scores, policy, entity=entity, scorer_name=cached.name)
        out[entity] = {
            "at_threshold": rep.as_dict(include_outcomes=True),
            "sweep": rows,
            "best": best_thresholds(rows),
        }
    return out


def _fmt(x: object) -> str:
    return "  -  " if x is None else f"{x:.3f}" if isinstance(x, float) else str(x)


def render_text(result: dict[str, object]) -> str:
    lines: list[str] = []
    for entity in ("product", "producer"):
        block = result[entity]
        assert isinstance(block, dict)
        rep = block["at_threshold"]
        o = rep["overall"]
        lines.append(
            f"== {entity} | scorer={result['scorer']} policy={rep['policy']} "
            f"t={rep['threshold']} confidence={result['confidence_filter']} =="
        )
        lines.append(
            f"overall n={o['n']} pos={o['positives']} TP={o['tp']} FP={o['fp']} "
            f"FN={o['fn']} TN={o['tn']}  P={_fmt(o['precision'])} R={_fmt(o['recall'])} "
            f"F1={_fmt(o['f1'])}"
        )
        lines.append(f"vetoes: {rep['veto_reasons']}")
        lines.append(f"{'category':34s} {'n':>4s} {'pos':>4s} {'TP':>3s} {'FP':>3s} "
                     f"{'FN':>3s} {'TN':>3s} {'P':>6s} {'R':>6s} {'F1':>6s} {'acc':>6s}")
        for cat, c in rep["by_category"].items():
            lines.append(
                f"{cat:34s} {c['n']:>4d} {c['positives']:>4d} {c['tp']:>3d} {c['fp']:>3d} "
                f"{c['fn']:>3d} {c['tn']:>3d} {_fmt(c['precision']):>6s} "
                f"{_fmt(c['recall']):>6s} {_fmt(c['f1']):>6s} {_fmt(c['accuracy']):>6s}"
            )
        lines.append("sweep:  t      P      R     F1")
        for r in block["sweep"]:
            lines.append(
                f"      {r['threshold']:.2f}  {_fmt(r['precision'])}  {_fmt(r['recall'])}  "
                f"{_fmt(r['f1'])}"
            )
        best = block["best"]
        b = best["best_f1"]
        lines.append(f"best F1 threshold: {b['threshold']} (P={_fmt(b['precision'])} "
                     f"R={_fmt(b['recall'])} F1={_fmt(b['f1'])})")
        tgt = best[f"lowest_threshold_with_precision_ge_{PRECISION_TARGET}"]
        lines.append(
            f"lowest t with P>={PRECISION_TARGET}: "
            + ("none in sweep" if tgt is None else
               f"{tgt['threshold']} (R={_fmt(tgt['recall'])})")
        )
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--database-url",
        default=os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL"),
        help="Postgres with pg_trgm (defaults to TEST_DATABASE_URL / DATABASE_URL)",
    )
    ap.add_argument("--fixtures-dir", type=Path, default=DEFAULT_FIXTURES_DIR)
    ap.add_argument("--confidence", choices=("all", "high", "low"), default="all")
    ap.add_argument("--json", type=Path, help="write the full result (incl. per-pair outcomes)")
    args = ap.parse_args(argv)
    if not args.database_url:
        print("error: --database-url or TEST_DATABASE_URL/DATABASE_URL required", file=sys.stderr)
        return 2
    result = run_v1_baseline(
        TrigramScorer(args.database_url), args.fixtures_dir, confidence=args.confidence
    )
    print(render_text(result))
    if args.json:
        args.json.write_text(json.dumps(result, indent=1, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
