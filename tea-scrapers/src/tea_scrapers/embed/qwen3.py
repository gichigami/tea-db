"""Qwen3-Embedding-0.6B name embedder: dense, last-token pooled, MRL-truncatable (ROADMAP H.3).

Owner "Option B" probe (spec §12 hybrid entry, H.3 results): is H.3's weak
BGE-M3 result a BGE-M3 problem or an embeddings problem? This is the second
model behind the same :class:`~tea_scrapers.embed.base.Embedder` protocol and
the same optional ``[embed]`` extra (``torch`` + ``transformers>=4.51`` — the
Qwen3 architecture landed in 4.51). Importing this module is free; torch /
transformers are imported on first :meth:`Qwen3Embedder.embed`.

Inference follows the model card's ``transformers`` example exactly
(huggingface.co/Qwen/Qwen3-Embedding-0.6B at :data:`QWEN3_REVISION`):

- tokenizer loaded with ``padding_side="left"``; its post-processor appends
  ``<|endoftext|>`` to every sequence (checked at load — pooling is wrong
  without it);
- **last-token pooling** (the ``<|endoftext|>`` hidden state; with left
  padding that is column ``-1`` for every row), then L2-normalize;
- optional instruction, query side: ``"Instruct: {task}\\nQuery:{text}"``
  (no space after ``Query:``, as on the card). Name matching is symmetric, so
  when an instruction is set it is applied to **every** text;
- **MRL**: ``dim`` < 1024 keeps the first ``dim`` components and
  re-normalizes (cosine is scale-invariant, so truncating before or after
  the first normalization is the same).

**Cache identity.** The instruction changes every vector, so it is part of
``model_name`` (``"Qwen/Qwen3-Embedding-0.6B+inst:<sha256[:12]>"``) and hence
of the :class:`~tea_scrapers.embed.base.ModelKey`; ``dim`` is already in the
key. ``text_hash`` stays the hash of the bare normalized name — the prompt is
applied after normalization (``normalize_embed_text`` would collapse its
``\\n``).

Parity against the card's example is ``scripts/build_name_embedding_fixtures.py
--model qwen3 --parity``. Weights are ``model.safetensors`` (~1.2 GB, bf16
on disk, run in fp32 on CPU like the card example).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tea_scrapers.embed.base import EmbedderUnavailable, Embedding, normalize_embed_text

QWEN3_MODEL = "Qwen/Qwen3-Embedding-0.6B"
# huggingface.co/api/models/Qwen/Qwen3-Embedding-0.6B "sha" (lastModified 2026-04-20),
# pinned 2026-11-04.
QWEN3_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
QWEN3_DIM = 1024
QWEN3_MIN_DIM = 32  # model card: "user-defined output dimensions ranging from 32 to 1024"
QWEN3_FILES: tuple[str, ...] = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "model.safetensors",
)
# Entity-matching instruction tried in the Option-B probe (spec §12 H.3 results).
NAME_MATCH_INSTRUCTION = "Given a tea product name, retrieve names of the same product"
PRODUCER_MATCH_INSTRUCTION = (
    "Given a tea producer or brand name, retrieve names of the same producer"
)
INSTALL_HINT = (
    "Qwen3-Embedding needs the optional [embed] extra (transformers>=4.51): "
    "pip install -e '.[embed]' --extra-index-url https://download.pytorch.org/whl/cpu"
)


def instruction_tag(instruction: str | None) -> str:
    """``""`` for no instruction, else ``"+inst:<sha256[:12]>"`` (ModelKey suffix)."""
    if not instruction:
        return ""
    return "+inst:" + hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:12]


def format_input(text: str, instruction: str | None) -> str:
    """Model-card prompt: ``Instruct: {task}\\nQuery:{text}`` (or the bare text)."""
    return f"Instruct: {instruction}\nQuery:{text}" if instruction else text


class Qwen3Embedder:
    """Local Qwen3-Embedding-0.6B. Dense only. Not thread-safe."""

    supports_sparse = False

    def __init__(
        self,
        *,
        revision: str = QWEN3_REVISION,
        dim: int = QWEN3_DIM,
        instruction: str | None = None,
        cache_dir: str | Path | None = None,
        local_files_only: bool = False,
        max_length: int = 128,
        batch_size: int = 32,
        num_threads: int | None = None,
    ) -> None:
        if not QWEN3_MIN_DIM <= dim <= QWEN3_DIM:
            raise ValueError(f"dim must be in [{QWEN3_MIN_DIM}, {QWEN3_DIM}], got {dim}")
        self.revision = revision
        self.dim = dim
        self.instruction = instruction or None
        self.model_name = QWEN3_MODEL + instruction_tag(self.instruction)
        self._cache_dir = None if cache_dir is None else str(cache_dir)
        self._local_files_only = local_files_only
        self.max_length = max_length
        self.batch_size = batch_size
        self._num_threads = num_threads
        self._loaded: tuple[Any, Any] | None = None
        self.truncated = 0  # inputs longer than max_length tokens (names: expect 0)

    def _load(self) -> tuple[Any, Any]:
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
            QWEN3_MODEL,
            revision=self.revision,
            allow_patterns=list(QWEN3_FILES),
            cache_dir=self._cache_dir,
            local_files_only=self._local_files_only,
        )
        tok = AutoTokenizer.from_pretrained(path, padding_side="left")
        eos = tok.convert_tokens_to_ids("<|endoftext|>")
        if tok("x")["input_ids"][-1] != eos:
            raise EmbedderUnavailable(
                f"tokenizer at {self.revision} does not append <|endoftext|>; "
                "last-token pooling would read the wrong position"
            )
        # fp32 on CPU like the card example (weights are bf16 on disk).
        model = AutoModel.from_pretrained(path).float()
        model.eval()
        hidden = int(model.config.hidden_size)
        if hidden != QWEN3_DIM:
            raise EmbedderUnavailable(f"unexpected hidden size {hidden} at {self.revision}")
        self._loaded = (tok, model)
        return self._loaded

    def embed(self, texts: Sequence[str], *, with_sparse: bool = False) -> list[Embedding]:
        if with_sparse:
            raise ValueError("Qwen3Embedder is dense-only (supports_sparse=False)")
        if not texts:
            return []
        tok, model = self._load()
        import torch

        prompts = [format_input(normalize_embed_text(t), self.instruction) for t in texts]
        out: list[Embedding] = []
        for start in range(0, len(prompts), self.batch_size):
            batch = prompts[start : start + self.batch_size]
            enc = tok(batch, padding=True, truncation=True, max_length=self.max_length,
                      return_tensors="pt")
            self.truncated += sum(
                len(ids) > self.max_length for ids in tok(batch)["input_ids"]
            )
            with torch.inference_mode():
                hidden = model(**enc, return_dict=True).last_hidden_state
                pooled = last_token_pool(hidden, enc["attention_mask"])
                dense = torch.nn.functional.normalize(pooled[:, : self.dim].float(), dim=-1)
            out.extend(Embedding(tuple(row)) for row in dense.tolist())
        return out


def last_token_pool(last_hidden_states: Any, attention_mask: Any) -> Any:
    """Model-card ``last_token_pool`` (handles left *and* right padding)."""
    import torch

    left_padding = bool(attention_mask[:, -1].sum() == attention_mask.shape[0])
    if left_padding:
        return last_hidden_states[:, -1]
    lengths = attention_mask.sum(dim=1) - 1
    rows = torch.arange(last_hidden_states.shape[0], device=last_hidden_states.device)
    return last_hidden_states[rows, lengths]


__all__ = [
    "NAME_MATCH_INSTRUCTION",
    "PRODUCER_MATCH_INSTRUCTION",
    "QWEN3_DIM",
    "QWEN3_FILES",
    "QWEN3_MODEL",
    "QWEN3_REVISION",
    "Qwen3Embedder",
    "format_input",
    "instruction_tag",
    "last_token_pool",
]
