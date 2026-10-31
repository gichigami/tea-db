"""Bronze → silver normalization + canonical-ID matching (spec §8)."""

from tea_scrapers.normalize.canonical import (
    CanonicalMatcher,
    GuardVeto,
    ProductDecision,
    ProductMatchResult,
)
from tea_scrapers.normalize.silver import NormalizeStats, SilverNormalizer

__all__ = [
    "CanonicalMatcher",
    "GuardVeto",
    "NormalizeStats",
    "ProductDecision",
    "ProductMatchResult",
    "SilverNormalizer",
]
