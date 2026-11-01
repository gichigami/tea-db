"""Test embedders: no model, no network, no download (ROADMAP H.3 / H.7).

- :class:`FakeEmbedder` — deterministic SHA-256-seeded unit vectors plus
  whitespace-token sparse weights. Distinct texts are near-orthogonal; it says
  nothing about semantic similarity. Use it for protocol / cache / plumbing
  tests.
- :func:`fixture_embedder` — the committed BGE-M3 vectors for the H.1 labeled
  pair texts, cache-only: an unknown text raises :class:`EmbeddingCacheMiss`
  (never falls back to downloading a model).
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence
from pathlib import Path

from tea_scrapers.embed.base import Embedding, l2_normalize, normalize_embed_text
from tea_scrapers.embed.cache import CachedEmbedder, FileEmbeddingStore

FAKE_REVISION = "0" * 40

DEFAULT_NAME_EMBEDDING_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "tests" / "fixtures" / "matching" / "name_embeddings_bge_m3.jsonl.gz"
)


class FakeEmbedder:
    supports_sparse = True

    def __init__(self, dim: int = 16, model_name: str = "fake/sha256", revision: str = FAKE_REVISION):
        self.dim = dim
        self.model_name = model_name
        self.revision = revision
        self.calls: list[list[str]] = []

    def _dense(self, text: str) -> tuple[float, ...]:
        out: list[float] = []
        counter = 0
        while len(out) < self.dim:
            block = hashlib.sha256(f"{counter}\x00{text}".encode()).digest()
            # 8 signed int32 per block, mapped to [-1, 1).
            out.extend(v / 2**31 for v in struct.unpack("<8i", block))
            counter += 1
        return l2_normalize(out[: self.dim])

    @staticmethod
    def _sparse(text: str) -> dict[int, float]:
        weights: dict[int, float] = {}
        for tok in text.lower().split():
            tid = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:3], "little")
            weights[tid] = round(0.1 + (tid % 50) / 100, 4)
        return weights

    def embed(self, texts: Sequence[str], *, with_sparse: bool = False) -> list[Embedding]:
        norm = [normalize_embed_text(t) for t in texts]
        self.calls.append(norm)
        return [
            Embedding(self._dense(t), self._sparse(t) if with_sparse else None) for t in norm
        ]


def fixture_embedder(path: str | Path = DEFAULT_NAME_EMBEDDING_FIXTURE) -> CachedEmbedder:
    """Cache-only embedder over a read-only :class:`FileEmbeddingStore`."""
    store = FileEmbeddingStore(path, read_only=True)
    return CachedEmbedder(store, None, key=store.key, supports_sparse=True)


__all__ = ["DEFAULT_NAME_EMBEDDING_FIXTURE", "FAKE_REVISION", "FakeEmbedder", "fixture_embedder"]
