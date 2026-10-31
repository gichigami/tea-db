"""Schema + invariants for the H.1 canonical-matcher labeled pair set.

Fast and offline: no network, no Postgres. Validates the committed fixtures in
``tests/fixtures/matching/`` and the scorer-agnostic parts of the eval
harness (gate / exact / threshold / sweep logic) with a fake scorer.

Fixture regeneration (cassette parse, ~3 s) is deliberately NOT run here; CI
staleness is covered by ``python scripts/build_matcher_pairs.py --check``.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

import pytest

from tea_scrapers.eval.matcher_eval import (
    PRECISION_TARGET,
    SWEEP_THRESHOLDS,
    V1_TRIGRAM_THRESHOLD,
    best_thresholds,
    evaluate,
    h2_product_policy,
    producer_norm,
    sweep,
    v1_producer_policy,
    v1_producer_same,
    v1_product_policy,
)
from tea_scrapers.eval.pairs import (
    CATEGORIES,
    LabeledPair,
    PairSide,
    load_pairs,
)

MATCHING = Path(__file__).resolve().parents[1] / "fixtures" / "matching"
FILES = {"product": MATCHING / "product_pairs.jsonl", "producer": MATCHING / "producer_pairs.jsonl"}
HARD_NEGATIVE_PREFIX = "hard_negative_"


@pytest.fixture(scope="module")
def pairs() -> dict[str, list[LabeledPair]]:
    return {entity: load_pairs(path) for entity, path in FILES.items()}


# ----------------------------------------------------------------------
# Fixture schema + invariants
# ----------------------------------------------------------------------


@pytest.mark.parametrize("entity", ["product", "producer"])
def test_every_line_validates_and_is_canonical(entity: str) -> None:
    """Each line parses through the pydantic schema and is in generator form."""
    lines = FILES[entity].read_text(encoding="utf-8").splitlines()
    assert lines, f"{entity} fixture is empty"
    for line in lines:
        p = LabeledPair.model_validate_json(line)
        assert p.entity == entity
        canonical = json.dumps(p.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        assert line == canonical, f"{p.pair_id}: hand-edited? regenerate via the script"


def test_sizes_in_target_range(pairs: dict[str, list[LabeledPair]]) -> None:
    assert 300 <= len(pairs["product"]) <= 420
    assert 100 <= len(pairs["producer"]) <= 150


@pytest.mark.parametrize("entity", ["product", "producer"])
def test_pair_ids_sequential_and_unique(pairs: dict[str, list[LabeledPair]], entity: str) -> None:
    ids = [p.pair_id for p in pairs[entity]]
    prefix = "prod" if entity == "product" else "prdc"
    assert ids == [f"{prefix}-{i:04d}" for i in range(1, len(ids) + 1)]


@pytest.mark.parametrize("entity", ["product", "producer"])
def test_no_duplicate_or_self_pairs(pairs: dict[str, list[LabeledPair]], entity: str) -> None:
    def key(s: PairSide) -> tuple[object, ...]:
        return (s.name, s.producer, s.harvest_year, s.weight_grams, s.source_ref)

    seen: set[frozenset[tuple[object, ...]]] = set()
    for p in pairs[entity]:
        k = frozenset({key(p.left), key(p.right)})
        assert len(k) == 2, f"{p.pair_id} is a self-pair"
        assert k not in seen, f"{p.pair_id} duplicates an earlier pair"
        seen.add(k)


@pytest.mark.parametrize("entity", ["product", "producer"])
def test_every_category_is_covered(pairs: dict[str, list[LabeledPair]], entity: str) -> None:
    counts = Counter(p.category for p in pairs[entity])
    missing = CATEGORIES[entity] - set(counts)
    assert not missing, f"{entity} categories with no pairs: {sorted(missing)}"
    assert min(counts.values()) >= 5, counts


def test_product_set_overweights_hard_cases(pairs: dict[str, list[LabeledPair]]) -> None:
    ps = pairs["product"]
    hard_neg = sum(p.category.startswith(HARD_NEGATIVE_PREFIX) for p in ps)
    assert hard_neg / len(ps) >= 0.40, "hard negatives should dominate the negatives"
    pos = [p for p in ps if p.label == "match"]
    assert 0.30 <= len(pos) / len(ps) <= 0.60
    cross = [p for p in pos if "cross_source" in p.tags]
    assert len(cross) >= 40, "need genuine cross-vendor/cross-source positives"
    assert all(p.label == "non_match" for p in ps if p.category.startswith(HARD_NEGATIVE_PREFIX))
    assert all(p.label == "non_match" for p in ps if p.category in {"easy_negative", "cross_vendor_lookalike"})


@pytest.mark.parametrize("entity", ["product", "producer"])
def test_provenance_rules(pairs: dict[str, list[LabeledPair]], entity: str) -> None:
    for p in pairs[entity]:
        refs = (p.left.source_ref, p.right.source_ref)
        assert all(r.startswith(("cassette:", "golden:", "derived:", "config:")) or r == "synthetic"
                   for r in refs), p.pair_id
        if p.source == "cassette":
            assert not any(r.startswith(("derived:", "config:")) or r == "synthetic" for r in refs)
    synthetic = [p for p in pairs[entity] if p.source == "synthetic"]
    assert synthetic, "synthetic subset expected (clearly tagged, not silent)"
    assert len(synthetic) / len(pairs[entity]) <= 0.20, "synthetic must stay a minority"


@pytest.mark.parametrize("entity", ["product", "producer"])
def test_review_status_and_rationale(pairs: dict[str, list[LabeledPair]], entity: str) -> None:
    for p in pairs[entity]:
        assert p.review_status in {"agent_proposed", "owner_confirmed", "owner_corrected"}
        assert len(p.rationale) >= 10


def test_low_confidence_review_sheet_matches_fixtures(pairs: dict[str, list[LabeledPair]]) -> None:
    with open(MATCHING / "low_confidence_review.csv", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    low = {p.pair_id: p for ps in pairs.values() for p in ps if p.label_confidence == "low"}
    assert {r["pair_id"] for r in rows} == set(low)
    for r in rows:
        assert r["proposed_label"] == low[r["pair_id"]].label
        assert {"owner_label", "owner_note"} <= set(r)


def test_product_sides_carry_blocking_attributes(pairs: dict[str, list[LabeledPair]]) -> None:
    ps = pairs["product"]
    assert all(p.left.producer and p.right.producer for p in ps)
    # Enough attribute-bearing rows for the attribute guard (H.2) to be exercised.
    assert sum(p.left.harvest_year is not None for p in ps) >= 200
    assert sum(p.left.weight_grams is not None for p in ps) >= 200


def test_schema_rejects_bad_rows() -> None:
    side = PairSide(name="x", producer="p", source_ref="cassette:a#1")
    base = {"pair_id": "prod-0001", "entity": "product", "left": side, "right": side,
            "label": "match", "label_confidence": "high", "category": "suffix_variant",
            "source": "cassette", "rationale": "a reason long enough"}
    LabeledPair(**base)
    with pytest.raises(ValueError):
        LabeledPair(**{**base, "category": "producer_alias"})  # wrong entity vocabulary
    with pytest.raises(ValueError):
        LabeledPair(**{**base, "pair_id": "prdc-0001"})  # prefix / entity mismatch
    with pytest.raises(ValueError):
        LabeledPair(**{**base, "source": "synthetic"})  # no synthetic side
    with pytest.raises(ValueError):
        LabeledPair(**{**base, "left": PairSide(name="x", source_ref="cassette:a#1")})  # no producer


# ----------------------------------------------------------------------
# Harness logic (fake scorer, no DB)
# ----------------------------------------------------------------------


class FakeScorer:
    name = "fake"

    def __init__(self, table: dict[tuple[str, str], float]) -> None:
        self.table = table

    def score(self, pairs):  # type: ignore[no-untyped-def]
        return [self.table.get((a, b), self.table.get((b, a), 0.0)) for a, b in pairs]


def _pp(i: int, ln: str, rn: str, label: str, *, ly=2020, ry=2020, lw=200, rw=200,
        lp="P", rp="P", cat="easy_negative") -> LabeledPair:
    return LabeledPair(
        pair_id=f"prod-{i:04d}", entity="product", label=label, label_confidence="high",
        category=cat if label == "non_match" else "suffix_variant", source="cassette",
        rationale="fixture for harness unit test",
        left=PairSide(name=ln, producer=lp, harvest_year=ly, weight_grams=lw, source_ref="cassette:t"),
        right=PairSide(name=rn, producer=rp, harvest_year=ry, weight_grams=rw, source_ref="cassette:t"),
    )


def test_v1_threshold_mirrors_live_matcher() -> None:
    """Drift guard: the frozen baseline must agree with canonical.py's constant."""
    from tea_scrapers.normalize import canonical

    assert V1_TRIGRAM_THRESHOLD == canonical.TRIGRAM_THRESHOLD
    for raw in ("  Da Yi  (Menghai)", "WHITE2TEA", "Lao Mané"):
        assert producer_norm(raw) == canonical._producer_norm(raw)


