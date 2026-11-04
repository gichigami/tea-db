"""Toggleable "cheap fix" variants of the live matcher on the H.1 harness
(ROADMAP H.2b; spec §12 "Hybrid canonical matcher" sub-qs #7 and #9).

Each variant switches on a subset of three independent changes so the owner
can see what each buys on its own:

1. **Transliteration** (``pinyin`` / ``skeleton`` / ``collapsed_trgm``) —
   :mod:`tea_scrapers.normalize.translit`. ``pinyin`` scores trigram on the
   pinyin key (CJK → toneless spaced pinyin; Latin unchanged).
   ``skeleton`` adds an exact step on the space/punctuation-insensitive
   skeleton (``Lao Ban Zhang`` ≡ ``Laobanzhang`` ≡ ``老班章``); product
   skeleton hits still go through the attribute guard. ``collapsed_trgm``
   additionally scores ``max(sim(spaced), sim(skeleton))`` — measured, not
   shipped (see the spec entry).
2. **Curated producer aliases** (``aliases``) —
   :class:`tea_scrapers.normalize.producer_aliases.AliasTable`, modelling
   what the matcher does after ``seed_producer_aliases``: two names that
   resolve to the same curated canonical match; two that resolve to
   *different* canonicals are vetoed; a name that resolves is compared to its
   canonical (the seeded row's ``canonical_name``) otherwise.
3. **One-sided attribute policy** (``year`` / ``weight`` / ``strict``) —
   spec §12 sub-q #7. ``year``: ``veto`` (V1 ``IS NOT DISTINCT FROM`` on the
   mapper year), ``fallback`` (block on the *effective* year — mapper year,
   else the name-parsed vintage the H.2 guard already uses — one-sided still
   vetoes), ``pass`` (effective year; one-sided passes to scoring).
   ``weight``: ``veto`` | ``pass`` | ``pass_review_source`` (one-sided passes
   only when the weightless side is a review source with no SKU concept —
   Steepster). ``strict``: a pair that got through *only* because of a
   one-sided pass needs ``score ≥ strict`` (or an exact hit) to match.

Same pairwise caveats as :mod:`matcher_eval` (no AMBIGUOUS gap, first
encounter only). One more that matters for #7: a Shopify listing sold at
N weights is N ``product`` rows with the same name, so in corpus a
weightless Steepster entry that passes the weight gate meets N equal-score
candidates and the AMBIGUOUS step over-creates. Pairwise recall for
relaxed-weight variants is therefore an upper bound that the live matcher
would not reach for multi-weight listings.

CLI::

    python -m tea_scrapers.eval.matcher_variants [--aliases PATH] [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from tea_scrapers.eval.matcher_eval import (
    DEFAULT_FIXTURES_DIR,
    PRECISION_TARGET,
    V1_TRIGRAM_THRESHOLD,
    CachedScorer,
    Counts,
    PairScorer,
    TrigramScorer,
    producer_norm,
)
from tea_scrapers.eval.pairs import LabeledPair, load_pairs
from tea_scrapers.normalize.attributes import (
    ONE_SIDED_POLICY,
    GuardAttributes,
    OneSidedPolicy,
    check_attribute_guard,
    matching_key,
    parse_name_attributes,
    strip_inventory_suffix,
)
from tea_scrapers.normalize.producer_aliases import AliasTable, load_producer_aliases
from tea_scrapers.normalize.translit import pinyin_key, usable_skeleton

FINE_THRESHOLDS: tuple[float, ...] = tuple(round(0.01 * i, 2) for i in range(101))
REVIEW_SOURCES = frozenset({"steepster"})
# ``strict`` above any similarity: a one-sided pass may only match on an
# exact / skeleton-exact name (no score-based auto-match).
EXACT_ONLY = 1.01


@dataclass(frozen=True)
class Variant:
    name: str
    pinyin: bool = False
    skeleton: bool = False
    collapsed_trgm: bool = False
    aliases: AliasTable | None = None
    year: str = "veto"  # veto | fallback | pass
    weight: str = "veto"  # veto | pass | pass_review_source
    strict: float | None = None


@dataclass
class Decision:
    veto: str | None
    exact: bool
    relaxed: bool  # survived the gate only via a one-sided pass
    text_a: str
    text_b: str


# ----------------------------------------------------------------------
# Producer
# ----------------------------------------------------------------------


def _producer_text(name: str, v: Variant) -> str:
    return pinyin_key(name) if v.pinyin else name.strip()


def _producer_exact(a: str, b: str, v: Variant) -> bool:
    if producer_norm(a) == producer_norm(b):
        return True
    if v.pinyin and pinyin_key(a) == pinyin_key(b):
        return True
    if v.skeleton:
        sa, sb = usable_skeleton(a), usable_skeleton(b)
        if sa is not None and sa == sb:
            return True
    return False


def decide_producer(a: str, b: str, v: Variant) -> Decision:
    if v.aliases is not None:
        ra = v.aliases.resolve(a, use_skeleton=v.skeleton)
        rb = v.aliases.resolve(b, use_skeleton=v.skeleton)
        if ra is not None and rb is not None:
            if ra != rb:
                return Decision("alias_conflict", False, False, a, b)
            return Decision(None, True, False, a, b)
        a = ra or a
        b = rb or b
    return Decision(
        None, _producer_exact(a, b, v), False, _producer_text(a, v), _producer_text(b, v)
    )


# ----------------------------------------------------------------------
# Product
# ----------------------------------------------------------------------


def _effective_year(name: str, mapper_year: int | None) -> int | None:
    return mapper_year if mapper_year is not None else parse_name_attributes(name).vintage_year


def _product_text(name: str, v: Variant) -> str:
    base = strip_inventory_suffix(name)
    return pinyin_key(base) if v.pinyin else base


def decide_product(
    p: LabeledPair, v: Variant, producer_same: Callable[[str, str], bool]
) -> Decision:
    L, R = p.left, p.right
    ta, tb = _product_text(L.name, v), _product_text(R.name, v)
    lp, rp = L.producer, R.producer
    if (lp is None) != (rp is None) or (lp and rp and not producer_same(lp, rp)):
        return Decision("producer", False, False, ta, tb)

    relaxed = False
    guard_policy: dict[str, OneSidedPolicy] = {}

    # Year.
    if v.year == "veto":
        if L.harvest_year != R.harvest_year:
            return Decision("harvest_year", False, False, ta, tb)
    else:
        ya, yb = _effective_year(L.name, L.harvest_year), _effective_year(R.name, R.harvest_year)
        if ya is not None and yb is not None and ya != yb:
            return Decision("harvest_year", False, False, ta, tb)
        if (ya is None) != (yb is None):
            if v.year != "pass":
                return Decision("harvest_year:one_sided", False, False, ta, tb)
            relaxed = True
            guard_policy["harvest_year"] = OneSidedPolicy.PASS

    # Weight.
    wa, wb = L.weight_grams, R.weight_grams
    if wa is not None and wb is not None and wa != wb:
        return Decision("weight_grams", False, False, ta, tb)
    if (wa is None) != (wb is None):
        weightless = L if wa is None else R
        ok = v.weight == "pass" or (
            v.weight == "pass_review_source" and weightless.vendor in REVIEW_SOURCES
        )
        if not ok:
            return Decision("weight_grams:one_sided", False, False, ta, tb)
        relaxed = True
        guard_policy["weight_grams"] = OneSidedPolicy.PASS

    plain_exact = matching_key(L.name) == matching_key(R.name)
    if plain_exact and not relaxed:
        # Live matcher: the SQL exact step does not run the guard.
        return Decision(None, True, False, ta, tb)

    verdict = check_attribute_guard(
        GuardAttributes.from_name(L.name, harvest_year=L.harvest_year, weight_grams=wa),
        GuardAttributes.from_name(R.name, harvest_year=R.harvest_year, weight_grams=wb),
        policy={**ONE_SIDED_POLICY, **guard_policy},
    )
    if verdict.vetoed:
        return Decision("guard:" + verdict.conflicts[0].reason, False, relaxed, ta, tb)

    exact = plain_exact
    if not exact and v.skeleton:
        sa = usable_skeleton(strip_inventory_suffix(L.name))
        exact = sa is not None and sa == usable_skeleton(strip_inventory_suffix(R.name))
    return Decision(None, exact, relaxed, ta, tb)


# ----------------------------------------------------------------------
# Scoring + metrics
# ----------------------------------------------------------------------


def _scores(decisions: Sequence[Decision], scorer: PairScorer, v: Variant) -> list[float]:
    spaced = scorer.score([(d.text_a, d.text_b) for d in decisions])
    if not v.collapsed_trgm:
        return spaced
    coll = scorer.score(
        [
            (usable_skeleton(d.text_a) or d.text_a, usable_skeleton(d.text_b) or d.text_b)
            for d in decisions
        ]
    )
    return [max(a, b) for a, b in zip(spaced, coll, strict=True)]


def _predict(d: Decision, s: float, t: float, v: Variant) -> bool:
    if d.veto is not None:
        return False
    if d.exact:
        return True
    floor = max(t, v.strict) if (d.relaxed and v.strict is not None) else t
    return s >= floor


@dataclass
class VariantResult:
    entity: str
    variant: str
    at_085: Counts
    by_category: dict[str, Counts]
    recall_at_target: float | None
    threshold_at_target: float | None
    vetoed_positives: int

    def as_dict(self) -> dict[str, object]:
        return {
            "entity": self.entity,
            "variant": self.variant,
            "at_0.85": self.at_085.as_dict(),
            "by_category": {k: c.as_dict() for k, c in sorted(self.by_category.items())},
            f"recall_at_p_ge_{PRECISION_TARGET}": self.recall_at_target,
            "threshold_at_target": self.threshold_at_target,
            "vetoed_positives": self.vetoed_positives,
        }


def _measure(
    entity: str,
    v: Variant,
    pairs: Sequence[LabeledPair],
    decisions: Sequence[Decision],
    scores: Sequence[float],
) -> VariantResult:
    def counts_at(t: float) -> tuple[Counts, dict[str, Counts]]:
        c, by = Counts(), {}
        for p, d, s in zip(pairs, decisions, scores, strict=True):
            gold, pred = p.label == "match", _predict(d, s, t, v)
            c.add(gold, pred)
            by.setdefault(p.category, Counts()).add(gold, pred)
        return c, by

    at, by_cat = counts_at(V1_TRIGRAM_THRESHOLD)
    best_r: float | None = None
    best_t: float | None = None
    for t in FINE_THRESHOLDS:
        c, _ = counts_at(t)
        ok = c.precision is not None and c.precision >= PRECISION_TARGET
        if ok and c.recall is not None and (best_r is None or c.recall > best_r):
            best_r, best_t = c.recall, t
    vetoed = sum(1 for p, d in zip(pairs, decisions, strict=True)
                 if d.veto is not None and p.label == "match")
    return VariantResult(
        entity, v.name, at, by_cat,
        None if best_r is None else round(best_r, 4), best_t, vetoed,
    )


def evaluate_variant(
    v: Variant,
    scorer: PairScorer,
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR,
) -> dict[str, VariantResult]:
    producer_pairs = load_pairs(fixtures_dir / "producer_pairs.jsonl")
    product_pairs = load_pairs(fixtures_dir / "product_pairs.jsonl")

    def producer_same(a: str, b: str) -> bool:
        d = decide_producer(a, b, v)
        if d.veto is not None:
            return False
        if d.exact:
            return True
        (s,) = _scores([d], scorer, v)
        return s >= V1_TRIGRAM_THRESHOLD

    out: dict[str, VariantResult] = {}
    pd = [decide_product(p, v, producer_same) for p in product_pairs]
    out["product"] = _measure("product", v, product_pairs, pd, _scores(pd, scorer, v))
    rd = [decide_producer(p.left.name, p.right.name, v) for p in producer_pairs]
    out["producer"] = _measure("producer", v, producer_pairs, rd, _scores(rd, scorer, v))
    return out


def standard_variants(aliases: AliasTable | None) -> list[Variant]:
    base = Variant("baseline (live h2)")
    py = replace(base, name="1a pinyin (trigram on pinyin key)", pinyin=True)
    sk = replace(py, name="1b pinyin + skeleton exact", skeleton=True)
    co = replace(sk, name="1c 1b + collapsed trigram", collapsed_trgm=True)
    vs: list[Variant] = [base, py, sk, co]
    if aliases is not None:
        vs += [
            replace(base, name="2 aliases only", aliases=aliases),
            replace(sk, name="1b+2 skeleton + aliases", aliases=aliases),
        ]
    shipped = vs[-1] if aliases is not None else sk
    for name, kw in (
        ("3a year=fallback", {"year": "fallback"}),
        ("3b year=pass", {"year": "pass"}),
        ("3c weight=pass", {"weight": "pass"}),
        ("3d weight=pass_review_source", {"weight": "pass_review_source"}),
        ("3e year=fallback weight=pass_review_source",
         {"year": "fallback", "weight": "pass_review_source"}),
        ("3f 3e + strict 0.95", {"year": "fallback", "weight": "pass_review_source",
                                 "strict": 0.95}),
        ("3g year=pass weight=pass", {"year": "pass", "weight": "pass"}),
        ("3h 3g + strict 0.95", {"year": "pass", "weight": "pass", "strict": 0.95}),
        ("3i weight=pass exact-only", {"weight": "pass", "strict": EXACT_ONLY}),
        ("3j year+weight=pass exact-only", {"year": "pass", "weight": "pass",
                                           "strict": EXACT_ONLY}),
    ):
        vs.append(replace(shipped, name=f"{name} (on {shipped.name.split()[0]})", **kw))
    return vs


def render(results: Sequence[dict[str, VariantResult]]) -> str:
    header = (
        f"{'variant':52s} | {'product P@.85':>13s} {'R@.85':>6s} {'R@P≥.98':>8s} {'(t)':>5s}"
        f" | {'producer P@.85':>14s} {'R@.85':>6s} {'R@P≥.98':>8s} {'(t)':>5s}"
    )
    lines = [header]

    def f(x: float | None) -> str:
        return "  -  " if x is None else f"{x:.3f}"

    for r in results:
        pr, pd = r["product"], r["producer"]
        lines.append(
            f"{pr.variant:52s} | {f(pr.at_085.precision):>13s} {f(pr.at_085.recall):>6s} "
            f"{f(pr.recall_at_target):>8s} {f(pr.threshold_at_target):>5s} | "
            f"{f(pd.at_085.precision):>14s} {f(pd.at_085.recall):>6s} "
            f"{f(pd.recall_at_target):>8s} {f(pd.threshold_at_target):>5s}"
        )
    lines.append("")
    lines.append("per-category TP/pos (FP) at t=0.85:")
    cats: dict[str, list[str]] = {}
    for r in results:
        for ent in ("product", "producer"):
            for cat in r[ent].by_category:
                cats.setdefault(ent, [])
                if cat not in cats[ent]:
                    cats[ent].append(cat)
    for ent in ("product", "producer"):
        lines.append(f"  [{ent}]")
        for cat in sorted(cats.get(ent, [])):
            cells = []
            for r in results:
                c = r[ent].by_category.get(cat)
                cells.append("-" if c is None else f"{c.tp}/{c.positives}({c.fp})")
            lines.append(f"  {cat:28s} " + " ".join(f"{x:>10s}" for x in cells))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--database-url",
        default=os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL"),
    )
    ap.add_argument("--fixtures-dir", type=Path, default=DEFAULT_FIXTURES_DIR)
    ap.add_argument("--aliases", type=Path, default=None,
                    help="curated producer_aliases.yaml (omit to skip alias variants)")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    if not args.database_url:
        print("error: --database-url or TEST_DATABASE_URL/DATABASE_URL required", file=sys.stderr)
        return 2
    aliases = load_producer_aliases(args.aliases) if args.aliases else None
    scorer = CachedScorer(TrigramScorer(args.database_url))
    results = [evaluate_variant(v, scorer, args.fixtures_dir) for v in standard_variants(aliases)]
    print(render(results))
    if args.json:
        args.json.write_text(json.dumps(
            [{k: r.as_dict() for k, r in res.items()} for res in results], indent=1,
            ensure_ascii=False,
        ))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
