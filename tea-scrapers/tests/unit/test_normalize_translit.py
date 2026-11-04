"""Hanzi → pinyin matching keys (ROADMAP H.2b part 1; spec §12 hybrid sub-q #9)."""

from __future__ import annotations

import pytest

from tea_scrapers.normalize.attributes import TeaStyle, parse_name_attributes
from tea_scrapers.normalize.translit import (
    has_cjk,
    pinyin_key,
    pinyin_text,
    skeleton_key,
    usable_skeleton,
)


@pytest.mark.parametrize(
    ("hanzi", "pinyin"),
    [
        ("大红袍", "da hong pao"),
        ("勐海茶厂", "meng hai cha chang"),
        ("下关", "xia guan"),
        ("金家棋", "jin jia qi"),
        ("老班章", "lao ban zhang"),
        ("冰岛老寨", "bing dao lao zhai"),
        ("绿茶", "lu cha"),  # ü folded to u, never "lv"
    ],
)
def test_pinyin_key_toneless_spaced(hanzi: str, pinyin: str) -> None:
    assert pinyin_key(hanzi) == pinyin


def test_mixed_script_keeps_latin_and_spaces_runs() -> None:
    assert pinyin_key("Menghai 大益") == "menghai da yi"
    assert pinyin_key("2025 冰岛老寨 生普洱 茶饼") == "2025 bing dao lao zhai sheng pu er cha bing"
    assert pinyin_key("Man Gang Village \"黄片\" Ripe") == 'man gang village " huang pian " ripe'
    # Runs glued to Latin text still get a word boundary.
    assert pinyin_key("2001 Big Red Mark甲级") == "2001 big red mark jia ji"


def test_latin_only_is_untouched_by_pinyin_text() -> None:
    title = '2019 Xiaguan "Holy Flame" Baoyan Sheng / Raw Puerh Tea'
    assert not has_cjk(title)
    assert pinyin_text(title) == title


@pytest.mark.parametrize(
    "variants",
    [
        ("Lao Ban Zhang", "Laobanzhang", "老班章", "LAO-BAN-ZHANG"),
        ("Da Hong Pao", "Dahongpao", "大红袍"),
        ("Jin Jia Qi", "Jinjiaqi", "金家棋"),
        ("Pu-erh", "Pu'erh", "Puerh"),
        ("Bing Dao Lao Zhai", "Bingdao Lao Zhai", "冰岛老寨"),
    ],
)
def test_skeleton_is_space_punct_and_script_insensitive(variants: tuple[str, ...]) -> None:
    assert len({skeleton_key(v) for v in variants}) == 1


def test_skeleton_keeps_distinguishing_tokens() -> None:
    assert skeleton_key("2023 大益 7262 熟饼") != skeleton_key("2023 大益 7572 熟饼")
    assert skeleton_key("Xiaguan Tea Factory") != skeleton_key("Xinghai Tea Factory")


def test_short_skeletons_are_not_usable() -> None:
    assert usable_skeleton("FT") is None
    assert usable_skeleton("W2T") == "w2t"
    assert usable_skeleton("???") is None


def test_guard_still_reads_style_from_the_raw_name() -> None:
    # 熟 transliterates to "shu", which is NOT the "shou" style marker — the
    # attribute guard must keep parsing the raw title (it does: 生 / 熟).
    assert pinyin_key("熟饼") == "shu bing"
    assert parse_name_attributes("2023 大益 7572 熟饼").style is TeaStyle.SHOU
