"""Unit tests for :mod:`tea_scrapers.normalize.attributes` (ROADMAP H.2).

Pure-Python — no DB. Names are real titles from the golden JSONL fixtures
and Shopify cassettes (survey 2026-10-31) unless marked synthetic.
"""

from __future__ import annotations

import re

import pytest

from tea_scrapers.normalize.attributes import (
    INVENTORY_SUFFIX_PATTERN,
    ONE_SIDED_POLICY,
    GuardAttributes,
    OneSidedPolicy,
    TeaStyle,
    check_attribute_guard,
    has_inventory_suffix,
    matching_key,
    parse_name_attributes,
    strip_inventory_suffix,
)
from tea_scrapers.normalize.shopify_mapper import normalize_name

# ---------------------------------------------------------------------------
# parse_name_attributes — the three tricky names from the H.2 brief
# ---------------------------------------------------------------------------


def test_year_of_the_horse_ripe_mini_cake() -> None:
    a = parse_name_attributes(
        "2026 Yunnan Sourcing \"Year of the Horse\" Ripe Pu-erh Tea Mini Cake"
    )
    assert a.vintage_year == 2026
    assert a.style is TeaStyle.SHOU
    assert a.recipe_codes == frozenset()  # 2026 is a year, not a code
    assert a.weight_grams is None


def test_three_cranes_1815_recipe_is_a_code_not_a_year() -> None:
    a = parse_name_attributes('2020 Three Cranes "1815 Recipe" Liu Bao Tea Brick')
    assert a.vintage_year == 2020
    assert a.recipe_codes == frozenset({"1815"})
    assert a.style is None  # Liu Bao, no sheng/shou marker


def test_cha_gao_instant_ripe_resin() -> None:
    a = parse_name_attributes('2009 Jinggu "Cha Gao" Instant Ripe Pu-erh Tea Resin')
    assert a.vintage_year == 2009
    assert a.style is TeaStyle.SHOU
    assert a.recipe_codes == frozenset()


# ---------------------------------------------------------------------------
# Recipe codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "codes", "year"),
    [
        ('2003 CNNP "7262" Ripe Pu-erh Tea Cake', {"7262"}, 2003),
        ('1998 CNNP "7262 Yellow Mark" Ripe Pu-erh Tea Cake', {"7262"}, 1998),
        ('2019 Menghai "7452" Ripe Pu-erh Tea Cake', {"7452"}, 2019),
        ("2012 Menghai Tea Factory 7542 Recipe Raw Pu-erh Tea Cake", {"7542"}, 2012),
        ('2000 "7562 Recipe" Aged Ripe Pu-erh Tea Brick', {"7562"}, 2000),
        ("2003 Tian Di Ren “7572” Ripe Pu-erh Tea Cake", {"7572"}, 2003),
        ("2003 CNNP 7581 Aged Ripe Pu-erh Tea Brick", {"7581"}, 2003),
        ('2023 Xiaguan "7623 Xiao Fa" Ripe Pu-erh Tea Coins in Tin', {"7623"}, 2023),
        ('Three Cranes "2503" Premium Liu Bao Tea in Basket', {"2503"}, None),
        ("2005 Liming 7540 Raw Pu-erh Tea Cake", {"7540"}, 2005),
        ("2010 Menghai 8582 Raw Pu-erh Tea Cake", {"8582"}, 2010),  # synthetic
    ],
)
def test_recipe_codes_from_real_names(name: str, codes: set[str], year: int | None) -> None:
    a = parse_name_attributes(name)
    assert a.recipe_codes == frozenset(codes)
    assert a.vintage_year == year


@pytest.mark.parametrize(
    "name",
    [
        # Years are never codes (incl. a non-leading second year-like token).
        "2015 Colbert Holland 1945",
        "Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler for 2024 - Part 5",
        '"Purple Voodoo" Purple Black Tea Cake * Spring 2025',
        # Quantities with units are never codes.
        'Pure Silver 999 "Dragon" Boiler Kettle * 1100ml',
        "2019 Bulk 1000g Ripe Pu-erh Brick",  # synthetic
        "2019 Bulk 1000 grams Ripe Pu-erh Brick",  # synthetic
        # 3-digit tokens are never codes.
        '1998 Raw Basket Aged "133" Liu Bao Tea',
        '"519 Shanghai" Memorial Sheng / Raw Puerh Tea',
        '2022 Zu Xiang "Wu Liang Chuan Qi 888" Certified Organic Raw Pu-erh Tea',
        # Longer digit runs (ids) are never codes.
        "Some Tea 1234567890",  # synthetic
    ],
)
def test_non_codes(name: str) -> None:
    assert parse_name_attributes(name).recipe_codes == frozenset()


