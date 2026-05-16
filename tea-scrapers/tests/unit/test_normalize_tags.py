"""Unit tests for :mod:`tea_scrapers.normalize.tags`.

The parser is two-mode:
- Known YS structured ``Key_Value`` prefixes → ``structured`` bucket.
- Anything else → ``unstructured`` bucket (for heuristic-fallback callers
  to inspect).
"""

from __future__ import annotations

from tea_scrapers.normalize.tags import parse_tags


def test_structured_key_value_tag_buckets_to_structured() -> None:
    out = parse_tags(
        [
            "Producer_Yunnan Sourcing Brand Tea",
            "Region_Yunnan",
            "Pu-erh Harvest Season_Spring Harvest",
        ]
    )
    assert out.structured["producer"] == ["Yunnan Sourcing Brand Tea"]
    assert out.structured["region"] == ["Yunnan"]
    assert out.structured["pu-erh harvest season"] == ["Spring Harvest"]
    assert out.unstructured == []


def test_unstructured_tag_buckets_to_unstructured() -> None:
    out = parse_tags(["Non-Tea", "2025", "Sheng / Raw Puerh", "Single Origin"])
    assert out.structured == {}
    assert out.unstructured == ["Non-Tea", "2025", "Sheng / Raw Puerh", "Single Origin"]


def test_first_returns_none_for_missing_key() -> None:
    out = parse_tags(["Region_Yunnan"])
    assert out.first("producer") is None
    assert out.first("region") == "Yunnan"


def test_leading_space_in_tag_value_is_trimmed() -> None:
    """YS sometimes encodes a leading space after the underscore."""
    out = parse_tags(["Price per gram_ $0.15-$0.199/g"])
    assert out.structured["price per gram"] == ["$0.15-$0.199/g"]


def test_empty_and_none_inputs_safe() -> None:
    assert parse_tags(None).structured == {}
    assert parse_tags([]).structured == {}
    assert parse_tags(["", "  "]).structured == {}
    assert parse_tags(["", "  "]).unstructured == []


def test_non_string_values_are_skipped() -> None:
    # Defensive — Shopify schemas have nested odd types historically.
    out = parse_tags(["Region_Yunnan", 42, None, {"weird": "shape"}, "Single Origin"])  # type: ignore[list-item]
    assert out.structured["region"] == ["Yunnan"]
    assert out.unstructured == ["Single Origin"]


def test_repeated_key_keeps_both_values() -> None:
    """A product tagged twice for the same key keeps both values in order."""
    out = parse_tags(["Region_Yunnan", "Region_Fujian"])
    assert out.structured["region"] == ["Yunnan", "Fujian"]


def test_underscored_unknown_prefix_falls_through_to_unstructured() -> None:
    """An unknown prefix with an underscore is NOT silently structured."""
    out = parse_tags(["MysteryKey_some value", "Region_Yunnan"])
    assert out.structured == {"region": ["Yunnan"]}
    assert out.unstructured == ["MysteryKey_some value"]
