"""Unit tests for the name-embedding layer (ROADMAP H.3).

No model, no network, no torch: everything runs on :class:`FakeEmbedder`
and the committed fixture vectors.
"""

from __future__ import annotations

import gzip
import json
import math
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from tea_scrapers.embed import (
    CachedEmbedder,
    CacheEntry,
    Embedder,
    EmbedderUnavailable,
    Embedding,
    EmbeddingCacheMiss,
    FakeEmbedder,
    FileEmbeddingStore,
    InMemoryEmbeddingStore,
    ModelKey,
    dense_cosine,
    fixture_embedder,
    l2_normalize,
    model_key,
    normalize_embed_text,
    producer_embedding_text,
    product_embedding_text,
    sparse_cosine,
    sparse_dot,
    text_hash,
)
from tea_scrapers.embed.bge_m3 import BGE_M3_DIM, BGE_M3_MODEL, BGE_M3_REVISION, BgeM3Embedder
from tea_scrapers.embed.fake import DEFAULT_NAME_EMBEDDING_FIXTURE

SHA_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


class CountingEmbedder(FakeEmbedder):
    """FakeEmbedder that records how many texts reached the 'model'."""

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.embedded = 0

    def embed(self, texts, *, with_sparse=False):  # type: ignore[no-untyped-def]
        self.embedded += len(texts)
        return super().embed(texts, with_sparse=with_sparse)


# ----------------------------------------------------------------------
# Text normalization + hashing
# ----------------------------------------------------------------------


def test_normalize_embed_text_nfc_whitespace_no_casefold() -> None:
    decomposed = unicodedata.normalize("NFD", "Lao Man’e  Café\n")
    out = normalize_embed_text(decomposed)
    assert out == unicodedata.normalize("NFC", "Lao Man’e Café")
    assert normalize_embed_text("  Da\tHong   Pao ") == "Da Hong Pao"  # case preserved


def test_text_hash_is_sha256_of_normalized_text() -> None:
    assert text_hash("abc") == SHA_ABC
    assert text_hash("  abc\n") == SHA_ABC
    assert text_hash("ABC") != SHA_ABC
    assert len(text_hash("大红袍")) == 64


def test_embedding_texts_use_matching_key() -> None:
    # Spec §12 hybrid sub-q #8: suffix stripped, so both listings share a hash.
    a = product_embedding_text("2017 Stargazer 357g Cake :: Seattle Inventory")
    b = product_embedding_text("2017  Stargazer 357g cake")
    assert a == b == "2017 stargazer 357g cake"
    assert text_hash(a) == text_hash(b)
    assert producer_embedding_text("  Menghai  Tea Factory ") == "menghai tea factory"


# ----------------------------------------------------------------------
# ModelKey + math
# ----------------------------------------------------------------------


def test_model_key_requires_pinned_sha_and_positive_dim() -> None:
    ModelKey("m", "a" * 40, 4)
    with pytest.raises(ValueError, match="pinned"):
        ModelKey("m", "main", 4)
    with pytest.raises(ValueError):
        ModelKey("m", "a" * 40, 0)
    with pytest.raises(ValueError):
        ModelKey("", "a" * 40, 4)
    assert "@aaaaaaaaaaaa/4d" in str(ModelKey("m", "a" * 40, 4))