def test_explicit_recipe_marker_wins_over_year_range() -> None:
    # Synthetic: a year-shaped number explicitly labelled as a recipe.
    a = parse_name_attributes('2015 Factory "2001 Recipe" Ripe Brick')
    assert a.recipe_codes == frozenset({"2001"})
    assert a.vintage_year == 2015


def test_first_year_is_vintage_second_year_like_token_ignored() -> None:
    assert parse_name_attributes("2015 Colbert Holland 1945").vintage_year == 2015
    assert (
        parse_name_attributes('"Shou Girls" a 2025 Shou / Ripe Puerh Tea Blend').vintage_year
        == 2025
    )


# ---------------------------------------------------------------------------
# Sheng / shou
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "style"),
    [
        ('2025 "Mountain Shadow" Shou / Ripe Puerh Tea - 200g Cake', TeaStyle.SHOU),
        ('"Gourdsmasher" Sheng / Raw Puerh Tea Dragon Balls :: Seattle Inventory', TeaStyle.SHENG),
        ("2006 Changtai \"Si Pu Yuan\" Menghai Raw Pu-erh Tea", TeaStyle.SHENG),
        ("200X Failcorp Shou", TeaStyle.SHOU),
        ("2025 Raw Autumn Liubao", TeaStyle.SHENG),
        ("FT 28 Years Aged \"Li Li Xiang\" Ripe Loose Leaf Pu-erh Tea", TeaStyle.SHOU),
        ("2024 Yiwu \"Shousparilla\" Lao Cha Tou Shou Puerh Tea", TeaStyle.SHOU),
        # Both markers → MIXED (combo packs; YS "Ripe Raw" titles).
        ('"Simple Sheng" and "Simple Shou" Combo Pack', TeaStyle.MIXED),
        ('2024 Yunnan Sourcing "Subduer of Dragons" Ripe Raw Pu-erh Tea Cake', TeaStyle.MIXED),
        # CJK markers (synthetic).
        ("2018 老班章 生普 357g", TeaStyle.SHENG),
        ("2018 勐海 熟普 357g", TeaStyle.SHOU),
        # No marker.
        ("Feng Qing \"Old Plantation\" Dian Hong Black Tea", None),
        ("2025 Steam Ship", None),
    ],
)
def test_style(name: str, style: TeaStyle | None) -> None:
    assert parse_name_attributes(name).style is style


@pytest.mark.parametrize(
    "name",
    [
        # 野生 "wild" — not sheng.
        '2016 Yunnan Sourcing "Dehong Ye Sheng Cha" Wild Tree Purple Tea Cake',
        '2002 Jiang Cheng "Ye Sheng Gu Cha" Aged Pu-erh Tea Cake',
        # 寿眉 white tea — not shou.
        "Fuding Shou Mei White Tea in King Orange",
        "Shou Mei White Tea with Aged Tangerine Peel Dragon Balls",
        # Potter / brand names on teaware.
        'Jian Shui Clay "Fish" Teapot by Zhang Quan Sheng',
        'Jian Shui Clay "Qiu Yi" Teapot by Huang Shou Zhen',
        'Jian Shui Clay "Guan Ren Sheng" Teapot by Xiao Xi Cao Tang',
    ],
)
def test_style_false_positives_suppressed(name: str) -> None:
    assert parse_name_attributes(name).style is None


def test_ye_sheng_does_not_mask_a_real_marker() -> None:
    a = parse_name_attributes('2002 Jiang Cheng "Ye Sheng Gu Cha" Aged Raw Pu-erh Tea Cake')
    assert a.style is TeaStyle.SHENG  # "Raw" still counts


