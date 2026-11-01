"""BGE-M3 name embedder: dense (CLS, 1024-d) + sparse lexical weights (ROADMAP H.3).

Requires the optional ``[embed]`` extra (``torch`` + ``transformers``; CPU
wheels are enough). Importing this module is free — torch / transformers are
imported on first :meth:`BgeM3Embedder.embed` and a missing extra raises
:class:`EmbedderUnavailable` with the install hint.

**Why not FlagEmbedding / sentence-transformers** (spec §12 hybrid entry,
§11 new-dependency rule): both sit *on top of* ``transformers`` + ``torch``.
sentence-transformers exposes only the dense head for BGE-M3 and adds
scikit-learn / scipy / Pillow; FlagEmbedding exposes dense + sparse but its
install pulls ``datasets``, ``accelerate``, ``peft``, ``sentence-transformers``
and ``ir-datasets`` for training code we never run. BGE-M3's inference is
small and documented — dense = L2-normalized ``[CLS]`` hidden state; sparse =
``relu(sparse_linear(hidden))`` per token, max-pooled per token id, special
tokens dropped — so we reproduce exactly that (FlagEmbedding
``BGEM3FlagModel`` semantics) over ``transformers.AutoModel``. A parity test
against the published model-card numbers is in
``scripts/build_name_embedding_fixtures.py --parity``.

The model files are fetched once with ``huggingface_hub.snapshot_download``
pinned to :data:`BGE_M3_REVISION` (a commit SHA, not ``main``) and limited to
the 7 files inference needs (~2.2 GB; the repo's ONNX copy and ColBERT head
are skipped). ``use_safetensors=False`` is passed explicitly: the repo ships
only ``pytorch_model.bin`` and transformers otherwise starts a background
Hub "safetensors conversion" request. Torch ≥ 2.6 loads it ``weights_only``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tea_scrapers.embed.base import Embedding, EmbedderUnavailable, normalize_embed_text

BGE_M3_MODEL = "BAAI/bge-m3"
# huggingface.co/api/models/BAAI/bge-m3 "sha" (lastModified 2024-07-03), pinned 2026-11-01.
BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
BGE_M3_DIM = 1024
BGE_M3_FILES: tuple[str, ...] = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "pytorch_model.bin",
    "sparse_linear.pt",
)
INSTALL_HINT = (
    "BGE-M3 needs the optional [embed] extra: "
    "pip install -e '.[embed]' --extra-index-url https://download.pytorch.org/whl/cpu"
)


class BgeM3Embedder:
    """Local BGE-M3. Not thread-safe; one instance per process is plenty."""

    model_name = BGE_M3_MODEL
    dim = BGE_M3_DIM
    supports_sparse = True

    def __init__(
        self,
        *,
        revision: str = BGE_M3_REVISION,
        cache_dir: str | Path | None = None,
        local_files_only: bool = False,
        max_length: int = 128,
        batch_size: int = 32,
        num_threads: int | None = None,
    ) -> None:
        self.revision = revision
        self._cache_dir = None if cache_dir is None else str(cache_dir)
        self._local_files_only = local_files_only
        self.max_length = max_length
        self.batch_size = batch_size
        self._num_threads = num_threads
        self._loaded: tuple[Any, Any, Any, frozenset[int]] | None = None
        self.truncated = 0  # inputs longer than max_length tokens (names: expect 0)

    def _load(self) -> tuple[Any, Any, Any, frozenset[int]]:
        if self._loaded is not None:
            return self._loaded
        try:
            import torch
            from huggingface_hub import snapshot_download
            from transformers import AutoModel, AutoTokenizer
        except ImportError as err:  # pragma: no cover - exercised via sys.modules stub
            raise EmbedderUnavailable(f"{INSTALL_HINT} ({err})") from err

        if self._num_threads:
            torch.set_num_threads(self._num_threads)
        path = snapshot_download(
            BGE_M3_MODEL,
            revision=self.revision,
            allow_patterns=list(BGE_M3_FILES),
            cache_dir=self._cache_dir,
            local_files_only=self._local_files_only,
        )
        tok = AutoTokenizer.from_pretrained(path)
        model = AutoModel.from_pretrained(path, use_safetensors=False)
        model.eval()
        hidden = int(model.config.hidden_size)
        if hidden != BGE_M3_DIM:
            raise EmbedderUnavailable(f"unexpected hidden size {hidden} at {self.revision}")
        sparse_linear = torch.nn.Linear(hidden, 1)
        state = torch.load(Path(path) / "sparse_linear.pt", map_location="cpu", weights_only=True)
        sparse_linear.load_state_dict(state)
        sparse_linear.eval()
        special = frozenset(
            int(i)
            for i in (tok.cls_token_id, tok.eos_token_id, tok.pad_token_id, tok.unk_token_id)
            if i is not None
        )
        self._loaded = (tok, model, sparse_linear, special)
        return self._loaded

    def embed(self, texts: Sequence[str], *, with_sparse: bool = False) -> list[Embedding]:
        if not texts:
            return []
        tok, model, sparse_linear, special = self._load()
        import torch

        norm = [normalize_embed_text(t) for t in texts]
        out: list[Embedding] = []
        for start in range(0, len(norm), self.batch_size):
            batch = norm[start : start + self.batch_size]
            enc = tok(
                batch,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
                return_overflowing_tokens=False,
            )
            self.truncated += sum(
                len(ids) > self.max_length
                for ids in tok(batch, add_special_tokens=True)["input_ids"]
            )
            with torch.inference_mode():
                hidden = model(**enc, return_dict=True).last_hidden_state
                dense = torch.nn.functional.normalize(hidden[:, 0].float(), dim=-1)
                weights = (
                    torch.relu(sparse_linear(hidden)).squeeze(-1) if with_sparse else None
                )
            ids = enc["input_ids"].tolist()
            mask = enc["attention_mask"].tolist()
            for row in range(len(batch)):
                sparse: dict[int, float] | None = None
                if weights is not None:
                    sparse = {}
                    for tid, w, m in zip(ids[row], weights[row].tolist(), mask[row], strict=True):
                        if m and tid not in special and w > 0 and w > sparse.get(tid, 0.0):
                            sparse[tid] = float(w)
                out.append(Embedding(tuple(dense[row].tolist()), sparse))
        return out

    def token_strings(self, sparse: dict[int, float]) -> dict[str, float]:
        """Debug helper: map token ids back to vocabulary pieces."""
        tok = self._load()[0]
        return {tok.convert_ids_to_tokens(t): w for t, w in sparse.items()}


__all__ = ["BGE_M3_DIM", "BGE_M3_FILES", "BGE_M3_MODEL", "BGE_M3_REVISION", "BgeM3Embedder"]