def test_vector_math() -> None:
    v = l2_normalize([3.0, 4.0])
    assert v == pytest.approx((0.6, 0.8))
    assert dense_cosine(v, v) == pytest.approx(1.0)
    assert dense_cosine([1.0, 0.0], [0.0, 2.0]) == pytest.approx(0.0)
    assert dense_cosine([1.0, 1.0], [2.0, 2.0]) == pytest.approx(1.0)  # re-normalizes
    with pytest.raises(ValueError):
        dense_cosine([1.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        l2_normalize([0.0, 0.0])
    a, b = {1: 0.5, 2: 0.5}, {2: 0.4, 3: 0.9}
    assert sparse_dot(a, b) == pytest.approx(0.2)
    assert sparse_dot(a, {}) == 0.0
    assert sparse_cosine(a, a) == pytest.approx(1.0)
    assert 0.0 < sparse_cosine(a, b) < 1.0
    assert sparse_cosine({}, b) == 0.0


# ----------------------------------------------------------------------
# FakeEmbedder satisfies the protocol
# ----------------------------------------------------------------------


def test_fake_embedder_protocol_and_determinism() -> None:
    fake = FakeEmbedder(dim=12)
    assert isinstance(fake, Embedder)
    assert model_key(fake) == ModelKey("fake/sha256", "0" * 40, 12)
    e1, e2, e3 = fake.embed(["Bing Dao", "bing  dao", "Bing Dao"], with_sparse=True)
    assert e1.dim == 12
    assert math.fsum(x * x for x in e1.dense) == pytest.approx(1.0)
    assert e1 == e3 and e1 != e2  # case-sensitive, whitespace-normalized
    assert e1.sparse and all(w > 0 for w in e1.sparse.values())
    assert FakeEmbedder(dim=12).embed(["Bing Dao"])[0].dense == e1.dense
    assert fake.embed(["x"])[0].sparse is None


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------


def test_cached_embedder_read_through_and_dedupe() -> None:
    inner = CountingEmbedder(dim=8)
    store = InMemoryEmbeddingStore()
    cached = CachedEmbedder(store, inner)
    assert isinstance(cached, Embedder)
    out = cached.embed(["a", "b", "a", " a "])
    assert inner.embedded == 2 and len(store) == 2
    assert out[0] == out[2] == out[3] and out[0] != out[1]
    again = cached.embed(["b", "a"])
    assert inner.embedded == 2  # all hits
    assert again == [out[1], out[0]]
    assert (cached.hits, cached.misses) == (2, 2)


def test_cache_is_keyed_by_model_revision_dim() -> None:
    store = InMemoryEmbeddingStore()
    a = CountingEmbedder(dim=8)
    b = CountingEmbedder(dim=8, revision="1" * 40)
    CachedEmbedder(store, a).embed(["same text"])
    CachedEmbedder(store, b).embed(["same text"])
    assert b.embedded == 1  # different revision → miss
    assert len(store) == 2


def test_sparse_request_misses_dense_only_entry() -> None:
    inner = CountingEmbedder(dim=8)
    cached = CachedEmbedder(InMemoryEmbeddingStore(), inner)
    cached.embed(["x"])
    (e,) = cached.embed(["x"], with_sparse=True)
    assert inner.embedded == 2 and e.sparse is not None
    cached.embed(["x"])  # sparse entry also serves dense requests
    assert inner.embedded == 2


def test_cache_only_embedder_raises_on_miss() -> None:
    key = ModelKey("fake/sha256", "0" * 40, 8)
    store = InMemoryEmbeddingStore()
    store.put_many(key, [CacheEntry(text_hash("known"), "known", FakeEmbedder(dim=8).embed(["known"])[0])])
    cached = CachedEmbedder(store, None, key=key)
    assert cached.embed(["known"])[0].dim == 8
    with pytest.raises(EmbeddingCacheMiss) as ei:
        cached.embed(["known", "unknown"])
    assert ei.value.missing == ["unknown"]
    assert "build_name_embedding_fixtures" in str(ei.value)
    with pytest.raises(ValueError):
        CachedEmbedder(store, None)


def test_store_rejects_wrong_dim() -> None:
    key = ModelKey("m", "a" * 40, 4)
    with pytest.raises(ValueError, match="dim"):
        InMemoryEmbeddingStore().put_many(key, [CacheEntry("h", "t", Embedding((1.0, 0.0)))])


def test_file_store_roundtrip_float16_and_deterministic(tmp_path: Path) -> None:
    inner = FakeEmbedder(dim=16)
    path = tmp_path / "vec.jsonl.gz"
    store = FileEmbeddingStore(path, model_key(inner))
    cached = CachedEmbedder(store, inner)
    orig = cached.embed(["Lao Ban Zhang", "老班章"], with_sparse=True)
    assert store.flush() is True and store.flush() is False
    first = path.read_bytes()

    reloaded = FileEmbeddingStore(path, read_only=True)
    assert reloaded.key == model_key(inner) and len(reloaded) == 2
    back = CachedEmbedder(reloaded, None, key=reloaded.key).embed(["Lao Ban Zhang", "老班章"],
                                                                  with_sparse=True)
    for o, b in zip(orig, back, strict=True):
        assert b.dense == pytest.approx(o.dense, abs=1e-3)  # float16
        assert dense_cosine(o.dense, b.dense) == pytest.approx(1.0, abs=1e-6)
        assert b.sparse == pytest.approx(o.sparse, abs=1e-4)
    with pytest.raises(PermissionError):
        reloaded.put_many(reloaded.key, [])

    # Byte-identical rewrite (gzip mtime=0, sorted entries).
    again = FileEmbeddingStore(path)
    again._dirty = True
    again.flush()
    assert path.read_bytes() == first

    with gzip.open(path, "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline())
    assert header["dtype"] == "float16" and header["count"] == 2


def test_file_store_key_isolation(tmp_path: Path) -> None:
    path = tmp_path / "v.jsonl.gz"
    key = ModelKey("m", "a" * 40, 16)
    other = ModelKey("m", "b" * 40, 16)
    s = FileEmbeddingStore(path, key)
    s.put_many(key, [CacheEntry("h", "t", FakeEmbedder(dim=16).embed(["t"])[0])])
    s.flush()
    assert FileEmbeddingStore(path).get_many(other, ["h"]) == {}
    with pytest.raises(ValueError):
        FileEmbeddingStore(path).put_many(other, [])
    with pytest.raises(ValueError, match="holds"):
        FileEmbeddingStore(path, other)
    with pytest.raises(FileNotFoundError):
        FileEmbeddingStore(tmp_path / "missing.jsonl.gz")


# ----------------------------------------------------------------------
# Committed fixture + BGE-M3 stub behavior
# ----------------------------------------------------------------------


def test_committed_fixture_is_bge_m3_at_pinned_revision() -> None:
    emb = fixture_embedder()
    assert (emb.model_name, emb.revision, emb.dim) == (BGE_M3_MODEL, BGE_M3_REVISION, BGE_M3_DIM)
    size = DEFAULT_NAME_EMBEDDING_FIXTURE.stat().st_size
    assert size < 3 * 1024 * 1024, f"fixture grew to {size} bytes; keep it small"
    text = product_embedding_text("2016 Spring Lao Ban Zhang Sheng / Raw Puerh Tea - 357g Cake")
    with pytest.raises(EmbeddingCacheMiss):
        emb.embed([text + " not in the labeled set"])


def test_bge_m3_revision_pinned() -> None:
    assert len(BGE_M3_REVISION) == 40 and int(BGE_M3_REVISION, 16)
    e = BgeM3Embedder()  # constructing never loads torch or the model
    assert isinstance(e, Embedder) and e.supports_sparse and e.embed([]) == []


def test_bge_m3_missing_extra_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch → ImportError
    with pytest.raises(EmbedderUnavailable, match=r"\[embed\]"):
        BgeM3Embedder().embed(["Da Hong Pao"])


def test_base_package_does_not_import_torch() -> None:
    code = (
        "import sys, tea_scrapers.embed, tea_scrapers.embed.bge_m3, "
        "tea_scrapers.eval.embedding_eval; "
        "bad = [m for m in ('torch', 'transformers', 'numpy') if m in sys.modules]; "
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
