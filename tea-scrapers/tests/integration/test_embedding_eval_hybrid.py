"""Smoke test: trigram vs BGE-M3 (fixture vectors) vs hybrid on the H.1 pairs.

Needs Postgres with pg_trgm (read-only ``SELECT similarity()``); the embedding
side is the committed fixture — no model download. P/R numbers are not
pinned (PRELIMINARY; H.5 calibrates after owner label review).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from tea_scrapers.embed import fixture_embedder
from tea_scrapers.eval.embedding_eval import TARGET_KEY, render_comparison, run_comparison
from tea_scrapers.eval.matcher_eval import CachedScorer, TrigramScorer

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/tea",
)


@pytest.fixture(scope="module")
def trigram() -> CachedScorer:
    try:
        with create_engine(DB_URL, pool_pre_ping=True).connect():
            pass
    except Exception as exc:  # noqa: BLE001 - any connect failure means skip
        pytest.skip(f"Postgres not reachable at {DB_URL}: {exc}")
    return CachedScorer(TrigramScorer(DB_URL))


def test_hybrid_comparison_end_to_end(trigram: CachedScorer) -> None:
    result = run_comparison(fixture_embedder(), trigram=trigram)
    views = result["views"]
    assert isinstance(views, list)
    assert [(v["entity"], v["policy"]) for v in views] == [
        ("product", "ungated"), ("product", "h2_product"), ("producer", "v1_producer")]
    for v in views:
        names = set(v["scorers"])
        assert {"trigram", "dense", "sparse", "hybrid_dense0.50_trigram",
                "hybrid_dense_sparse_trigram"} == names
        for blk in v["scorers"].values():
            tgt = blk["best"][TARGET_KEY]
            assert tgt is None or tgt["precision"] >= 0.98
    # Same gate for every scorer under h2 → identical recall ceiling at t=0.
    h2 = views[1]["scorers"]
    ceilings = {n: blk["sweep"][0]["recall"] for n, blk in h2.items()}
    assert len(set(ceilings.values())) == 1, ceilings
    assert "hybrid_dense_sparse_trigram" in render_comparison(result)
