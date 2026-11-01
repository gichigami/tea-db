"""Offline harness test: BGE-M3 scorers over the H.1 pairs via committed vectors.

No DB, no model, no network — :func:`fixture_embedder` raises on any text not
in ``tests/fixtures/matching/name_embeddings_bge_m3.jsonl.gz``. Like the
trigram tests, this deliberately does NOT pin P/R numbers (labels are
agent-proposed and H.5 calibrates); it pins coverage, plumbing and a few
order-of-magnitude invariants.
"""

from __future__ import annotations

import statistics

import pytest

from tea_scrapers.embed import fixture_embedder, text_hash
from tea_scrapers.embed.cache import CachedEmbedder, FileEmbeddingStore
from tea_scrapers.embed.fake import DEFAULT_NAME_EMBEDDING_FIXTURE
from tea_scrapers.embed.text import producer_embedding_text, product_embedding_text
from tea_scrapers.eval.embedding_eval import (
    FINE_THRESHOLDS,
    SECONDARY_KEY,
    TARGET_KEY,
    EmbeddingScorer,
    HybridScorer,
    collect_pair_texts,
    render_comparison,
    roc_auc,
    run_comparison,
)
from tea_scrapers.eval.matcher_eval import DEFAULT_FIXTURES_DIR
from tea_scrapers.eval.pairs import load_pairs
from tea_scrapers.normalize.attributes import strip_inventory_suffix


@pytest.fixture(scope="module")
def embedder() -> CachedEmbedder:
    return fixture_embedder()


def test_fixture_covers_every_pair_text() -> None:
    """Fails when labels or ``matching_key`` change: regenerate the vectors."""
    store = FileEmbeddingStore(DEFAULT_NAME_EMBEDDING_FIXTURE, read_only=True)
    have = {e.text_hash for e in store.entries() if e.embedding.sparse is not None}
    missing = [t for t in collect_pair_texts() if text_hash(t) not in have]
    assert not missing, (
        f"{len(missing)} pair texts lack fixture vectors (e.g. {missing[:3]}); run "
        "`python scripts/build_name_embedding_fixtures.py` with the [embed] extra"
    )


def test_dense_and_sparse_scorers_bounds_and_identity(embedder: CachedEmbedder) -> None:
    pairs = [(p.left.name, p.right.name) for p in
             load_pairs(DEFAULT_FIXTURES_DIR / "product_pairs.jsonl")[:40]]
    for kind in ("dense", "sparse"):
        s = EmbeddingScorer(embedder, kind, product_embedding_text)  # type: ignore[arg-type]
        scores = s.score(pairs)
        assert len(scores) == len(pairs)
        assert all(-1.0 - 1e-6 <= x <= 1.0 + 1e-6 for x in scores)
        same = s.score([(a, a) for a, _ in pairs[:5]])
        assert same == pytest.approx([1.0] * 5, abs=1e-3)
        assert s.score([]) == []


def test_hybrid_is_weighted_mean(embedder: CachedEmbedder) -> None:
    class Const:
        def __init__(self, v: float, name: str) -> None:
            self.v, self.name = v, name

        def score(self, pairs):  # type: ignore[no-untyped-def]
            return [self.v] * len(pairs)

    h = HybridScorer([(Const(1.0, "a"), 3.0), (Const(0.0, "b"), 1.0)])
    assert h.score([("x", "y")]) == [pytest.approx(0.75)]
    assert "0.75*a" in h.name
    with pytest.raises(ValueError):
        HybridScorer([])


def test_roc_auc() -> None:
    assert roc_auc([True, False], [0.9, 0.1]) == 1.0
    assert roc_auc([True, False], [0.1, 0.9]) == 0.0
    assert roc_auc([True, False], [0.5, 0.5]) == 0.5
    assert roc_auc([True], [0.5]) is None


def test_dense_separates_easy_cases(embedder: CachedEmbedder) -> None:
    """Sanity, not calibration: true suffix twins ≫ easy negatives."""
    pairs = load_pairs(DEFAULT_FIXTURES_DIR / "product_pairs.jsonl")
    dense = EmbeddingScorer(embedder, "dense", product_embedding_text)

    def med(cat: str) -> float:
        sel = [(strip_inventory_suffix(p.left.name), strip_inventory_suffix(p.right.name))
               for p in pairs if p.category == cat]
        return statistics.median(dense.score(sel))

    assert med("suffix_variant") > 0.95
    assert med("easy_negative") < med("suffix_variant") - 0.2


def test_offline_comparison_runs_end_to_end(embedder: CachedEmbedder) -> None:
    result = run_comparison(embedder, trigram=None)
    views = result["views"]
    assert isinstance(views, list)
    assert [(v["entity"], v["policy"]) for v in views] == [
        ("product", "ungated"), ("producer", "v1_producer")]
    for v in views:
        assert set(v["scorers"]) == {"dense", "sparse"}
        for blk in v["scorers"].values():
            assert [r["threshold"] for r in blk["sweep"]] == list(FINE_THRESHOLDS)
            assert {"best_f1", TARGET_KEY, SECONDARY_KEY} <= set(blk["best"])
            assert 0.0 <= blk["auc_score_decided"] <= 1.0
    assert "PRELIMINARY" in render_comparison(result)


def test_producer_texts_present(embedder: CachedEmbedder) -> None:
    # Producer hints on product pairs are embedded too (H.5 may resolve by embedding).
    hints = {p.left.producer for p in load_pairs(DEFAULT_FIXTURES_DIR / "product_pairs.jsonl")}
    texts = [producer_embedding_text(h) for h in hints if h]
    assert len(embedder.embed(texts)) == len(texts)
