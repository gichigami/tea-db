"""H.2b variant harness logic (no DB: a deterministic stand-in scorer)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tea_scrapers.eval.matcher_variants import (
    EXACT_ONLY,
    Variant,
    decide_producer,
    evaluate_variant,
    standard_variants,
)
from tea_scrapers.normalize.producer_aliases import load_producer_aliases

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "producer_aliases"
    / "sample_producer_aliases.yaml"
)


class EqScorer:
    """1.0 on case-insensitive equality, else 0.0 — isolates gate/exact logic."""

    name = "eq"

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        return [float(a.lower() == b.lower()) for a, b in pairs]


def test_alias_resolution_and_conflict_veto() -> None:
    v = Variant("a", aliases=load_producer_aliases(FIXTURE), skeleton=True, pinyin=True)
    assert decide_producer("大益", "Dayi", v).exact
    assert decide_producer("W2T", "white2tea", v).exact
    assert decide_producer("Dayi", "Xiaguan", v).veto == "alias_conflict"


def test_each_toggle_only_adds_recall_on_eq_scorer() -> None:
    base, py, sk = standard_variants(None)[:3]
    r = {v.name: evaluate_variant(v, EqScorer()) for v in (base, py, sk)}
    rec = [r[v.name]["product"].at_085.recall for v in (base, py, sk)]
    assert rec == sorted(rec)
    assert r[sk.name]["producer"].at_085.tp > r[base.name]["producer"].at_085.tp


def test_exact_only_relaxation_never_adds_score_based_matches() -> None:
    veto = evaluate_variant(Variant("veto"), EqScorer())["product"]
    res = evaluate_variant(Variant("w", weight="pass", strict=EXACT_ONLY), EqScorer())["product"]
    # The 12 ":: Seattle Inventory" twins whose variant has no parseable weight.
    gained = res.by_category["suffix_variant"].tp - veto.by_category["suffix_variant"].tp
    assert gained == 12
    assert res.at_085.fp == veto.at_085.fp