# ---------------------------------------------------------------------------
# Weight (delegates to the mapper grammar; informational only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "grams"),
    [
        ('2025 "Mountain Shadow" Shou / Ripe Puerh Tea - 200g Cake', 200),
        ('2003 Changtai "Ji Nian / Memorial" Sheng / Raw Tuo Cha (100g) :: Seattle Inventory', 100),
        ('2014 Yiwu "Iron Forge" Huang Pian Shou Puerh Tea (250 grams) :: Seattle Inventory', 250),
        ("Puerhiodic Table of the Elements 2019 - 6x Sheng Puerh Tea Dragon Balls", None),
        ("2019 Bulk 1kg Ripe Brick", 1000),  # synthetic
        ("2026 Yunnan Sourcing \"Year of the Horse\" Ripe Pu-erh Tea Mini Cake", None),
    ],
)
def test_weight(name: str, grams: int | None) -> None:
    assert parse_name_attributes(name).weight_grams == grams


def test_empty_and_none_names() -> None:
    for n in (None, ""):
        a = parse_name_attributes(n)
        assert (a.vintage_year, a.weight_grams, a.recipe_codes, a.style) == (
            None,
            None,
            frozenset(),
            None,
        )


# ---------------------------------------------------------------------------
# Inventory suffix (matching key only)
# ---------------------------------------------------------------------------


def test_seattle_suffix_stripped_to_base_listing() -> None:
    suffixed = '2025 "Mountain Shadow" Shou / Ripe Puerh Tea - 200g Cake :: Seattle Inventory'
    base = '2025 "Mountain Shadow" Shou / Ripe Puerh Tea - 200g Cake'
    assert has_inventory_suffix(suffixed)
    assert not has_inventory_suffix(base)
    assert strip_inventory_suffix(suffixed) == base
    assert matching_key(suffixed) == matching_key(base) == normalize_name(base)


def test_only_trailing_inventory_segment_is_stripped() -> None:
    # The first " :: " segment is part of the product name.
    name = (
        '2025 "Dirty Dozen" 12th Anniversary Tuo Cha :: Sheng / Raw Puerh Tea '
        ":: Seattle Inventory"
    )
    assert strip_inventory_suffix(name) == (
        '2025 "Dirty Dozen" 12th Anniversary Tuo Cha :: Sheng / Raw Puerh Tea'
    )
    assert strip_inventory_suffix(strip_inventory_suffix(name)) == strip_inventory_suffix(name)


def test_suffix_variants_whitespace_and_case() -> None:
    # Real: double space before "::" ("2023 Bangdong Old Tree ...").
    assert (
        strip_inventory_suffix("2023 Bangdong Old Tree Sheng / Raw Puerh Tea  :: Seattle Inventory")
        == "2023 Bangdong Old Tree Sheng / Raw Puerh Tea"
    )
    assert strip_inventory_suffix("X ::seattle   INVENTORY ") == "X"


@pytest.mark.parametrize(
    "name",
    [
        # Identity-bearing suffixes from YS — must NOT be stripped.
        '"Purple Voodoo" Purple Black Tea Cake * Spring 2025',
        "Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler for 2024 - Part 5",
        "Seattle Inventory Sampler",  # synthetic: not a trailing ":: " segment
    ],
)
def test_non_inventory_suffixes_untouched(name: str) -> None:
    assert strip_inventory_suffix(name) == name


def test_suffix_pattern_is_postgres_compatible_subset() -> None:
    # The same string is passed to Postgres ``regexp_replace(..., 'i')``;
    # keep it to \s, literals, quantifiers and $ (no Python-only syntax).
    assert re.fullmatch(r"[\\sa-z:*+$]+", INVENTORY_SUFFIX_PATTERN)


def test_suffix_does_not_change_attributes() -> None:
    base = '2025 "Mountain Shadow" Shou / Ripe Puerh Tea - 200g Cake'
    assert parse_name_attributes(base + " :: Seattle Inventory") == parse_name_attributes(base)


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def _g(name: str, year: int | None = 2024, weight: int | None = 357) -> GuardAttributes:
    return GuardAttributes.from_name(name, harvest_year=year, weight_grams=weight)


def test_guard_style_mismatch_vetoes_real_ys_pair() -> None:
    v = check_attribute_guard(
        _g('2024 Yunnan Sourcing "Bang Wai Mountain" Raw Pu-erh Tea Cake'),
        _g('2024 Yunnan Sourcing "Bang Wai Mountain" Ripe Pu-erh Tea Cake'),
    )
    assert v.vetoed
    assert [c.reason for c in v.conflicts] == ["style:mismatch"]


