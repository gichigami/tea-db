"""Unit tests for the silver ``description_hash`` recipe.

The hash is computed inside :mod:`tea_scrapers.normalize.shopify_mapper`
via :func:`_description_hash`. We exercise it via the public mapper
entrypoint so tests don't depend on private surface.
"""

from __future__ import annotations

import unicodedata

from tea_scrapers.normalize.shopify_mapper import map_payload_to_products


def _hash(payload: dict) -> str:
    return map_payload_to_products(payload)[0].description_hash


def _base_payload() -> dict:
    return {
        "id": 1,
        "title": "2025 Some Tea",
        "vendor": "X",
        "product_type": "Raw Pu-erh Tea",
        "body_html": "<p>Tasting notes here.</p>",
        "tags": ["Region_Yunnan", "Producer_Foo"],
        "handle": "x",
        "variants": [
            {
                "id": 9,
                "option1": "100 Grams",
                "title": "100 Grams",
                "grams": 125,
                "available": True,
                "price": "10.00",
            }
        ],
    }


def test_same_payload_different_tag_order_same_hash() -> None:
    a = _base_payload()
    b = _base_payload()
    b["tags"] = list(reversed(a["tags"]))
    assert _hash(a) == _hash(b)


def test_case_change_in_title_changes_hash() -> None:
    """Case is content per the recipe — flipping case is a description change."""
    a = _base_payload()
    b = _base_payload()
    b["title"] = b["title"].lower()
    assert _hash(a) != _hash(b)


def test_body_html_change_changes_hash() -> None:
    a = _base_payload()
    b = _base_payload()
    b["body_html"] = b["body_html"] + " More text."
    assert _hash(a) != _hash(b)


def test_nfc_equivalence_collapses_to_one_hash() -> None:
    """NFD and NFC forms of the same Unicode string produce the same hash."""
    a = _base_payload()
    a["title"] = unicodedata.normalize("NFC", "白茶 寿眉")
    b = _base_payload()
    b["title"] = unicodedata.normalize("NFD", "白茶 寿眉")
    assert _hash(a) == _hash(b)


def test_unrelated_field_change_does_NOT_change_hash() -> None:
    """Description hash only covers (title, body_html, tags) — other fields irrelevant."""
    a = _base_payload()
    b = _base_payload()
    b["variants"][0]["price"] = "99.00"  # not in the projection
    b["vendor"] = "Different Vendor"
    assert _hash(a) == _hash(b)


def test_is_hex_sha256() -> None:
    h = _hash(_base_payload())
    assert len(h) == 64
    int(h, 16)
