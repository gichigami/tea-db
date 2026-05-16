"""Pure mapping: Shopify ``payload`` → silver-shaped fields.

No DB, no LLM, no I/O — this module is fully unit-testable in isolation.
The runner in :mod:`tea_scrapers.normalize.silver` calls these functions
once per bronze row and hands the result to the canonical matcher.

Per the step-6 plan:
- **Primary path**: Yunnan Sourcing's ``Key_Value`` structured tags
  (handled in :mod:`tea_scrapers.normalize.tags`) drive producer / region /
  harvest-year extraction.
- **Fallback path**: white2tea / Crimson Lotus emit free-form tags;
  producer falls back to ``payload.vendor``, harvest-year to a strict
  start-of-title regex, weight to a parse of ``variant.option1``.

Field-extraction rules of the road (all from the step-6 brief):

- ``weight_grams`` is parsed from ``variant.option1`` / ``variant.title``,
  **never** from ``variant.grams``. The YS-US golden record shows
  ``"100 Grams"`` → ``grams=125`` because Shopify's ``grams`` field
  includes packaging tare. See ``test_normalize_shopify_mapper.py``.
- The harvest-year regex anchors at the start of the title:
  ``^(?:19|20)\\d\\d``. ``"1990s shou aged blend"`` must NOT match.
- Non-tea records (``product_type == "Non-Tea"``) are flagged so the
  silver runner can skip + count, rather than silently dropping.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from tea_scrapers.normalize.tags import ParsedTags, parse_tags

# Anchored at start — "2025 Yunnan Sourcing ..." matches, "1990s aged"
# does NOT. Per the brief.
_TITLE_YEAR_RE = re.compile(r"^(?P<year>(?:19|20)\d\d)\b")
# Title can mention a weight in either order: "200g Cake" or "Cake - 200g".
# Captures both "g" and "grams" (case-insensitive).
_TITLE_WEIGHT_RE = re.compile(
    r"(?P<num>\d{2,4})\s*(?:g|grams?)\b", re.IGNORECASE
)
# ``variant.option1`` shapes seen in the goldens:
#   "100 Grams" / "50 grams" / "1 Kilogram" / "1 Cake (357 Grams)" /
#   "200g \"Mountain Shadow\" Cake" / "6x 5g Dragon Balls" /
#   "1x 100g \"Dirty Dozen\" Tuo" / "Default Title" / "Winter 2024".
# Strategy: first look for a parenthesized "(N Grams/Kilogram)"; then look
# for "Nx Mg" / "Nx Mgrams" multi-pack ("6x 5g" → 30); then a plain "N g".
_OPTION_PAREN_RE = re.compile(
    r"\((?P<num>\d+(?:\.\d+)?)\s*(?P<unit>g|grams?|kg|kilograms?)\)",
    re.IGNORECASE,
)
_OPTION_MULTIPACK_RE = re.compile(
    r"(?P<count>\d+)\s*x\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>g|grams?|kg|kilograms?)\b",
    re.IGNORECASE,
)
_OPTION_PLAIN_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>g|grams?|kg|kilograms?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProducerHint:
    """Producer name candidate + provenance for matcher logging."""

    name: str
    source: str  # "tag" | "vendor_field" | None for upstream debugging


@dataclass(frozen=True)
class RegionHint:
    """Region candidates pulled from YS structured tags.

    All fields free-form Text; the canonical-matcher composes them into a
    ``(country, province, county, mountain, village)`` row. V1 only fills
    ``country`` + ``province`` (the latter rough-grained — "Yunnan", "Fujian").
    Sub-Region tag values aren't currently mapped to the schema's nested
    region fields because the parse is ambiguous ("Menghai (Yunnan)" vs.
    "Feng Qing County (Lincang)"); ml-engineer owns that in V1.5.
    """

    country: str | None = None
    province: str | None = None
    county: str | None = None
    mountain: str | None = None
    village: str | None = None


@dataclass(frozen=True)
class VariantFields:
    """One Shopify variant unpacked into silver-shape."""

    shopify_variant_id: str
    weight_grams: int | None
    available: bool
    price_cents: int | None
    currency: str  # V1 hardcode "USD" — see §12 OQ.


@dataclass(frozen=True)
class ProductFields:
    """Full mapped result for one ``(product, variant)`` row pair.

    The silver runner will call the canonical matcher with the
    ``(producer_hint, harvest_year, canonical_name, weight_grams)`` tuple
    derived from this. Multi-variant products produce N ``ProductFields``
    each — one per (product, variant) pair — sharing everything except
    ``variant`` and ``weight_grams``.
    """

    shopify_product_id: str
    canonical_name: str  # NFC-normalized; case preserved
    normalized_name: str  # lower + collapsed whitespace, for exact-match step
    producer_hint: ProducerHint | None
    region_hint: RegionHint | None
    harvest_year: int | None
    tea_type: str | None
    tea_style: str | None  # V1: None — left for ml-engineer V1.5.
    format: str | None  # cake / brick / loose / etc. — V1: None.
    cultivar: str | None  # V1: None.
    weight_grams: int | None
    variant: VariantFields
    vendor_url: str | None
    description_hash: str
    # Silver-side filter flags — the runner uses these for stats bucketing.
    is_non_tea: bool


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def normalize_name(canonical_name: str) -> str:
    """Lower + NFC + collapse whitespace.

    The 4-tuple matcher key (producer, year, normalized_name, weight_grams)
    uses this to absorb pure-presentation differences ("Yi Wu " vs "yi wu")
    without going through trigram. Punctuation is preserved — quoted product
    names like ``"Year of the Horse"`` collide hard if we strip quotes.
    """
    return _collapse_ws(_nfc(canonical_name).lower())


def _parse_weight_from_option(option_text: str | None) -> int | None:
    """Parse weight in grams from a Shopify variant option string.

    Order: parenthesized "(N g/grams/kg/kilograms)" first (most explicit),
    then "Nx Mg/grams/kg" multipack, then plain "N g/grams/kg". Returns
    ``None`` if nothing parseable.
    """
    if not option_text:
        return None
    text = _nfc(option_text)

    m = _OPTION_PAREN_RE.search(text)
    if m is not None:
        return _to_grams(float(m.group("num")), m.group("unit"))

    m = _OPTION_MULTIPACK_RE.search(text)
    if m is not None:
        count = int(m.group("count"))
        per = float(m.group("num"))
        return _to_grams(count * per, m.group("unit"))

    m = _OPTION_PLAIN_RE.search(text)
    if m is not None:
        return _to_grams(float(m.group("num")), m.group("unit"))

    return None


def _to_grams(num: float, unit: str) -> int:
    u = unit.lower()
    if u in ("kg", "kilogram", "kilograms"):
        return int(round(num * 1000))
    return int(round(num))


def _parse_weight_from_title(title: str) -> int | None:
    """Fallback: parse weight from anywhere in the product title."""
    m = _TITLE_WEIGHT_RE.search(_nfc(title))
    if m is None:
        return None
    return int(m.group("num"))


def _parse_harvest_year(title: str, tags: ParsedTags) -> int | None:
    """Extract a 4-digit harvest year.

    YS tags sometimes encode the year as ``Pu-erh Harvest Season_Spring 2026``
    or ``Harvest & Season_Autumn 2025``; we look there first. Otherwise we
    fall back to the strict start-of-title regex (per the brief: "1990s
    aged blend" must NOT match).
    """
    for key in ("harvest & season", "pu-erh harvest season"):
        v = tags.first(key)
        if v is None:
            continue
        m = re.search(r"(19|20)\d\d", v)
        if m is not None:
            return int(m.group(0))

    m = _TITLE_YEAR_RE.match(_nfc(title))
    if m is None:
        return None
    return int(m.group("year"))


def _producer_from_payload(
    payload: dict[str, Any], tags: ParsedTags
) -> ProducerHint | None:
    """YS structured tag wins; otherwise ``payload.vendor`` field.

    Both are noisy:
    - YS-US "Year of the Horse" carries ``Producer_Yunnan Sourcing Brand Tea``
      via the structured tag.
    - Crimson Lotus / white2tea omit ``Producer_*`` tags entirely; their
      Shopify ``vendor`` field carries "Crimson Lotus Tea" / "white2tea".
    Either way the matcher will canonicalize via alias-append.
    """
    tag_value = tags.first("producer")
    if tag_value:
        return ProducerHint(name=tag_value, source="tag")
    vendor_field = payload.get("vendor")
    if isinstance(vendor_field, str) and vendor_field.strip():
        return ProducerHint(name=_collapse_ws(vendor_field), source="vendor_field")
    return None


def _region_from_tags(tags: ParsedTags) -> RegionHint | None:
    """Pull a country + province hint from YS ``Region_*`` tags.

    V1 rules — deliberately loose:
    - ``Region_Yunnan`` / ``Region_Fujian`` / ``Region_Guangdong`` are
      Chinese provinces, so we set ``country="China"`` + ``province=value``.
    - Bare values like ``"Fujian"`` / ``"Guangdong"`` (Crimson Lotus
      free-form, not Key_Value) are NOT seen as region hints — they'd need a
      gazetteer lookup. ml-engineer can layer that in V1.5.
    """
    raw = tags.first("region")
    if not raw:
        return None
    return RegionHint(country="China", province=raw)


def _tea_type_from_payload(payload: dict[str, Any]) -> str | None:
    """Use Shopify ``product_type`` verbatim (after NFC + trim).

    Goldens show: "Raw Pu-erh Tea", "Ripe Pu-erh Tea", "White Tea",
    "Oolong Tea", "Black Tea", "Hei Cha", "Seattle", "Shou Puerh", etc.
    V1 keeps the raw value; the ontology curator V1.5 will normalize it.
    """
    ptype = payload.get("product_type")
    if not isinstance(ptype, str):
        return None
    cleaned = _collapse_ws(_nfc(ptype))
    return cleaned or None


def _is_non_tea(payload: dict[str, Any]) -> bool:
    """white2tea encodes teaware via ``product_type = "Non-Tea"``."""
    ptype = payload.get("product_type")
    if isinstance(ptype, str) and ptype.strip().lower() == "non-tea":
        return True
    # Also catch the "Non-Tea" free-form tag, belt-and-braces.
    tags = payload.get("tags")
    if isinstance(tags, list) and any(
        isinstance(t, str) and t.strip().lower() == "non-tea" for t in tags
    ):
        return True
    return False


def _vendor_url(payload: dict[str, Any], vendor_base_url: str | None) -> str | None:
    handle = payload.get("handle")
    if not isinstance(handle, str) or not handle.strip() or not vendor_base_url:
        return None
    return f"{vendor_base_url.rstrip('/')}/products/{handle}"


def _variant_price_cents(variant: dict[str, Any]) -> int | None:
    """Parse the variant ``price`` (a string in Shopify) to integer cents."""
    raw = variant.get("price")
    if raw is None:
        return None
    try:
        # Shopify quotes prices as strings: "17.00", "189.00". float() is
        # safe for the 2-decimal range; round() snaps to integer cents.
        return int(round(float(str(raw)) * 100))
    except (TypeError, ValueError):
        return None


def _description_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over a canonical projection of (title, body_html, tags).

    Cross-references :func:`tea_scrapers.load.bronze.payload_hash`. Same
    algorithm (sort_keys, compact separators, NFC); narrower projection.
    Case is preserved — flipping the title to lowercase IS a description
    change for V1 purposes.
    """
    import hashlib
    import json

    title = payload.get("title")
    body = payload.get("body_html")
    tags = payload.get("tags") or []

    projected = {
        "title": _nfc(title) if isinstance(title, str) else None,
        "body_html": _nfc(body) if isinstance(body, str) else None,
        "tags": sorted(_nfc(t) for t in tags if isinstance(t, str)),
    }
    canonical = json.dumps(
        projected, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def map_payload_to_products(
    payload: dict[str, Any],
    *,
    vendor_base_url: str | None = None,
    default_currency: str = "USD",
) -> list[ProductFields]:
    """Expand one Shopify payload into N (product, variant) silver tuples.

    Returns one :class:`ProductFields` per Shopify variant. The runner
    decides what to do with non-tea or unmappable results based on the
    ``is_non_tea`` flag and a ``weight_grams=None`` value on every variant.

    Never raises on a recognized-but-empty payload — callers count
    ``skipped_unmappable`` for that.
    """
    title = _nfc(str(payload.get("title", ""))).strip()
    if not title:
        # No usable name — the runner will count this as skipped_unmappable.
        # Synthesize a single placeholder with empty name so the runner can
        # decide; we don't want to silently drop here.
        title = ""

    tags = parse_tags(payload.get("tags"))
    producer_hint = _producer_from_payload(payload, tags)
    region_hint = _region_from_tags(tags)
    harvest_year = _parse_harvest_year(title, tags)
    tea_type = _tea_type_from_payload(payload)
    non_tea = _is_non_tea(payload)
    desc_hash = _description_hash(payload)
    vendor_url = _vendor_url(payload, vendor_base_url)

    shopify_product_id = str(payload.get("id", ""))
    canonical_name = title
    norm = normalize_name(canonical_name)

    variants_raw = payload.get("variants")
    if not isinstance(variants_raw, list) or not variants_raw:
        # Pathological — Shopify always emits at least the default variant.
        # Synthesize a sentinel single-variant record so the runner can
        # bucket it as unmappable rather than crashing.
        return [
            ProductFields(
                shopify_product_id=shopify_product_id,
                canonical_name=canonical_name,
                normalized_name=norm,
                producer_hint=producer_hint,
                region_hint=region_hint,
                harvest_year=harvest_year,
                tea_type=tea_type,
                tea_style=None,
                format=None,
                cultivar=None,
                weight_grams=None,
                variant=VariantFields(
                    shopify_variant_id="",
                    weight_grams=None,
                    available=False,
                    price_cents=None,
                    currency=default_currency,
                ),
                vendor_url=vendor_url,
                description_hash=desc_hash,
                is_non_tea=non_tea,
            )
        ]

    out: list[ProductFields] = []
    for variant in variants_raw:
        if not isinstance(variant, dict):
            continue
        # weight: option1 first (per the brief — variant.grams includes
        # packaging tare); fall back to title for "200g Cake"-style names.
        option_weight = _parse_weight_from_option(variant.get("option1"))
        if option_weight is None:
            option_weight = _parse_weight_from_option(variant.get("title"))
        if option_weight is None:
            option_weight = _parse_weight_from_title(canonical_name)

        variant_fields = VariantFields(
            shopify_variant_id=str(variant.get("id", "")),
            weight_grams=option_weight,
            available=bool(variant.get("available", False)),
            price_cents=_variant_price_cents(variant),
            currency=default_currency,
        )

        out.append(
            ProductFields(
                shopify_product_id=shopify_product_id,
                canonical_name=canonical_name,
                normalized_name=norm,
                producer_hint=producer_hint,
                region_hint=region_hint,
                harvest_year=harvest_year,
                tea_type=tea_type,
                tea_style=None,
                format=None,
                cultivar=None,
                weight_grams=option_weight,
                variant=variant_fields,
                vendor_url=vendor_url,
                description_hash=desc_hash,
                is_non_tea=non_tea,
            )
        )

    return out


__all__ = [
    "ProducerHint",
    "RegionHint",
    "VariantFields",
    "ProductFields",
    "map_payload_to_products",
    "normalize_name",
]
