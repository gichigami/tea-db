"""Qwen3-Embedding adapter + Option-B eval helpers — no torch, no model, no network.

The real-model parity check (card example, matched to ~3e-7) is
``scripts/build_name_embedding_fixtures.py --model qwen3 --parity``; these
tests pin the parts that determine the cache key and the prompt, plus the
eval-path helpers (MRL truncation, pinyin probe, dense-only harness run).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tea_scrapers.embed import (
    EmbedderUnavailable,
    Embedding,
    FakeEmbedder,
    FileEmbeddingStore,
    fixture_embedder,
    model_key,
    text_hash,
)
from tea_scrapers.embed.cache import CacheEntry
from tea_scrapers.embed.qwen3 import (
    NAME_MATCH_INSTRUCTION,
    QWEN3_MODEL,
    QWEN3_REVISION,
    Qwen3Embedder,
    format_input,
    instruction_tag,
)
from tea_scrapers.eval.embedding_eval import (
    TransformedScorer,
    TruncatedEmbedder,
    collect_pair_texts,
    pinyin_augment,
    run_comparison,
)


def test_revision_pinned_and_key_valid() -> None:
    assert len(QWEN3_REVISION) == 40 and QWEN3_REVISION != "main"
    key = model_key(Qwen3Embedder())
    assert (key.model, key.revision, key.dim) == (QWEN3_MODEL, QWEN3_REVISION, 1024)


def test_instruction_is_part_of_model_key() -> None:
    plain = Qwen3Embedder()
    inst = Qwen3Embedder(instruction=NAME_MATCH_INSTRUCTION)
    assert instruction_tag(None) == "" and instruction_tag("") == ""
    assert inst.model_name == QWEN3_MODEL + instruction_tag(NAME_MATCH_INSTRUCTION)
    assert inst.model_name.startswith(QWEN3_MODEL + "+inst:") and len(inst.model_name) == (
        len(QWEN3_MODEL) + len("+inst:") + 12)
    assert model_key(plain) != model_key(inst)
    # MRL dim is in the key too.
    assert model_key(Qwen3Embedder(dim=256)).dim == 256


def test_prompt_format_matches_model_card() -> None:
    assert format_input("da hong pao", None) == "da hong pao"
    assert format_input("da hong pao", "Task X") == "Instruct: Task X\nQuery:da hong pao"


@pytest.mark.parametrize("dim", [0, 31, 1025])
def test_dim_bounds(dim: int) -> None:
    with pytest.raises(ValueError):
        Qwen3Embedder(dim=dim)


def test_dense_only() -> None:
    m = Qwen3Embedder()
    assert m.supports_sparse is False
    with pytest.raises(ValueError):
        m.embed(["x"], with_sparse=True)
    assert m.embed([]) == []  # no model load for an empty batch


def test_missing_extra_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(EmbedderUnavailable, match=r"\[embed\]"):
        Qwen3Embedder().embed(["x"])


# ----------------------------------------------------------------------
# Eval-path helpers
# ----------------------------------------------------------------------


def test_truncated_embedder_is_prefix_renormalized() -> None:
    inner = FakeEmbedder(dim=16)
    t = TruncatedEmbedder(inner, 4)
    assert (t.dim, t.model_name, t.supports_sparse) == (4, inner.model_name, False)
    full = inner.embed(["abc"])[0].dense
    got = t.embed(["abc"])[0].dense
    norm = sum(x * x for x in full[:4]) ** 0.5
    assert got == pytest.approx([x / norm for x in full[:4]])
    with pytest.raises(ValueError):
        TruncatedEmbedder(inner, 17)
    with pytest.raises(ValueError):
        t.embed(["abc"], with_sparse=True)


def test_pinyin_augment_modes() -> None:
    pytest.importorskip("pypinyin")
    assert pinyin_augment("da hong pao") == "da hong pao"  # no hanzi → unchanged
    assert pinyin_augment("大红袍", "replace") == "da hong pao"
    assert pinyin_augment("2025 冰岛老寨 生普洱", "append") == (
        "2025 冰岛老寨 生普洱 bing dao lao zhai sheng pu er")
    assert pinyin_augment("menghai 大益", "replace") == "menghai da yi"
    with pytest.raises(ValueError):
        pinyin_augment("大益", "bogus")  # type: ignore[arg-type]


def test_transformed_scorer_applies_fn_both_sides() -> None:
    class Echo:
        name = "echo"

        def __init__(self) -> None:
            self.seen: list[tuple[str, str]] = []

        def score(self, pairs):  # type: ignore[no-untyped-def]
            self.seen.extend(pairs)
            return [0.5] * len(pairs)

    inner = Echo()
    s = TransformedScorer(inner, str.upper)
    assert s.name == "echo" and s.score([("a", "b")]) == [0.5]
    assert inner.seen == [("A", "B")]


def test_collect_pair_texts_with_augment_is_superset() -> None:
    pytest.importorskip("pypinyin")
    plain = set(collect_pair_texts())
    aug = set(collect_pair_texts(augment=(None, "replace")))
    assert plain < aug
    assert "da hong pao wu yi shan yan cha" in aug  # 大红袍 武夷山岩茶, transliterated


def test_dense_only_comparison_skips_sparse() -> None:
    result = run_comparison(TruncatedEmbedder(fixture_embedder(), 256), trigram=None)
    for view in result["views"]:  # type: ignore[union-attr]
        assert set(view["scorers"]) == {"dense"}
    assert str(result["embedder"]).endswith("/256d")


def test_augmented_comparison_runs_offline() -> None:
    pytest.importorskip("pypinyin")
    result = run_comparison(FakeEmbedder(), trigram=None, augment="replace")
    assert result["augment"] == "replace"
    assert [v["entity"] for v in result["views"]] == ["product", "producer"]  # type: ignore[union-attr]


def test_fixture_embedder_sparse_support_follows_file(tmp_path: Path) -> None:
    fake = FakeEmbedder(dim=8)
    key = model_key(fake)
    path = tmp_path / "dense_only.jsonl.gz"
    store = FileEmbeddingStore(path, key)
    store.put_many(key, [CacheEntry(text_hash("x"), "x", Embedding(fake.embed(["x"])[0].dense))])
    store.flush()
    assert fixture_embedder(path).supports_sparse is False
    assert fixture_embedder().supports_sparse is True  # committed BGE-M3 fixture
