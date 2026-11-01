"""Name embeddings for the hybrid canonical matcher (ROADMAP H.3).

Scrapers spec §12 "Hybrid canonical matcher": product + producer *names* are
embedded with a local BGE-M3 model and compared alongside trigram. This
package is the model-agnostic layer the matcher (H.4+) and the eval harness
(H.1 / H.5) build on:

- :mod:`.base` — :class:`Embedder` protocol, :class:`Embedding` value type,
  :class:`ModelKey`, text normalization + ``text_hash``, vector math.
- :mod:`.cache` — :class:`EmbeddingStore` interface (in-memory + gzip-JSONL
  file stores; H.4's ``name_embedding`` table implements the same interface),
  and :class:`CachedEmbedder`, the read-through wrapper.
- :mod:`.fake` — :class:`FakeEmbedder` (deterministic, no model) and
  :func:`fixture_embedder` (committed vectors; raises on a cache miss).
- :mod:`.text` — which string is embedded for a product / producer (the
  suffix-stripped matching key, spec §12 hybrid sub-question #8).
- :mod:`.bge_m3` — the BGE-M3 implementation. Needs the optional ``[embed]``
  extra (torch + transformers); importing this package never imports torch.
"""

from tea_scrapers.embed.base import (
    TEXT_NORMALIZATION_VERSION,
    Embedder,
    EmbedderUnavailable,
    Embedding,
    EmbeddingCacheMiss,
    EmbeddingError,
    ModelKey,
    dense_cosine,
    l2_normalize,
    model_key,
    normalize_embed_text,
    sparse_cosine,
    sparse_dot,
    text_hash,
)
from tea_scrapers.embed.cache import (
    CacheEntry,
    CachedEmbedder,
    EmbeddingStore,
    FileEmbeddingStore,
    InMemoryEmbeddingStore,
)
from tea_scrapers.embed.fake import FakeEmbedder, fixture_embedder
from tea_scrapers.embed.text import producer_embedding_text, product_embedding_text

__all__ = [
    "TEXT_NORMALIZATION_VERSION",
    "CacheEntry",
    "CachedEmbedder",
    "Embedder",
    "EmbedderUnavailable",
    "Embedding",
    "EmbeddingCacheMiss",
    "EmbeddingError",
    "EmbeddingStore",
    "FakeEmbedder",
    "FileEmbeddingStore",
    "InMemoryEmbeddingStore",
    "ModelKey",
    "dense_cosine",
    "fixture_embedder",
    "l2_normalize",
    "model_key",
    "normalize_embed_text",
    "producer_embedding_text",
    "product_embedding_text",
    "sparse_cosine",
    "sparse_dot",
    "text_hash",
]
