"""Unit tests for :mod:`tea_scrapers.normalize.shopify_mapper`.

These are pure-Python tests over the mapping layer — no DB, no LLM. The
mapper turns a Shopify ``payload`` dict into one ``ProductFields`` per
weight variant, plus producer / region / harvest-year / tea-type hints.
"""

from __future__ import annotations

from typing import Any

from tea_scrapers.normalize.shopify_mapper import (
    map_payload_to_products,
    normalize_name,
)


def _payload(
    *,
    pid: int = 999,
    title: str = "Tea",
    vendor: str = "Some Vendor",
    product_type: str = "Raw Pu-erh Tea",
    tags: list[str] | None = None,
    variants: list[dict[str, Any]] | None = None,
    handle: str = "tea",
    body_html: str = "<p>desc</p>",
) -> dict[str, Any]:
    return {
        "id": pid,
        "title": title,
        "vendor": vendor,
        "product_type": product_type,
        "tags": tags or [],
        "handle": handle,
        "body_html": body_html,
        "variants": variants
        or [
            {
                "id": 1,
                "option1": "100 Grams",
                "title": "100 Grams",
                "grams": 125,
                "available": True,
                "price": "17.00",
            }
        ],
    }


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


def test_normalize_name_lower_collapse_ws() -> None:
    assert normalize_name("  Yi Wu  Spring   ") == "yi wu spring"


def test_normalize_name_nfc_preserves_hanzi() -> None:
    # Two visually equivalent NFC / NFD forms collapse to the same string.
    import unicodedata

    nfd = unicodedata.normalize("NFD", "白茶")
    assert normalize_name(nfd) == normalize_name("白茶")


# ---------------------------------------------------------------------------
# weight extraction
# ---------------------------------------------------------------------------


def test_option1_overrides_variant_grams() -> None:
    """option1='100 Grams' wins even when variant.grams=125 (packaging tare).

    This is the YS-US golden record's canonical disagreement (see
    ``tests/fixtures/golden/yunnan_sourcing_us.jsonl`` first record);
    documents the rule that the mapper never trusts ``variant.grams``.
    """
    fields_list = map_payload_to_products(
        _payload(
            variants=[
                {
                    "id": 1,
                    "option1": "100 Grams",
                    "title": "100 Grams",
                    "grams": 125,  # tare-inflated; must be ignored
                    "available": True,
                    "price": "17.00",
                }
            ]
        )
    )
    assert len(fields_list) == 1
    assert fields_list[0].weight_grams == 100


def test_kilogram_option_parses_to_grams() -> None:
    fields_list = map_payload_to_products(
        _payload(
            variants=[
                {
                    "id": 1,
                    "option1": "1 Kilogram",
                    "title": "1 Kilogram",
                    "grams": 1250,
                    "available": True,
                    "price": "80.00",
                }
            ]
        )
    )
    assert fields_list[0].weight_grams == 1000


def test_parenthesized_cake_weight() -> None:
    """option1='1 Cake (357 Grams)' picks the parenthesized weight."""
    fields_list = map_payload_to_products(
        _payload(
            variants=[
                {
                    "id": 2,
                    "option1": "1 Cake (357 Grams)",
                    "title": "1 Cake (357 Grams)",
                    "grams": 390,
                    "available": True,
                    "price": "75.00",
                }
            ]
        )
    )
    assert fields_list[0].weight_grams == 357


def test_multipack_option() -> None:
    """option1='6x 5g Dragon Balls' = 30 g per Crimson Lotus golden record."""
    fields_list = map_payload_to_products(
        _payload(
            variants=[
                {
                    "id": 3,
                    "option1": "6x 5g Dragon Balls",
                    "title": "6x 5g Dragon Balls",
                    "grams": 50,
                    "available": False,
                    "price": "12.00",
                }
            ]
        )
    )
    assert fields_list[0].weight_grams == 30


