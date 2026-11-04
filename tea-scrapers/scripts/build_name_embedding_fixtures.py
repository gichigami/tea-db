#!/usr/bin/env python
"""Build the committed BGE-M3 name-embedding fixture for the H.1 pair texts (ROADMAP H.3).

Writes ``tests/fixtures/matching/name_embeddings_bge_m3.jsonl.gz``: dense
float16 vectors + sparse lexical weights for every text
:func:`tea_scrapers.eval.embedding_eval.collect_pair_texts` returns, so the
eval harness and its tests run offline through
:func:`tea_scrapers.embed.fake.fixture_embedder` (raises on a miss; never
downloads a model).

Needs the ``[embed]`` extra and ~2.2 GB of model files (fetched once, pinned to
``BGE_M3_REVISION``). Point ``--cache-dir`` / ``HF_HOME`` somewhere you can
delete afterwards. Existing entries are reused (read-through cache), so a
label-set append only embeds the new texts; ``--rebuild`` re-embeds all.

Usage::

    python scripts/build_name_embedding_fixtures.py [--cache-dir DIR] [--rebuild]
    python scripts/build_name_embedding_fixtures.py --check     # no model needed
    python scripts/build_name_embedding_fixtures.py --parity    # model-card parity check
    # Option-B probe model (spec §12 H.3 results; vectors NOT committed — see there):
    python scripts/build_name_embedding_fixtures.py --model qwen3 --parity
    python scripts/build_name_embedding_fixtures.py --model qwen3 --instruct \
        [--dim 256] [--augment replace] --out /scratch/qwen3.jsonl.gz

``--check`` exits 1 when a pair text is missing from the fixture (labels or
``matching_key`` changed) and lists stale entries (texts no longer requested).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tea_scrapers.embed.base import model_key, sparse_dot, text_hash
from tea_scrapers.embed.cache import CachedEmbedder, FileEmbeddingStore
from tea_scrapers.embed.fake import DEFAULT_NAME_EMBEDDING_FIXTURE
from tea_scrapers.eval.embedding_eval import collect_pair_texts

# BGE-M3 model card (huggingface.co/BAAI/bge-m3), computed there in fp16.
_PARITY_Q = ["What is BGE M3?", "Defination of BM25"]
_PARITY_P = [
    (
        "BGE M3 is an embedding model supporting dense retrieval, lexical matching and "
        "multi-vector interaction."
    ),
    (
        "BM25 is a bag-of-words retrieval function that ranks a set of documents based on the "
        "query terms appearing in each document"
    ),
]
_PARITY_DENSE = [[0.6265, 0.3477], [0.3499, 0.678]]
_PARITY_LEXICAL = 0.19554901123046875

# Qwen3-Embedding-0.6B model card, transformers example (fp32).
_QWEN3_TASK = "Given a web search query, retrieve relevant passages that answer the query"
_QWEN3_Q = ["What is the capital of China?", "Explain gravity"]
_QWEN3_D = [
    "The capital of China is Beijing.",
    (
        "Gravity is a force that attracts two bodies towards each other. It gives weight to "
        "physical objects and is responsible for the movement of planets around the sun."
    ),
]
_QWEN3_SCORES = [[0.7645568251609802, 0.14142508804798126],
                 [0.13549736142158508, 0.5999549627304077]]


def _check(path: Path) -> int:
    want = collect_pair_texts()
    store = FileEmbeddingStore(path, read_only=True)
    have = {e.text_hash for e in store.entries() if e.embedding.sparse is not None}
    missing = [t for t in want if text_hash(t) not in have]
    stale = sorted({e.text_hash for e in store.entries()} - {text_hash(t) for t in want})
    print(f"{path.name}: {len(store)} entries, {len(want)} requested, "
          f"{len(missing)} missing, {len(stale)} stale ({store.key})")
    for t in missing[:20]:
        print(f"  missing: {t!r}")
    return 1 if missing else 0


def _parity(embedder) -> int:
    q = embedder.embed(_PARITY_Q, with_sparse=True)
    p = embedder.embed(_PARITY_P, with_sparse=True)
    ok = True
    for i, eq in enumerate(q):
        for j, ep in enumerate(p):
            got = sum(a * b for a, b in zip(eq.dense, ep.dense, strict=True))
            ok &= abs(got - _PARITY_DENSE[i][j]) < 2e-3
            print(f"dense[{i}][{j}] = {got:.4f} (card {_PARITY_DENSE[i][j]})")
    assert q[0].sparse is not None and p[0].sparse is not None and q[1].sparse is not None
    lex = sparse_dot(q[0].sparse, p[0].sparse)
    zero = sparse_dot(q[0].sparse, q[1].sparse)
    ok &= abs(lex - _PARITY_LEXICAL) < 2e-3 and zero == 0.0
    print(f"lexical = {lex:.4f} (card {_PARITY_LEXICAL:.4f}); disjoint = {zero} (card 0.0)")
    print("parity OK" if ok else "parity FAILED")
    return 0 if ok else 1


def _qwen3_parity(cache_dir: Path | None, local_files_only: bool) -> int:
    from tea_scrapers.embed.qwen3 import Qwen3Embedder

    kw = {"cache_dir": cache_dir, "local_files_only": local_files_only, "max_length": 8192}
    q = Qwen3Embedder(instruction=_QWEN3_TASK, **kw).embed(_QWEN3_Q)  # type: ignore[arg-type]
    d = Qwen3Embedder(**kw).embed(_QWEN3_D)  # type: ignore[arg-type]
    ok = True
    for i, eq in enumerate(q):
        for j, ed in enumerate(d):
            got = sum(a * b for a, b in zip(eq.dense, ed.dense, strict=True))
            ok &= abs(got - _QWEN3_SCORES[i][j]) < 1e-4
            print(f"score[{i}][{j}] = {got:.6f} (card {_QWEN3_SCORES[i][j]:.6f})")
    print("parity OK" if ok else "parity FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_NAME_EMBEDDING_FIXTURE)
    ap.add_argument("--cache-dir", type=Path, default=None, help="HF hub cache dir")
    ap.add_argument("--local-files-only", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--prune", action="store_true", help="drop entries no longer requested")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--parity", action="store_true")
    ap.add_argument("--model", choices=("bge-m3", "qwen3"), default="bge-m3")
    ap.add_argument("--instruct", action="store_true",
                    help="qwen3: prefix NAME_MATCH_INSTRUCTION (model-card query format)")
    ap.add_argument("--dim", type=int, default=None, help="qwen3: MRL output dim (32-1024)")
    ap.add_argument("--augment", choices=("append", "replace"), action="append", default=[],
                    help="also embed hanzi→pinyin variants of every text (repeatable)")
    args = ap.parse_args(argv)

    if args.check:
        return _check(args.out)

    with_sparse = args.model == "bge-m3"
    if args.model == "qwen3":
        if args.parity:
            return _qwen3_parity(args.cache_dir, args.local_files_only)
        if args.out == DEFAULT_NAME_EMBEDDING_FIXTURE:
            ap.error("--model qwen3 needs an explicit --out (the default is the BGE-M3 fixture)")
        from tea_scrapers.embed.qwen3 import NAME_MATCH_INSTRUCTION, QWEN3_DIM, Qwen3Embedder

        model = Qwen3Embedder(
            cache_dir=args.cache_dir, local_files_only=args.local_files_only,
            dim=args.dim or QWEN3_DIM,
            instruction=NAME_MATCH_INSTRUCTION if args.instruct else None,
        )
    else:
        from tea_scrapers.embed.bge_m3 import BgeM3Embedder

        model = BgeM3Embedder(cache_dir=args.cache_dir, local_files_only=args.local_files_only)
        if args.parity:
            model.max_length = 8192
            return _parity(model)

    key = model_key(model)
    texts = collect_pair_texts(augment=(None, *args.augment))
    kept = []
    if args.prune and args.out.exists() and not args.rebuild:
        keep = {text_hash(t) for t in texts}
        kept = [e for e in FileEmbeddingStore(args.out, key).entries() if e.text_hash in keep]
    if (args.rebuild or args.prune) and args.out.exists():
        args.out.unlink()
    store = FileEmbeddingStore(args.out, key)
    if kept:
        store.put_many(key, kept)
    cached = CachedEmbedder(store, model)
    cached.embed(texts, with_sparse=with_sparse)
    wrote = store.flush()
    size = args.out.stat().st_size
    print(f"{args.out}: {len(store)} entries ({cached.misses} embedded, {cached.hits} cached), "
          f"{size / 1024:.0f} KiB, truncated={model.truncated}, wrote={wrote}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
