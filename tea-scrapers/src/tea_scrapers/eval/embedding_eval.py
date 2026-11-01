"""Embedding / hybrid scorers for the H.1 pair harness — PRELIMINARY (ROADMAP H.3).

Plugs name-embedding scorers into :mod:`tea_scrapers.eval.matcher_eval`
(same :class:`~tea_scrapers.eval.matcher_eval.PairScorer` protocol, same
``evaluate`` / ``sweep`` / ``best_thresholds``) and compares them with the
trigram baseline. **Nothing here is a calibrated threshold**: H.5 calibrates
after the owner reviews the agent-proposed labels; this module only tells us
whether embeddings move the right categories.

Scorers:

- ``dense``   — BGE-M3 dense cosine of the embedding texts.
- ``sparse``  — BGE-M3 lexical weights, cosine-normalized to [0, 1]
  (FlagEmbedding's raw lexical dot is length-dependent, not thresholdable).
- ``hybrid``  — weighted mean of component scorers (default: dense ⊕ trigram,
  0.5 / 0.5). Weighted blend rather than RRF because RRF ranks don't map to a
  precision target (spec §12 hybrid sub-question #2); H.5 picks the formula.

Views (decision rule per pair, see ``matcher_eval``):

- product ``ungated`` — score ≥ t only (no blocking, no guard, no exact): the
  scorer's raw discriminative power, per category.
- product ``h2`` — the live H.2 ladder (V1 producer / year / weight blocking,
  suffix-exact, attribute guard) with the scorer swapped into the trigram
  slot. Needs Postgres (the gate's producer resolution is V1 trigram).
- producer ``v1`` — normalized-equality exact, then score ≥ t.

Texts: names are embedded via :mod:`tea_scrapers.embed.text` (suffix-stripped
matching key; producer norm). Offline use reads the committed fixture vectors
(``tests/fixtures/matching/name_embeddings_bge_m3.jsonl.gz``) and raises on
any text not in them — no model download.

CLI::

    python -m tea_scrapers.eval.embedding_eval [--vectors PATH] [--database-url URL]
        [--confidence all|high|low] [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from tea_scrapers.embed.base import (
    Embedder,
    dense_cosine,
    normalize_embed_text,
    sparse_cosine,
)
from tea_scrapers.embed.cache import embed_mapping
from tea_scrapers.embed.fake import DEFAULT_NAME_EMBEDDING_FIXTURE, fixture_embedder
from tea_scrapers.embed.text import producer_embedding_text, product_embedding_text
from tea_scrapers.eval.matcher_eval import (
    DEFAULT_FIXTURES_DIR,
    PRECISION_TARGET,
    CachedScorer,
    Counts,
    MatchPolicy,
    PairScorer,
    TrigramScorer,
    evaluate,
    h2_product_policy,
    score_pairs,
    sweep,
    v1_producer_policy,
    v1_producer_same,
)
from tea_scrapers.eval.pairs import LabeledPair, load_pairs

# Dense cosines of unrelated short names sit around 0.3-0.6 and matched ones
# bunch above 0.9, so sweep the full [0, 1) range at 0.01 (H.1 used 0.50-0.95).
FINE_THRESHOLDS: tuple[float, ...] = tuple(round(0.01 * i, 2) for i in range(100))
TARGET_KEY = f"max_recall_with_precision_ge_{PRECISION_TARGET}"
SECONDARY_PRECISION = 0.90
SECONDARY_KEY = f"max_recall_with_precision_ge_{SECONDARY_PRECISION}"

Kind = Literal["dense", "sparse"]


class EmbeddingScorer:
    """Dense or sparse cosine between the embedding texts of two names."""

    def __init__(
        self,
        embedder: Embedder,
        kind: Kind,
        text_fn: Callable[[str], str],
        name: str | None = None,
    ) -> None:
        self._embedder = embedder
        self._kind = kind
        self._text_fn = text_fn
        self.name = name or kind

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        keyed = [
            (normalize_embed_text(self._text_fn(a)), normalize_embed_text(self._text_fn(b)))
            for a, b in pairs
        ]
        texts = [t for ab in keyed for t in ab]
        emb = embed_mapping(self._embedder, texts, with_sparse=self._kind == "sparse")
        out: list[float] = []
        for a, b in keyed:
            ea, eb = emb[a], emb[b]
            if self._kind == "dense":
                out.append(dense_cosine(ea.dense, eb.dense))
            else:
                assert ea.sparse is not None and eb.sparse is not None
                out.append(sparse_cosine(ea.sparse, eb.sparse))
        return out


class HybridScorer:
    """Weighted mean of component scorers (all expected in [0, 1]-ish)."""

    def __init__(self, parts: Sequence[tuple[PairScorer, float]], name: str | None = None):
        if not parts or any(w <= 0 for _, w in parts):
            raise ValueError("hybrid needs ≥1 component with positive weight")
        self._parts = list(parts)
        total = sum(w for _, w in parts)
        self._weights = [w / total for _, w in parts]
        self.name = name or "hybrid(" + "+".join(
            f"{w:.2f}*{s.name}" for (s, _), w in zip(self._parts, self._weights, strict=True)
        ) + ")"

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        cols = [s.score(pairs) for s, _ in self._parts]
        return [
            sum(w * c[i] for w, c in zip(self._weights, cols, strict=True))
            for i in range(len(pairs))
        ]


def ungated_policy(prepare: Callable[[str], str] | None = None) -> MatchPolicy:
    from tea_scrapers.normalize.attributes import strip_inventory_suffix

    return MatchPolicy(
        name="ungated",
        gate=lambda p: None,
        exact=lambda p: False,
        prepare=prepare or strip_inventory_suffix,
    )


# ----------------------------------------------------------------------
# Pair-text collection (fixture builder + staleness test)
# ----------------------------------------------------------------------


def collect_pair_texts(fixtures_dir: Path = DEFAULT_FIXTURES_DIR) -> list[str]:
    """Every embedding text the harness can request, normalized + sorted.

    Product names → product key; producer-pair names and the producer hints
    on product pairs → producer key (H.5 may resolve producers by embedding).
    """
    texts: set[str] = set()
    for p in load_pairs(fixtures_dir / "product_pairs.jsonl"):
        for side in (p.left, p.right):
            texts.add(normalize_embed_text(product_embedding_text(side.name)))
            if side.producer:
                texts.add(normalize_embed_text(producer_embedding_text(side.producer)))
    for p in load_pairs(fixtures_dir / "producer_pairs.jsonl"):
        for side in (p.left, p.right):
            texts.add(normalize_embed_text(producer_embedding_text(side.name)))
    return sorted(texts)


# ----------------------------------------------------------------------
# Comparison runner
# ----------------------------------------------------------------------


def _best(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    def f(row: Mapping[str, object], k: str) -> float:
        v = row.get(k)
        return float(v) if isinstance(v, (int, float)) else -1.0

    def at(target: float) -> dict[str, object] | None:
        # Highest recall among thresholds meeting the target (precision is not
        # monotone in t on ~500 pairs, so "lowest t" can undershoot).
        meeting = [r for r in rows if f(r, "precision") >= target]
        if not meeting:
            return None
        return dict(max(meeting, key=lambda r: (f(r, "recall"), -f(r, "threshold"))))

    best = max(rows, key=lambda r: (f(r, "f1"), f(r, "threshold")))
    return {"best_f1": dict(best), TARGET_KEY: at(PRECISION_TARGET),
            SECONDARY_KEY: at(SECONDARY_PRECISION)}


def roc_auc(gold: Sequence[bool], scores: Sequence[float]) -> float | None:
    """Mann-Whitney ROC-AUC (ties count 1/2). Threshold-free separability."""
    pos = [s for g, s in zip(gold, scores, strict=True) if g]
    neg = [s for g, s in zip(gold, scores, strict=True) if not g]
    if not pos or not neg:
        return None
    ranked = sorted((s, i) for i, s in enumerate([*pos, *neg]))
    ranks = [0.0] * len(ranked)
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[ranked[k][1]] = (i + j) / 2 + 1
        i = j + 1
    r_pos = sum(ranks[: len(pos)])
    return (r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def _cat_summary(by_cat: Mapping[str, Counts]) -> dict[str, dict[str, object]]:
    return {
        k: {"n": c.n, "pos": c.positives, "tp": c.tp, "fp": c.fp,
            "recall": None if c.recall is None else round(c.recall, 4)}
        for k, c in sorted(by_cat.items())
    }


def compare_scorers(
    entity: str,
    pairs: Sequence[LabeledPair],
    policy: MatchPolicy,
    scorers: Sequence[PairScorer],
    thresholds: Sequence[float] = FINE_THRESHOLDS,
) -> dict[str, object]:
    out: dict[str, object] = {"entity": entity, "policy": policy.name, "n": len(pairs)}
    per: dict[str, object] = {}
    for scorer in scorers:
        scores = score_pairs(pairs, scorer, policy.prepare)
        rows = sweep(pairs, scores, policy, thresholds, entity=entity, scorer_name=scorer.name)
        best = _best(rows)
        # AUC over the pairs whose fate the score decides (not vetoed, not exact).
        decided = [(p.label == "match", sc) for p, sc in zip(pairs, scores, strict=True)
                   if policy.gate(p) is None and not policy.exact(p)]
        auc = roc_auc([g for g, _ in decided], [sc for _, sc in decided])
        cats: dict[str, object] = {}
        for label, row in best.items():
            if row is None:
                continue
            assert isinstance(row, dict)
            rep = evaluate(pairs, scores, policy, float(row["threshold"]),
                           entity=entity, scorer_name=scorer.name)
            cats[label] = _cat_summary(rep.by_category)
        per[scorer.name] = {
            "sweep": rows, "best": best, "by_category_at": cats,
            "auc_score_decided": None if auc is None else round(auc, 4),
            "n_score_decided": len(decided),
        }
    out["scorers"] = per
    return out


def run_comparison(
    embedder: Embedder,
    *,
    trigram: PairScorer | None,
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR,
    confidence: str = "all",
    thresholds: Sequence[float] = FINE_THRESHOLDS,
    hybrid_weight: float = 0.5,
) -> dict[str, object]:
    prod_pairs = load_pairs(fixtures_dir / "product_pairs.jsonl")
    prdc_pairs = load_pairs(fixtures_dir / "producer_pairs.jsonl")
    if confidence != "all":
        prod_pairs = [p for p in prod_pairs if p.label_confidence == confidence]
        prdc_pairs = [p for p in prdc_pairs if p.label_confidence == confidence]

    def scorers_for(text_fn: Callable[[str], str]) -> list[PairScorer]:
        dense = EmbeddingScorer(embedder, "dense", text_fn)
        sparse = EmbeddingScorer(embedder, "sparse", text_fn)
        out: list[PairScorer] = [dense, sparse]
        if trigram is not None:
            out = [trigram, *out]
            out.append(HybridScorer([(dense, hybrid_weight), (trigram, 1 - hybrid_weight)],
                                    name=f"hybrid_dense{hybrid_weight:.2f}_trigram"))
            out.append(HybridScorer([(dense, 1.0), (sparse, 1.0), (trigram, 1.0)],
                                    name="hybrid_dense_sparse_trigram"))
        return out

    product_scorers = scorers_for(product_embedding_text)
    producer_scorers = scorers_for(producer_embedding_text)
    views: list[dict[str, object]] = [
        compare_scorers("product", prod_pairs, ungated_policy(), product_scorers, thresholds),
    ]
    if trigram is not None:
        producer_same = v1_producer_same(CachedScorer(trigram))
        views.append(compare_scorers("product", prod_pairs, h2_product_policy(producer_same),
                                     product_scorers, thresholds))
    views.append(compare_scorers("producer", prdc_pairs, v1_producer_policy(),
                                 producer_scorers, thresholds))
    return {
        "embedder": f"{embedder.model_name}@{embedder.revision}",
        "confidence_filter": confidence,
        "status": "PRELIMINARY — agent-proposed labels; thresholds not calibrated (H.5)",
        "views": views,
    }


def _f(x: object) -> str:
    return "  -  " if x is None else f"{x:.3f}" if isinstance(x, float) else str(x)


FOCUS_CATEGORIES = (
    "cross_script", "producer_alias", "suffix_variant", "cross_vendor_same_tea",
    "hard_negative_recipe_code", "hard_negative_year", "hard_negative_name_variant",
    "hard_negative_sheng_shou", "hard_negative_producer",
)


def render_comparison(result: Mapping[str, object]) -> str:
    lines = [
        f"# {result['status']}",
        f"# embedder={result['embedder']} confidence={result['confidence_filter']}",
        "",
    ]
    views = result["views"]
    assert isinstance(views, list)
    for view in views:
        lines.append(f"== {view['entity']} / {view['policy']} (n={view['n']}) ==")
        lines.append(f"{'scorer':30s} {'AUC':>6s} | {'bestF1 t':>8s} {'P':>6s} {'R':>6s} "
                     f"{'F1':>6s} | {'P>=.98 t':>8s} {'R':>6s} | {'P>=.90 t':>8s} {'R':>6s}")
        per = view["scorers"]
        for name, blk in per.items():
            b = blk["best"]["best_f1"]
            cells = []
            for key in (TARGET_KEY, SECONDARY_KEY):
                tgt = blk["best"][key]
                cells.append(f"{'none':>8s} {'-':>6s}" if tgt is None
                             else f"{tgt['threshold']:>8.2f} {_f(tgt['recall']):>6s}")
            lines.append(
                f"{name[:30]:30s} {_f(blk['auc_score_decided']):>6s} | {b['threshold']:>8.2f} "
                f"{_f(b['precision']):>6s} {_f(b['recall']):>6s} {_f(b['f1']):>6s} | "
                + " | ".join(cells)
            )
        lines.append(f"  (AUC over the {next(iter(per.values()))['n_score_decided']} pairs the "
                     "score decides; best-F1 degenerates to t~0 when positives are ~45%)")
        names = list(per)
        cats = sorted({c for blk in per.values() for c in blk["by_category_at"]["best_f1"]})
        cats = [c for c in FOCUS_CATEGORIES if c in cats] + [
            c for c in cats if c not in FOCUS_CATEGORIES]
        for label, title in (("best_f1", "best-F1 t"), (TARGET_KEY, "the P>=0.98 t")):
            lines.append(f"per-category at {title} (positives: TP/pos; negatives: FP/n):")
            lines.append(f"  {'category':30s} " + " ".join(f"{n[:16]:>16s}" for n in names))
            for c in cats:
                cells = []
                for n in names:
                    d = per[n]["by_category_at"].get(label, {}).get(c)
                    cells.append("-" if d is None else (
                        f"{d['tp']}/{d['pos']}" if d["pos"] else f"FP {d['fp']}/{d['n']}"))
                lines.append(f"  {c:30s} " + " ".join(f"{x:>16s}" for x in cells))
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vectors", type=Path, default=DEFAULT_NAME_EMBEDDING_FIXTURE,
                    help="name-embedding store file (default: committed BGE-M3 fixture)")
    ap.add_argument("--database-url",
                    default=os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL"),
                    help="Postgres with pg_trgm; enables trigram, hybrid and the h2 view")
    ap.add_argument("--fixtures-dir", type=Path, default=DEFAULT_FIXTURES_DIR)
    ap.add_argument("--confidence", choices=("all", "high", "low"), default="all")
    ap.add_argument("--hybrid-weight", type=float, default=0.5,
                    help="dense weight in the dense⊕trigram blend (trigram gets 1-w)")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    trigram = CachedScorer(TrigramScorer(args.database_url)) if args.database_url else None
    if trigram is None:
        print("note: no database URL — trigram / hybrid / h2 view skipped", file=sys.stderr)
    result = run_comparison(
        fixture_embedder(args.vectors), trigram=trigram, fixtures_dir=args.fixtures_dir,
        confidence=args.confidence, hybrid_weight=args.hybrid_weight,
    )
    print(render_comparison(result))
    if args.json:
        args.json.write_text(json.dumps(result, indent=1, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