def test_title_weight_fallback_when_option1_unparseable() -> None:
    """Crimson Lotus 'Spellbound Stag' — option1='Default Title', title has '200g'."""
    fields_list = map_payload_to_products(
        _payload(
            title='2025 "The Spellbound Stag" Kunlu Shan Big Tree Bai Cha / White Tea - 200g Cake',
            variants=[
                {
                    "id": 4,
                    "option1": "Default Title",
                    "title": "Default Title",
                    "grams": 210,  # tare-inflated; must be ignored
                    "available": True,
                    "price": "39.99",
                }
            ],
        )
    )
    assert fields_list[0].weight_grams == 200


def test_unparseable_weight_returns_none_not_zero() -> None:
    """No weight in option1 or title → None; runner counts as unmappable."""
    fields_list = map_payload_to_products(
        _payload(
            title="Yixing Teapot",
            variants=[
                {
                    "id": 5,
                    "option1": "Default Title",
                    "title": "Default Title",
                    "grams": 0,
                    "available": True,
                    "price": "150.00",
                }
            ],
        )
    )
    assert fields_list[0].weight_grams is None


# ---------------------------------------------------------------------------
# harvest year
# ---------------------------------------------------------------------------


def test_harvest_year_from_start_of_title() -> None:
    fields_list = map_payload_to_products(_payload(title="2025 Yunnan Sourcing"))
    assert fields_list[0].harvest_year == 2025


def test_harvest_year_does_NOT_match_1990s_decade_phrase() -> None:
    """Anchored regex must reject '1990s aged blend' — that's a decade, not a year."""
    fields_list = map_payload_to_products(_payload(title="1990s shou aged blend"))
    assert fields_list[0].harvest_year is None


def test_harvest_year_from_ys_tag_when_title_has_none() -> None:
    """YS encodes the year as 'Harvest & Season_Autumn 2025' / 'Pu-erh Harvest Season_Spring 2026'."""
    fields_list = map_payload_to_products(
        _payload(
            title="Yunnan Sourcing Mini Cakes",
            tags=["Harvest & Season_Autumn 2025"],
        )
    )
    assert fields_list[0].harvest_year == 2025


def test_harvest_year_tag_overrides_title_year() -> None:
    """Tag is structured / authoritative; if both present we still prefer the tag."""
    fields_list = map_payload_to_products(
        _payload(
            title="2010 Some Aged Sheng",
            tags=["Harvest & Season_Autumn 2025"],
        )
    )
    # Mapper consults the tag first.
    assert fields_list[0].harvest_year == 2025


# ---------------------------------------------------------------------------
# producer
# ---------------------------------------------------------------------------


def test_producer_tag_wins_over_vendor_field() -> None:
    fields_list = map_payload_to_products(
        _payload(
            vendor="Yunnan Sourcing Brand Pu-erh",
            tags=["Producer_Yunnan Sourcing Brand Tea"],
        )
    )
    producer = fields_list[0].producer_hint
    assert producer is not None
    assert producer.name == "Yunnan Sourcing Brand Tea"
    assert producer.source == "tag"


def test_producer_falls_back_to_vendor_field() -> None:
    fields_list = map_payload_to_products(
        _payload(vendor="Crimson Lotus Tea", tags=["2025", "Single Origin"])
    )
    producer = fields_list[0].producer_hint
    assert producer is not None
    assert producer.name == "Crimson Lotus Tea"
    assert producer.source == "vendor_field"


def test_producer_none_when_vendor_field_blank() -> None:
    fields_list = map_payload_to_products(_payload(vendor="", tags=[]))
    assert fields_list[0].producer_hint is None


# ---------------------------------------------------------------------------
# non-tea filter
# ---------------------------------------------------------------------------


def test_non_tea_product_type_flagged() -> None:
    fields_list = map_payload_to_products(
        _payload(product_type="Non-Tea", tags=["Non-Tea"])
    )
    assert fields_list[0].is_non_tea is True