def test_guard_recipe_mismatch_vetoes() -> None:
    v = check_attribute_guard(
        _g('2022 Menghai "7572" Ripe Pu-erh Tea Cake'),
        _g('2022 Menghai "7542" Ripe Pu-erh Tea Cake'),
    )
    assert [c.reason for c in v.conflicts] == ["recipe_code:mismatch"]


def test_guard_identical_attributes_pass() -> None:
    v = check_attribute_guard(
        _g('2024 Menghai "7572" Ripe Pu-erh Tea Cake'),
        _g('2024 Menghai 7572 Ripe Puerh Tea Cake'),
    )
    assert not v.vetoed


def test_guard_both_unknown_passes() -> None:
    assert not check_attribute_guard(_g("2025 Steam Ship"), _g("2025 Steam Ships")).vetoed


def test_guard_mixed_never_matches_single_style() -> None:
    v = check_attribute_guard(
        _g('"Simple Sheng" and "Simple Shou" Combo Pack', year=None, weight=None),
        _g('2017 "Simple Sheng" Sheng / Raw Puerh', year=None, weight=None),
    )
    assert [c.reason for c in v.conflicts] == ["style:mismatch"]


def test_guard_year_and_weight_compared_from_fields_not_name() -> None:
    # Name says 2025 but the mapper said 2024 for both sides → no conflict.
    a = GuardAttributes.from_name("2025 Foo Raw Cake", harvest_year=2024, weight_grams=200)
    b = GuardAttributes.from_name("2025 Foo Raw Cake", harvest_year=2024, weight_grams=200)
    assert not check_attribute_guard(a, b).vetoed
    c = GuardAttributes.from_name("2025 Foo Raw Cake", harvest_year=2024, weight_grams=100)
    assert [x.reason for x in check_attribute_guard(a, c).conflicts] == ["weight_grams:mismatch"]


def test_default_one_sided_policy_is_veto_for_every_attribute() -> None:
    assert set(ONE_SIDED_POLICY) == {"harvest_year", "weight_grams", "recipe_code", "style"}
    assert all(p is OneSidedPolicy.VETO for p in ONE_SIDED_POLICY.values())


@pytest.mark.parametrize(
    ("q", "c", "reason"),
    [
        ("2019 Menghai Classic Ripe Pu-erh Tea Cake", "2019 Menghai Classic Pu-erh Tea Cake", "style:one_sided"),
        ('2019 Menghai "7452" Ripe Pu-erh Tea Cake', "2019 Menghai Ripe Pu-erh Tea Cake", "recipe_code:one_sided"),
    ],
)
def test_one_sided_vetoes_by_default(q: str, c: str, reason: str) -> None:
    v = check_attribute_guard(_g(q), _g(c))
    assert [x.reason for x in v.conflicts] == [reason]
    # Symmetric.
    assert [x.reason for x in check_attribute_guard(_g(c), _g(q)).conflicts] == [reason]


def test_one_sided_pass_policy_defers() -> None:
    policy = {**ONE_SIDED_POLICY, "style": OneSidedPolicy.PASS, "recipe_code": OneSidedPolicy.PASS}
    v = check_attribute_guard(
        _g('2019 Menghai "7452" Ripe Pu-erh Tea Cake'),
        _g("2019 Menghai Pu-erh Tea Cake"),
        policy=policy,
    )
    assert not v.vetoed
    # A real mismatch still vetoes under PASS.
    v2 = check_attribute_guard(
        _g("2019 Menghai Raw Pu-erh Tea Cake"),
        _g("2019 Menghai Ripe Pu-erh Tea Cake"),
        policy=policy,
    )
    assert [x.reason for x in v2.conflicts] == ["style:mismatch"]


def test_one_sided_year_weight_veto_mirrors_v1_is_not_distinct_from() -> None:
    a = GuardAttributes.from_name("Foo", harvest_year=2024, weight_grams=None)
    b = GuardAttributes.from_name("Foo", harvest_year=None, weight_grams=None)
    assert [x.reason for x in check_attribute_guard(a, b).conflicts] == ["harvest_year:one_sided"]
