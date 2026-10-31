"""Smoke test: the H.1 trigram baseline runs end-to-end against Postgres pg_trgm.

Read-only (``SELECT similarity(...)`` only — no tables touched). Deliberately
does NOT pin P/R numbers: those move whenever the owner relabels a pair or H.2
changes the matcher, and the baseline report lives in the fixture README.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from tea_scrapers.eval.matcher_eval import (
    SWEEP_THRESHOLDS,
    TrigramScorer,
    render_text,
    run_v1_baseline,
)

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/tea",
)


@pytest.fixture(scope="module")
def scorer() -> TrigramScorer:
    try:
        with create_engine(DB_URL, pool_pre_ping=True).connect():
            pass
    except Exception as exc:  # noqa: BLE001 - any connect failure means skip
        pytest.skip(f"Postgres not reachable at {DB_URL}: {exc}")
    return TrigramScorer(DB_URL)


def test_trigram_scorer_orders_and_bounds(scorer: TrigramScorer) -> None:
    pairs = [("Xiaguan Tea Factory", "Xiaguan Tea Factory"), ("abc", "xyz"),
             ("2017 Stargazer", "2017 Stargazer :: Seattle Inventory")]
    sims = scorer.score(pairs)
    assert sims[0] == pytest.approx(1.0)
    assert sims[1] == pytest.approx(0.0)
    assert 0.0 < sims[2] < 1.0
    assert scorer.score([]) == []


@pytest.mark.parametrize("policy", ["v1", "h2"])
def test_baseline_runs_end_to_end(scorer: TrigramScorer, policy: str) -> None:
    result = run_v1_baseline(scorer, product_policy=policy)
    for entity in ("product", "producer"):
        block = result[entity]
        rep = block["at_threshold"]
        o = rep["overall"]
        assert o["tp"] + o["fp"] + o["fn"] + o["tn"] == o["n"] > 0
        assert len(rep["outcomes"]) == o["n"]
        assert [r["threshold"] for r in block["sweep"]] == list(SWEEP_THRESHOLDS)
        assert sum(c["n"] for c in rep["by_category"].values()) == o["n"]
    assert "recall ceiling" in render_text(result)