def test_tea_product_type_NOT_flagged() -> None:
    fields_list = map_payload_to_products(_payload(product_type="Raw Pu-erh Tea"))
    assert fields_list[0].is_non_tea is False


# ---------------------------------------------------------------------------
# multi-variant fan-out
# ---------------------------------------------------------------------------


def test_multi_variant_product_emits_one_fields_per_variant() -> None:
    """YS 'Year of the Horse' cake has 5 weight variants → 5 ProductFields."""
    variants = [
        {"id": 11, "option1": "50 Grams", "title": "50 Grams", "grams": 75, "available": True, "price": "6.25"},
        {"id": 12, "option1": "100 Grams", "title": "100 Grams", "grams": 130, "available": True, "price": "11.50"},
        {"id": 13, "option1": "250 Grams", "title": "250 Grams", "grams": 300, "available": True, "price": "25.00"},
        {"id": 14, "option1": "500 Grams", "title": "500 Grams", "grams": 625, "available": True, "price": "45.00"},
        {"id": 15, "option1": "1 Kilogram", "title": "1 Kilogram", "grams": 1250, "available": True, "price": "80.00"},
    ]
    fields_list = map_payload_to_products(
        _payload(
            pid=4242,
            title='2026 Yunnan Sourcing "Year of the Horse" Ripe Pu-erh Tea Mini Cake',
            tags=["Producer_Yunnan Sourcing Brand Tea", "Region_Yunnan"],
            variants=variants,
        )
    )
    assert len(fields_list) == 5
    weights = [f.weight_grams for f in fields_list]
    assert weights == [50, 100, 250, 500, 1000]

    # All share the product-level fields.
    first = fields_list[0]
    assert all(f.shopify_product_id == "4242" for f in fields_list)
    assert all(f.canonical_name == first.canonical_name for f in fields_list)
    assert all(f.producer_hint == first.producer_hint for f in fields_list)
    assert all(f.region_hint == first.region_hint for f in fields_list)

    # Variants differ.
    variant_ids = [f.variant.shopify_variant_id for f in fields_list]
    assert variant_ids == ["11", "12", "13", "14", "15"]


# ---------------------------------------------------------------------------
# region / vendor_url / price
# ---------------------------------------------------------------------------


def test_region_yunnan_tag_maps_to_china_yunnan() -> None:
    fields_list = map_payload_to_products(_payload(tags=["Region_Yunnan"]))
    region = fields_list[0].region_hint
    assert region is not None
    assert region.country == "China"
    assert region.province == "Yunnan"


def test_no_region_tag_means_no_region_hint() -> None:
    fields_list = map_payload_to_products(_payload(tags=["2025"]))
    assert fields_list[0].region_hint is None


def test_vendor_url_uses_handle_and_base_url() -> None:
    fields_list = map_payload_to_products(
        _payload(handle="some-tea"),
        vendor_base_url="https://yunnansourcing.us",
    )
    assert fields_list[0].vendor_url == "https://yunnansourcing.us/products/some-tea"


def test_price_parsed_to_cents() -> None:
    fields_list = map_payload_to_products(
        _payload(
            variants=[
                {
                    "id": 1,
                    "option1": "100 Grams",
                    "title": "100 Grams",
                    "grams": 125,
                    "available": True,
                    "price": "17.50",
                }
            ]
        )
    )
    assert fields_list[0].variant.price_cents == 1750
    assert fields_list[0].variant.currency == "USD"


def test_blank_title_returns_one_unmappable_fields() -> None:
    """Pathological no-title payloads return a single fields with empty name."""
    fields_list = map_payload_to_products(_payload(title=""))
    assert len(fields_list) == 1
    assert fields_list[0].canonical_name == ""


def test_no_variants_returns_one_sentinel_unmappable() -> None:
    payload = _payload(variants=[])
    payload["variants"] = []
    fields_list = map_payload_to_products(payload)
    assert len(fields_list) == 1
    assert fields_list[0].weight_grams is None
    assert fields_list[0].variant.shopify_variant_id == ""
