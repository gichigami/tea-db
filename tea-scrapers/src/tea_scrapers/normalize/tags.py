"""Shopify tag parsing.

Yunnan Sourcing (.us and .com) emits structured Shopify tags shaped like
``"Producer_Yunnan Sourcing Brand Tea"`` or ``"Region_Yunnan"`` — a
``Key_Value`` convention we exploit to pull producer / region / harvest-season
hints directly out of the payload. white2tea and Crimson Lotus emit
free-form tags (``"2025"``, ``"Sheng / Raw Puerh"``, ``"Single Origin"``); for
those we fall back to title-regex heuristics in :mod:`shopify_mapper`.

This module is pure data — no DB, no LLM, no I/O. It runs at normalize time
(silver-side), never at scrape time (per spec §11 anti-pattern: scrape-time
filtering / parsing).
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field


# A YS-style structured tag looks like "Key_Value" — exactly one underscore
# *as separator* between a known key prefix and a free-form value. We anchor
# on the key prefix (not "anything before the first underscore") because some
# legitimate values themselves contain underscores or extra spaces (e.g.
# ``"Price per gram_ $0.15-$0.199/g"`` — note the space after the underscore).
_KNOWN_KEYS: tuple[str, ...] = (
    "Producer",
    "Region",
    "Sub-Region",
    "Country",
    "Harvest & Season",
    "Pu-erh Harvest Season",
    "Storage Type",
    "Shape",
    "Cultivar",
    "Certified Organic",
    "Price per gram",
)

# Anchor on a known key followed by an underscore, then everything else.
_KEY_VALUE_RE = re.compile(
    r"^(?P<key>" + "|".join(re.escape(k) for k in _KNOWN_KEYS) + r")_(?P<value>.+)$"
)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _clean_value(value: str) -> str:
    """Strip and collapse whitespace inside a tag value.

    Some YS tags have a leading space after the underscore (``"Price per
    gram_ $0.15-$0.199/g"``) — we treat that as cosmetic and trim it.
    """
    return re.sub(r"\s+", " ", value).strip()


@dataclass
class ParsedTags:
    """Result of parsing a Shopify ``tags`` list.

    Keys are lowercased + space-collapsed; values are NFC-normalized and
    trimmed but otherwise verbatim from the payload. A single tag key can
    legitimately repeat across tags (a product tagged with both
    ``Region_Yunnan`` and ``Region_Fujian`` is rare but valid); we keep a
    list per key so downstream callers can pick the first / pick by rule.
    """

    structured: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    unstructured: list[str] = field(default_factory=list)

    def first(self, key: str) -> str | None:
        bucket = self.structured.get(key.lower())
        return bucket[0] if bucket else None


def parse_tags(tags: Iterable[str] | None) -> ParsedTags:
    """Bucket Shopify tags into structured ``Key_Value`` hits + raw fallbacks.

    Unknown-key tags (no recognized prefix) flow to ``unstructured`` so the
    fallback heuristics in :mod:`shopify_mapper` can still see them. Empty /
    whitespace-only tags are silently dropped.
    """
    out = ParsedTags()
    if not tags:
        return out
    for raw in tags:
        if not isinstance(raw, str):
            # Defensive — Shopify schemas have been seen to nest weird types
            # in `tags` historically. Skip rather than blow up the run.
            continue
        normalized = _nfc(raw).strip()
        if not normalized:
            continue
        m = _KEY_VALUE_RE.match(normalized)
        if m is None:
            out.unstructured.append(normalized)
            continue
        key = m.group("key").lower()
        value = _clean_value(m.group("value"))
        if value:
            out.structured[key].append(value)
        else:
            out.unstructured.append(normalized)
    return out


__all__ = ["ParsedTags", "parse_tags"]