def test_product_policy_gate_exact_and_threshold() -> None:
    pairs = [
        _pp(1, "A Cake", "a  cake", "match"),                      # exact after normalize
        _pp(2, "Alpha", "Alpha!", "match"),                        # score 0.9
        _pp(3, "Beta", "Beta 2", "non_match"),                     # score 0.88 -> FP at 0.85
        _pp(4, "Gamma", "Gamma", "match", ry=2021),                # year veto
        _pp(5, "Delta", "Delta", "match", rw=None),                # weight veto (one-sided)
        _pp(6, "Eps", "Eps", "match", rp="Q"),                     # producer veto
        _pp(7, "Zeta", "Zeta", "match", ly=None, ry=None),         # None == None passes
    ]
    scorer = FakeScorer({("Alpha", "Alpha!"): 0.9, ("Beta", "Beta 2"): 0.88, ("P", "Q"): 0.1})
    policy = v1_product_policy(v1_producer_same(scorer))
    scores = scorer.score([(p.left.name, p.right.name) for p in pairs])
    rep = evaluate(pairs, scores, policy, 0.85, entity="product", scorer_name="fake")
    o = rep.overall
    assert (o.tp, o.fp, o.fn, o.tn) == (3, 1, 3, 0)
    assert rep.veto_reasons == {"harvest_year": 1, "weight_grams": 1, "producer": 1}
    assert rep.vetoed_positives == 3
    assert rep.recall_ceiling == pytest.approx(3 / 6)
    assert [x.exact for x in rep.outcomes][:1] == [True]
    rep_hi = evaluate(pairs, scores, policy, 0.89, entity="product", scorer_name="fake")
    assert (rep_hi.overall.tp, rep_hi.overall.fp) == (3, 0)


