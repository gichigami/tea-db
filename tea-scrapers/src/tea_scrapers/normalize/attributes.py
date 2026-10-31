"""Attribute-guard parser + matching-key helpers for the canonical matcher.

Pure module: no DB, no LLM, no I/O. Owned by data-engineer (ROADMAP H.2,
spec §12 "Hybrid canonical matcher" step 2).

Two concerns live here:

1. **Name attributes** — :func:`parse_name_attributes` pulls the tokens that
   distinguish otherwise near-identical tea product names and that both
   trigram and dense embeddings collapse: vintage year, weight, Menghai /
   CNNP-style recipe codes (``7542``, ``7262``, ``"1815 Recipe"``), and
   sheng vs shou (raw / 生 vs ripe / 熟).

2. **The attribute guard** — :func:`check_attribute_guard` compares two
   :class:`GuardAttributes` and vetoes a candidate pair on any conflict.
   Precision-first: merges are irreversible downstream (snapshot chains,
   reviews and quote-evidence attach to the merged ``product_id``),
   over-creates are a single UPDATE to undo.

Source-of-truth note for year / weight: the matcher's ``harvest_year`` and
``weight_grams`` come from :mod:`shopify_mapper` (YS tags / start-of-title
year; ``variant.option1`` weight, never ``variant.grams``). The guard
compares *those* values (``GuardAttributes.harvest_year`` /
``.weight_grams``), not the name-parsed ones. The name-parsed
``NameAttributes.vintage_year`` / ``.weight_grams`` are informational and
exist mainly so recipe-code detection can tell ``7542`` apart from
``2007`` and ``357g``. Weight text parsing delegates to the mapper's parser
so there is a single weight grammar.

3. **Matching key** — :func:`strip_inventory_suffix` / :func:`matching_key`
   drop location / inventory suffixes such as ``" :: Seattle Inventory"``
   so a vendor's warehouse-specific re-listing matches its base listing.
   This is applied ONLY to the matching key; the raw vendor title is still
   what lands in ``product.canonical_name`` and bronze (spec §11:
   capture everything; design §4 quote-evidence).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from tea_scrapers.normalize.shopify_mapper import normalize_name, parse_weight_text

# ---------------------------------------------------------------------------
# Inventory / location suffixes (matching key only)
# ---------------------------------------------------------------------------

# Only suffixes evidenced in the cassettes are handled. Survey 2026-10-31 over
# all four Shopify cassettes (2,462 product titles): the ONLY ``" :: ..."``
# location/inventory suffix is Crimson Lotus ``":: Seattle Inventory"``
# (121 titles; 87 have a byte-identical base listing once stripped). Other
# ``" :: "`` segments (``"... Tuo Cha :: Sheng / Raw Puerh Tea"``) are part of
# the product name and are NOT stripped. YS uses ``" * Spring 2025"`` and
# ``" - Part 3"`` which carry identity (vintage / sampler part) — not stripped.
#
# The pattern string is written in the common subset of Python ``re`` and
# Postgres ARE syntax so the matcher can pass the very same string to
# ``regexp_replace(canonical_name, :sfx, '', 'i')`` — one definition, two
# engines. Always apply it case-insensitively.
INVENTORY_SUFFIX_PATTERN = r"\s*::\s*seattle\s+inventory\s*$"
_INVENTORY_SUFFIX_RE = re.compile(INVENTORY_SUFFIX_PATTERN, re.IGNORECASE)


def strip_inventory_suffix(name: str) -> str:
    """Remove a trailing inventory/location suffix (``" :: Seattle Inventory"``)."""
    return _INVENTORY_SUFFIX_RE.sub("", name)


def has_inventory_suffix(name: str) -> bool:
    return _INVENTORY_SUFFIX_RE.search(name) is not None


def matching_key(name: str) -> str:
    """Normalized name used for exact matching: suffix-stripped ``normalize_name``."""
    return normalize_name(strip_inventory_suffix(name))


# ---------------------------------------------------------------------------
# Name attribute parsing
# ---------------------------------------------------------------------------


class TeaStyle(str, Enum):
    """Pu-erh processing style parsed from a product name."""

    SHENG = "sheng"  # raw / 生
    SHOU = "shou"  # ripe / 熟
    MIXED = "mixed"  # both markers present (combo packs, "Ripe Raw" titles)


@dataclass(frozen=True)
class NameAttributes:
    vintage_year: int | None
    weight_grams: int | None
    recipe_codes: frozenset[str]
    style: TeaStyle | None


# Plausible vintage years. Tea vintages before 1900 are not a thing in the
# catalog; ``1815`` (Three Cranes Liu Bao recipe) therefore can never be read
# as a year.
_YEAR_MIN = 1900
_YEAR_MAX = 2099

# A standalone 4-digit token. Look-behind rejects digits / decimals / SKU-ish
# joins (``D-27``, ``#1234``, ``1.5``); look-ahead rejects longer numbers and
# decimals.
_FOUR_DIGIT_RE = re.compile(r"(?<![\d.#/\-])(\d{4})(?![\d.])")
# Units that make a number a quantity, not a code: "1100ml", "1000g",
# "1000 grams", "2000 kg". Checked against the text right after the token.
_UNIT_AFTER_RE = re.compile(
    r"\s*(?:g|grams?|gr|kg|kilos?|kilograms?|mg|ml|cc|pcs|pieces)\b", re.IGNORECASE
)
# Explicit recipe marker after the token, optionally across a closing quote:
# "1815 Recipe", '"1815 Recipe"', "7542 recipe".
_RECIPE_AFTER_RE = re.compile(r"\s*[\"'“”‘’]?\s*recipe\b", re.IGNORECASE)

# Style markers. Word-bounded so "Shousparilla" / "Shengtai" don't fire.
_SHENG_RE = re.compile(r"\b(?:sheng|raw)\b|生", re.IGNORECASE)
_SHOU_RE = re.compile(r"\b(?:shou|ripe)\b|熟", re.IGNORECASE)
# Phrases where the marker word is NOT a processing style. Each is either a
# standard tea term or evidenced in the cassettes (survey 2026-10-31):
#   ye sheng (野生, "wild": "Jinggu Ye Sheng Cha"), shou mei (寿眉 white tea),
#   sheng tai (生态, "ecological"), and potter / brand names on teaware
#   ("Zhang Quan Sheng", "Huang Shou Zhen", "Guan Ren Sheng",
#   "Ding Sheng Hao"). Removed before style detection.
_STYLE_FALSE_POSITIVE_RE = re.compile(
    r"\bye\s+sheng\b|野生|\bshou\s+mei\b|寿眉|壽眉|\bsheng\s+tai\b|生态|生態"
    r"|\bquan\s+sheng\b|\bren\s+sheng\b|\bshou\s+zhen\b|\bsheng\s+hao\b",
    re.IGNORECASE,
)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _parse_style(name: str) -> TeaStyle | None:
    cleaned = _STYLE_FALSE_POSITIVE_RE.sub(" ", name)
    sheng = _SHENG_RE.search(cleaned) is not None
    shou = _SHOU_RE.search(cleaned) is not None
    if sheng and shou:
        return TeaStyle.MIXED
    if sheng:
        return TeaStyle.SHENG
    if shou:
        return TeaStyle.SHOU
    return None


def _parse_years_and_codes(name: str) -> tuple[int | None, frozenset[str]]:
    """Classify each standalone 4-digit token as year, quantity, or recipe code.

    Order of rules per token:

    1. Followed by an explicit ``Recipe`` marker → recipe code (wins over
       everything, so a hypothetical ``"2001 Recipe"`` would still be a code).
    2. Followed by a unit (``g`` / ``grams`` / ``kg`` / ``ml`` …) → quantity;
       ignored here (weight comes from :func:`parse_weight_text`).
    3. In ``[1900, 2099]`` → year candidate. The first one is the vintage.
    4. Otherwise → recipe code (``7542``, ``7262``, ``8582``, ``2503``,
       ``0532``).
    """
    vintage: int | None = None
    codes: set[str] = set()
    for m in _FOUR_DIGIT_RE.finditer(name):
        token = m.group(1)
        rest = name[m.end() :]
        if _RECIPE_AFTER_RE.match(rest):
            codes.add(token)
            continue
        if _UNIT_AFTER_RE.match(rest):
            continue
        value = int(token)
        if _YEAR_MIN <= value <= _YEAR_MAX:
            if vintage is None:
                vintage = value
            continue
        codes.add(token)
    return vintage, frozenset(codes)


def parse_name_attributes(name: str | None) -> NameAttributes:
    """Parse vintage year, weight, recipe code(s) and sheng/shou from a name.

    Never raises; unparseable / empty names yield all-``None`` attributes.
    The inventory suffix is stripped first (it carries no attributes, and
    stripping keeps parse results identical between a base listing and its
    ``":: Seattle Inventory"`` twin).
    """
    if not name:
        return NameAttributes(None, None, frozenset(), None)
    text = strip_inventory_suffix(_nfc(name))
    vintage, codes = _parse_years_and_codes(text)
    return NameAttributes(
        vintage_year=vintage,
        weight_grams=parse_weight_text(text),
        recipe_codes=codes,
        style=_parse_style(text),
    )


# ---------------------------------------------------------------------------
# Attribute guard
# ---------------------------------------------------------------------------


class OneSidedPolicy(str, Enum):
    """What the guard does when an attribute is known on one side only."""

    VETO = "veto"  # precision-first; V1 ``IS NOT DISTINCT FROM`` equivalent
    PASS = "pass"  # defer to scoring (recall-friendlier)


# Spec §12 "Hybrid canonical matcher" sub-question #7 — PROVISIONAL
# (data-engineer, pending H.1 labeled-set evidence). All attributes veto on
# one-sided presence, matching the V1 trigram step's ``IS NOT DISTINCT FROM``
# blocking for harvest_year / weight_grams. Flip an entry to ``PASS`` only on
# labeled-set evidence that the recall cost outweighs the precision risk.
ONE_SIDED_POLICY: dict[str, OneSidedPolicy] = {
    "harvest_year": OneSidedPolicy.VETO,
    "weight_grams": OneSidedPolicy.VETO,
    "recipe_code": OneSidedPolicy.VETO,
    "style": OneSidedPolicy.VETO,
}


@dataclass(frozen=True)
class GuardAttributes:
    """Attributes compared by the guard for one side of a candidate pair."""

    harvest_year: int | None
    weight_grams: int | None
    recipe_codes: frozenset[str]
    style: TeaStyle | None

    @classmethod
    def from_name(
        cls, name: str | None, *, harvest_year: int | None, weight_grams: int | None
    ) -> GuardAttributes:
        """Recipe code + style from the name; year + weight from the mapper/row."""
        parsed = parse_name_attributes(name)
        return cls(
            harvest_year=harvest_year,
            weight_grams=weight_grams,
            recipe_codes=parsed.recipe_codes,
            style=parsed.style,
        )


@dataclass(frozen=True)
class GuardConflict:
    attribute: str  # key into ONE_SIDED_POLICY
    kind: str  # "mismatch" | "one_sided"
    query_value: object
    candidate_value: object

    @property
    def reason(self) -> str:
        """Stable stats key, e.g. ``"recipe_code:mismatch"``."""
        return f"{self.attribute}:{self.kind}"


@dataclass(frozen=True)
class GuardVerdict:
    conflicts: tuple[GuardConflict, ...]

    @property
    def vetoed(self) -> bool:
        return bool(self.conflicts)


def _compare(
    attribute: str,
    query_value: object,
    candidate_value: object,
    policy: dict[str, OneSidedPolicy],
) -> GuardConflict | None:
    q_known = query_value is not None and query_value != frozenset()
    c_known = candidate_value is not None and candidate_value != frozenset()
    if q_known and c_known:
        if query_value != candidate_value:
            return GuardConflict(attribute, "mismatch", query_value, candidate_value)
        return None
    if q_known != c_known and policy.get(attribute, OneSidedPolicy.VETO) is OneSidedPolicy.VETO:
        return GuardConflict(attribute, "one_sided", query_value, candidate_value)
    return None


def check_attribute_guard(
    query: GuardAttributes,
    candidate: GuardAttributes,
    *,
    policy: dict[str, OneSidedPolicy] | None = None,
) -> GuardVerdict:
    """Compare two sides; any conflict vetoes the pair.

    - Both known and different → ``mismatch`` (always vetoes).
    - Known on one side only → ``one_sided`` (vetoes iff policy is ``VETO``).
    - Both unknown → no conflict.

    ``MIXED`` style is a distinct value: a sheng/shou combo pack never
    matches a single-style cake.
    """
    pol = ONE_SIDED_POLICY if policy is None else policy
    conflicts = [
        c
        for c in (
            _compare("harvest_year", query.harvest_year, candidate.harvest_year, pol),
            _compare("weight_grams", query.weight_grams, candidate.weight_grams, pol),
            _compare("recipe_code", query.recipe_codes, candidate.recipe_codes, pol),
            _compare("style", query.style, candidate.style, pol),
        )
        if c is not None
    ]
    return GuardVerdict(conflicts=tuple(conflicts))


__all__ = [
    "INVENTORY_SUFFIX_PATTERN",
    "ONE_SIDED_POLICY",
    "GuardAttributes",
    "GuardConflict",
    "GuardVerdict",
    "NameAttributes",
    "OneSidedPolicy",
    "TeaStyle",
    "check_attribute_guard",
    "has_inventory_suffix",
    "matching_key",
    "parse_name_attributes",
    "strip_inventory_suffix",
]
