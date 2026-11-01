"""Embedding cache: store interface + read-through embedder (ROADMAP H.3).

Entries are keyed by ``(ModelKey, text_hash)`` — i.e. ``(model, revision,
dim, sha256(normalized text))``. Two stores ship here:

- :class:`InMemoryEmbeddingStore` — per-process dict.
- :class:`FileEmbeddingStore` — one gzip-compressed JSONL file per
  :class:`ModelKey`, dense vectors as little-endian **float16** (the same
  precision H.4's ``halfvec(1024)`` column stores, so file-backed and
  Postgres-backed vectors agree). The committed name-embedding fixture is a
  read-only instance of this store.

H.4's ``name_embedding`` table implements :class:`EmbeddingStore` against
Postgres (``model`` / ``revision`` / ``text_hash`` columns); nothing above the
store changes.

File format (line 1 is a header; one entry per following line, sorted by
``h``; gzip ``mtime=0`` so output is byte-identical for identical content)::

    {"format": "tea-name-embeddings/1", "model": ..., "revision": ..., "dim": 1024,
     "dtype": "float16", "text_normalization": "nfc-ws-v1", "count": N}
    {"h": <text_hash>, "t": <normalized text>, "d": <base64 float16 LE>,
     "s": [[token_id, weight], ...] | null}
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import struct
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tea_scrapers.embed.base import (
    TEXT_NORMALIZATION_VERSION,
    Embedder,
    Embedding,
    EmbeddingCacheMiss,
    ModelKey,
    model_key,
    normalize_embed_text,
    text_hash,
)

FILE_FORMAT = "tea-name-embeddings/1"
SPARSE_DECIMALS = 4


@dataclass(frozen=True)
class CacheEntry:
    text_hash: str
    text: str  # normalized text — kept for audit / fixture staleness checks
    embedding: Embedding


class EmbeddingStore(Protocol):
    """Persistent or in-memory map ``(ModelKey, text_hash) → Embedding``."""

    def get_many(self, key: ModelKey, text_hashes: Collection[str]) -> dict[str, Embedding]:
        """Return the subset of ``text_hashes`` present (absent ⇒ miss)."""
        ...

    def put_many(self, key: ModelKey, entries: Sequence[CacheEntry]) -> None: ...


class InMemoryEmbeddingStore:
    def __init__(self) -> None:
        self._data: dict[tuple[ModelKey, str], CacheEntry] = {}

    def __len__(self) -> int:
        return len(self._data)

    def get_many(self, key: ModelKey, text_hashes: Collection[str]) -> dict[str, Embedding]:
        out: dict[str, Embedding] = {}
        for h in text_hashes:
            e = self._data.get((key, h))
            if e is not None:
                out[h] = e.embedding
        return out

    def put_many(self, key: ModelKey, entries: Sequence[CacheEntry]) -> None:
        for e in entries:
            _check_dim(key, e.embedding)
            self._data[(key, e.text_hash)] = e


def _check_dim(key: ModelKey, emb: Embedding) -> None:
    if emb.dim != key.dim:
        raise ValueError(f"embedding dim {emb.dim} != {key}")


def _encode_dense(vec: Sequence[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vec)}e", *vec)).decode("ascii")


def _decode_dense(b64: str, dim: int) -> tuple[float, ...]:
    raw = base64.b64decode(b64)
    if len(raw) != 2 * dim:
        raise ValueError(f"dense payload is {len(raw)} bytes, expected {2 * dim}")
    return struct.unpack(f"<{dim}e", raw)


class FileEmbeddingStore:
    """gzip-JSONL store for exactly one :class:`ModelKey`.

    Loads eagerly; :meth:`put_many` buffers in memory; :meth:`flush` rewrites
    the file atomically (temp + ``os.replace``). ``read_only=True`` (fixtures)
    refuses writes. Lookups under any other ``ModelKey`` miss; writes under
    another key raise, so one file can never mix embedding spaces.
    """

    def __init__(self, path: str | Path, key: ModelKey | None = None, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._entries: dict[str, CacheEntry] = {}
        self._dirty = False
        if self.path.exists():
            file_key = self._load()
            if key is not None and key != file_key:
                raise ValueError(f"{self.path} holds {file_key}, not {key}")
            self.key = file_key
        elif key is None:
            raise FileNotFoundError(f"{self.path} does not exist and no ModelKey was given")
        elif read_only:
            raise FileNotFoundError(self.path)
        else:
            self.key = key

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[CacheEntry]:
        return [self._entries[h] for h in sorted(self._entries)]

    def _load(self) -> ModelKey:
        with gzip.open(self.path, "rt", encoding="utf-8") as fh:
            header = json.loads(fh.readline())
            if header.get("format") != FILE_FORMAT:
                raise ValueError(f"{self.path}: unknown format {header.get('format')!r}")
            if header.get("text_normalization") != TEXT_NORMALIZATION_VERSION:
                raise ValueError(
                    f"{self.path}: text_normalization {header.get('text_normalization')!r} "
                    f"!= {TEXT_NORMALIZATION_VERSION!r}; text hashes are stale, regenerate"
                )
            key = ModelKey(header["model"], header["revision"], int(header["dim"]))
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                sparse = row.get("s")
                emb = Embedding(
                    dense=_decode_dense(row["d"], key.dim),
                    sparse=None if sparse is None else {int(t): float(w) for t, w in sparse},
                )
                self._entries[row["h"]] = CacheEntry(row["h"], row["t"], emb)
        if len(self._entries) != int(header.get("count", len(self._entries))):
            raise ValueError(f"{self.path}: header count != entries (truncated file?)")
        return key

    def get_many(self, key: ModelKey, text_hashes: Collection[str]) -> dict[str, Embedding]:
        if key != self.key:
            return {}
        return {h: self._entries[h].embedding for h in text_hashes if h in self._entries}

    def put_many(self, key: ModelKey, entries: Sequence[CacheEntry]) -> None:
        if self.read_only:
            raise PermissionError(f"{self.path} is read-only")
        if key != self.key:
            raise ValueError(f"{self.path} holds {self.key}, cannot store {key}")
        for e in entries:
            _check_dim(key, e.embedding)
            self._entries[e.text_hash] = e
        self._dirty = self._dirty or bool(entries)

    def to_bytes(self) -> bytes:
        header = {
            "format": FILE_FORMAT,
            "model": self.key.model,
            "revision": self.key.revision,
            "dim": self.key.dim,
            "dtype": "float16",
            "text_normalization": TEXT_NORMALIZATION_VERSION,
            "count": len(self._entries),
        }
        lines = [json.dumps(header, sort_keys=True)]
        for e in self.entries():
            sp = e.embedding.sparse
            lines.append(
                json.dumps(
                    {
                        "h": e.text_hash,
                        "t": e.text,
                        "d": _encode_dense(e.embedding.dense),
                        "s": None
                        if sp is None
                        else [[t, round(w, SPARSE_DECIMALS)] for t, w in sorted(sp.items())],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(("\n".join(lines) + "\n").encode("utf-8"))
        return buf.getvalue()

    def flush(self) -> bool:
        """Write if dirty. Returns True when the file was written."""
        if self.read_only or not self._dirty:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(self.to_bytes())
        os.replace(tmp, self.path)
        self._dirty = False
        return True


class CachedEmbedder:
    """Read-through cache in front of an :class:`Embedder`.

    With ``inner=None`` it is cache-only and raises :class:`EmbeddingCacheMiss`
    for unknown texts — that is how tests / CI run with no model and no network.
    An entry stored without sparse weights is a miss for ``with_sparse=True``.
    """

    def __init__(
        self,
        store: EmbeddingStore,
        inner: Embedder | None = None,
        *,
        key: ModelKey | None = None,
        supports_sparse: bool | None = None,
    ) -> None:
        if inner is None and key is None:
            raise ValueError("cache-only CachedEmbedder needs an explicit ModelKey")
        if inner is not None and key is not None and key != model_key(inner):
            raise ValueError(f"key {key} != inner embedder {model_key(inner)}")
        self.key = key if key is not None else model_key(inner)  # type: ignore[arg-type]
        self.model_name, self.revision, self.dim = self.key.model, self.key.revision, self.key.dim
        self.supports_sparse = (
            supports_sparse if supports_sparse is not None
            else (inner.supports_sparse if inner is not None else True)
        )
        self._store = store
        self._inner = inner
        self.hits = 0
        self.misses = 0

    def embed(self, texts: Sequence[str], *, with_sparse: bool = False) -> list[Embedding]:
        if with_sparse and not self.supports_sparse:
            raise ValueError(f"{self.key} does not provide sparse weights")
        norm = [normalize_embed_text(t) for t in texts]
        hashes = [text_hash(t) for t in norm]
        uniq: dict[str, str] = dict(zip(hashes, norm, strict=True))
        found = {
            h: e
            for h, e in self._store.get_many(self.key, list(uniq)).items()
            if not with_sparse or e.sparse is not None
        }
        missing = [h for h in uniq if h not in found]
        self.hits += len(uniq) - len(missing)
        self.misses += len(missing)
        if missing:
            if self._inner is None:
                raise EmbeddingCacheMiss([uniq[h] for h in missing], self.key)
            fresh = self._inner.embed([uniq[h] for h in missing], with_sparse=with_sparse)
            if len(fresh) != len(missing):
                raise RuntimeError("inner embedder returned the wrong number of embeddings")
            entries = [CacheEntry(h, uniq[h], e) for h, e in zip(missing, fresh, strict=True)]
            self._store.put_many(self.key, entries)
            found.update((e.text_hash, e.embedding) for e in entries)
        return [found[h] for h in hashes]


def embed_mapping(embedder: Embedder, texts: Sequence[str], *, with_sparse: bool = False
                  ) -> Mapping[str, Embedding]:
    """Embed unique texts once; return ``normalized text → Embedding``."""
    uniq = sorted({normalize_embed_text(t) for t in texts})
    return dict(zip(uniq, embedder.embed(uniq, with_sparse=with_sparse), strict=True))


__all__ = [
    "FILE_FORMAT",
    "CacheEntry",
    "CachedEmbedder",
    "EmbeddingStore",
    "FileEmbeddingStore",
    "InMemoryEmbeddingStore",
    "embed_mapping",
]