def test_producer_policy_and_sweep() -> None:
    def ppair(i: int, a: str, b: str, label: str) -> LabeledPair:
        return LabeledPair(
            pair_id=f"prdc-{i:04d}", entity="producer", label=label, label_confidence="high",
            category="producer_alias" if label == "match" else "easy_negative",
            source="cassette", rationale="fixture for harness unit test",
            left=PairSide(name=a, source_ref="cassette:t"), right=PairSide(name=b, source_ref="cassette:t"),
        )

    pairs = [ppair(1, "White2Tea", "white2tea", "match"), ppair(2, "Xiaguan", "Xiaguan Tea Factory", "match"),
             ppair(3, "Wang Xu", "Wang Can", "non_match")]
    scores = [0.0, 0.6, 0.55]
    rows = sweep(pairs, scores, v1_producer_policy(), entity="producer", scorer_name="fake")
    assert [r["threshold"] for r in rows] == list(SWEEP_THRESHOLDS)
    by_t = {r["threshold"]: r for r in rows}
    assert by_t[0.5]["tp"] == 2 and by_t[0.5]["fp"] == 1
    assert by_t[0.6]["tp"] == 2 and by_t[0.6]["fp"] == 0
    assert by_t[0.65]["tp"] == 1  # exact (normalized-equality) row survives any threshold
    best = best_thresholds(rows)
    assert best["best_f1"]["threshold"] == 0.6
    assert best[f"lowest_threshold_with_precision_ge_{PRECISION_TARGET}"]["threshold"] == 0.6


def test_h2_policy_strips_suffix_and_applies_guard() -> None:
    """The live-H.2 policy: suffix-stripped exact key + recipe/style guard."""
    pairs = [
        _pp(1, "2017 Stargazer Sheng :: Seattle Inventory", "2017 Stargazer Sheng", "match"),
        _pp(2, "2023 Menghai 7572 Ripe Cake", "2023 Menghai 7262 Ripe Cake", "non_match"),
        _pp(3, "2023 Lao Man'e Raw Cake", "2023 Lao Man'e Ripe Cake", "non_match"),
    ]
    scorer = FakeScorer({})
    policy = h2_product_policy(v1_producer_same(scorer))
    assert policy.prepare("X :: Seattle Inventory") == "X"
    rep = evaluate(pairs, [0.0, 0.95, 0.95], policy, 0.85, entity="product", scorer_name="fake")
    assert [o.pred for o in rep.outcomes] == [True, False, False]
    assert rep.outcomes[0].exact
    assert rep.outcomes[1].veto == "guard:recipe_code:mismatch"
    assert rep.outcomes[2].veto == "guard:style:mismatch"
