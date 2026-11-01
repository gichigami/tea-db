"""Embedder protocol, value types, text hashing and vector math (ROADMAP H.3).

Stdlib only — no numpy, no torch — so the base install, CI and the offline
eval harness can use every piece here without the ``[embed]`` extra.

**Cache identity.** An embedding is addressed by
``(model, revision, dim, text_hash)`` where ``text_hash`` is the SHA-256 of
:func:`normalize_embed_text` applied to the input. The embedder embeds that
same normalized string, so the hash always describes exactly what was fed to
the model. ``revision`` is a pinned Hugging Face commit SHA, never a branch
name: a moved ``main`` must not silently change vectors under a stable key.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Bump if normalize_embed_text changes: every stored text_hash is invalidated.
TEXT_NORMALIZATION_VERSION = "nfc-ws-v1"

_WS_RE = re.compile(r"\s+")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class EmbeddingError(RuntimeError):
    """Base class for embed-package errors."""


class EmbedderUnavailable(EmbeddingError):
    """The real model cannot be loaded (``[embed]`` extra missing, no weights)."""


class EmbeddingCacheMiss(EmbeddingError):
    """A cache-only embedder (fixtures, tests) was asked for an unknown text."""

    def __init__(self, missing: Sequence[str], key: ModelKey) -> None:
        self.missing = list(missing)
        self.key = key
        preview = ", ".join(repr(t) for t in self.missing[:5])
        more = f" (+{len(self.missing) - 5} more)" if len(self.missing) > 5 else ""
        super().__init__(
            f"{len(self.missing)} text(s) not in the embedding cache for {key}: "
            f"{preview}{more}. Regenerate fixture vectors with "
            "`python scripts/build_name_embedding_fixtures.py` (needs the [embed] extra)."
        )


def normalize_embed_text(text: str) -> str:
    """NFC + collapse whitespace + strip. Deliberately no case folding.

    Which *string* gets embedded (e.g. the lower-cased, suffix-stripped
    matching key) is the caller's decision (:mod:`tea_scrapers.embed.text`);
    this only removes representation noise that must never change a hash.
    """
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def text_hash(text: str) -> str:
    """Hex SHA-256 of the normalized text (64 chars; the cache / DB key)."""
    return hashlib.sha256(normalize_embed_text(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True, order=True)
class ModelKey:
    """Identity of an embedding space. Vectors are comparable only within one."""

    model: str
    revision: str
    dim: int

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must be non-empty")
        if not _SHA_RE.match(self.revision):
            raise ValueError(
                f"revision must be a pinned 40-hex commit SHA, got {self.revision!r}"
            )
        if self.dim <= 0:
            raise ValueError("dim must be positive")

    def __str__(self) -> str:
        return f"{self.model}@{self.revision[:12]}/{self.dim}d"


@dataclass(frozen=True)
class Embedding:
    """One text's embedding.

    ``dense`` is L2-normalized (to float16 precision when read back from a
    store). ``sparse`` maps model-vocabulary token id → non-negative lexical
    weight (BGE-M3's sparse head) or is ``None`` when not computed.
    """

    dense: tuple[float, ...]
    sparse: Mapping[int, float] | None = field(default=None, compare=False)

    @property
    def dim(self) -> int:
        return len(self.dense)


@runtime_checkable
class Embedder(Protocol):
    """Embeds texts into one :class:`ModelKey` space.

    Contract:
    - ``embed`` returns one :class:`Embedding` per input, in input order;
      inputs are passed through :func:`normalize_embed_text` first.
    - ``dense`` vectors are L2-normalized and ``len == dim``.
    - ``with_sparse=True`` requires ``supports_sparse``; otherwise ``ValueError``.
    - Deterministic for a fixed ``(model_name, revision)`` on one machine.
    """

    model_name: str
    revision: str
    dim: int
    supports_sparse: bool

    def embed(self, texts: Sequence[str], *, with_sparse: bool = False) -> list[Embedding]: ...


def model_key(embedder: Embedder) -> ModelKey:
    return ModelKey(embedder.model_name, embedder.revision, embedder.dim)


# ----------------------------------------------------------------------
# Vector math (pure Python; ~1k pairs x 1024 dims is well under a second)
# ----------------------------------------------------------------------


def l2_normalize(vec: Sequence[float]) -> tuple[float, ...]:
    norm = math.sqrt(math.fsum(x * x for x in vec))
    if norm == 0.0:
        raise ValueError("cannot normalize a zero vector")
    return tuple(x / norm for x in vec)


def dense_cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Full cosine (re-normalizes, so float16-rounded vectors stay exact-ish)."""
    if len(a) != len(b):
        raise ValueError(f"dim mismatch: {len(a)} vs {len(b)}")
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def sparse_dot(a: Mapping[int, float], b: Mapping[int, float]) -> float:
    """BGE-M3 lexical matching score: Σ over shared tokens of w_a · w_b.

    Same as FlagEmbedding's ``compute_lexical_matching_score``. Unbounded
    above (depends on text length), so not directly thresholdable.
    """
    if len(a) > len(b):
        a, b = b, a
    return math.fsum(w * b[t] for t, w in a.items() if t in b)


def sparse_cosine(a: Mapping[int, float], b: Mapping[int, float]) -> float:
    """:func:`sparse_dot` normalized to [0, 1] — the thresholdable form."""
    na = math.sqrt(math.fsum(w * w for w in a.values()))
    nb = math.sqrt(math.fsum(w * w for w in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sparse_dot(a, b) / (na * nb)


__all__ = [
    "TEXT_NORMALIZATION_VERSION",
    "Embedder",
    "EmbedderUnavailable",
    "Embedding",
    "EmbeddingCacheMiss",
    "EmbeddingError",
    "ModelKey",
    "dense_cosine",
    "l2_normalize",
    "model_key",
    "normalize_embed_text",
    "sparse_cosine",
    "sparse_dot",
    "text_hash",
]
