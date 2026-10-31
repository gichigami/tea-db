"""Schema + loader for the canonical-matcher labeled pair set (ROADMAP H.1).

Fixtures live in ``tests/fixtures/matching/{product,producer}_pairs.jsonl``
and are produced deterministically by ``scripts/build_matcher_pairs.py``.
See ``tests/fixtures/matching/README.md`` for provenance + review status.

**Label semantics.** ``match`` means "the canonical matcher SHOULD resolve
both sides to the same row" — for products that is the §8 identity
``(producer, harvest_year, name, weight_grams)``, so a 25 g sample and a
357 g cake of the same pressing are ``non_match`` by policy (the hybrid
attribute guard keeps that invariant). For producers it means "same
producing entity" (factory / brand / workshop / artisan).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Entity = Literal["product", "producer"]
Label = Literal["match", "non_match"]
Confidence = Literal["high", "low"]
# cassette          — both sides verbatim from committed cassettes / golden JSONL
# cassette_derived  — one side rewritten from attested repo data (a CJK token
#                     found in the same product's body_html, a romanization
#                     variant seen elsewhere in the catalog, a sibling recipe
#                     code, a Steepster slug from config/vendors.yaml)
# synthetic         — invented for coverage; real data had no example
Source = Literal["cassette", "cassette_derived", "synthetic"]
ReviewStatus = Literal["agent_proposed", "owner_confirmed", "owner_corrected"]

PRODUCT_CATEGORIES: frozenset[str] = frozenset(
    {
        "suffix_variant",
        "cross_vendor_same_tea",
        "duplicate_listing",
        "cross_script",
        "hard_negative_recipe_code",
        "hard_negative_year",
        "hard_negative_weight",
        "hard_negative_sheng_shou",
        "hard_negative_tea_type",
        "hard_negative_name_variant",
        "cross_vendor_lookalike",
        "easy_negative",
    }
)
PRODUCER_CATEGORIES: frozenset[str] = frozenset(
    {
        "producer_alias",
        "cross_script",
        "hard_negative_producer",
        "easy_negative",
    }
)
CATEGORIES: dict[str, frozenset[str]] = {
    "product": PRODUCT_CATEGORIES,
    "producer": PRODUCER_CATEGORIES,
}
PAIR_ID_PREFIX: dict[str, str] = {"product": "prod", "producer": "prdc"}


class PairSide(BaseModel):
    """One side of a pair, carrying exactly what the V1 matcher blocks on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    # Product-only blocking attributes (None for producer pairs).
    producer: str | None = None
    harvest_year: int | None = Field(default=None, ge=1900, le=2100)
    weight_grams: int | None = Field(default=None, gt=0)
    # Provenance: ``cassette:<file>#<key>``, ``golden:<file>#<key>``,
    # ``derived:<how>:<base ref>``, ``config:<file>#<key>`` or ``synthetic``.
    source_ref: str = Field(min_length=1)
    vendor: str | None = None


class LabeledPair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pair_id: str = Field(pattern=r"^(prod|prdc)-\d{4}$")
    entity: Entity
    left: PairSide
    right: PairSide
    label: Label
    label_confidence: Confidence
    category: str
    tags: list[str] = Field(default_factory=list)
    source: Source
    rationale: str = Field(min_length=10)
    review_status: ReviewStatus = "agent_proposed"

    @model_validator(mode="after")
    def _check(self) -> LabeledPair:
        if not self.pair_id.startswith(PAIR_ID_PREFIX[self.entity] + "-"):
            raise ValueError(f"pair_id {self.pair_id!r} does not match entity {self.entity!r}")
        if self.category not in CATEGORIES[self.entity]:
            raise ValueError(f"category {self.category!r} not allowed for {self.entity}")
        if self.entity == "producer":
            for side in (self.left, self.right):
                if side.producer or side.harvest_year or side.weight_grams:
                    raise ValueError("producer pairs carry no product attributes")
        else:
            for side in (self.left, self.right):
                if not side.producer:
                    raise ValueError("product pair sides must carry a producer hint")
        refs = (self.left.source_ref, self.right.source_ref)
        if self.source == "cassette" and any(
            not r.startswith(("cassette:", "golden:")) for r in refs
        ):
            raise ValueError("source=cassette requires both sides from cassette/golden")
        if self.source == "synthetic" and not any(r == "synthetic" for r in refs):
            raise ValueError("source=synthetic requires a synthetic side")
        if self.source == "cassette_derived" and not any(
            r.startswith(("derived:", "config:")) for r in refs
        ):
            raise ValueError("source=cassette_derived requires a derived/config side")
        return self


def load_pairs(path: str | Path) -> list[LabeledPair]:
    """Load + validate a JSONL pair file. Raises on the first invalid line."""
    out: list[LabeledPair] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                out.append(LabeledPair.model_validate_json(line))
            except Exception as err:  # pragma: no cover - surfaced with context
                raise ValueError(f"{path}:{lineno}: {err}") from err
    return out


def dump_pairs(pairs: Iterable[LabeledPair], path: str | Path) -> None:
    """Write pairs as canonical JSONL (sorted keys, UTF-8, one per line)."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for p in pairs:
            fh.write(
                json.dumps(p.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                + "\n"
            )


__all__ = [
    "CATEGORIES",
    "PAIR_ID_PREFIX",
    "PRODUCER_CATEGORIES",
    "PRODUCT_CATEGORIES",
    "LabeledPair",
    "PairSide",
    "dump_pairs",
    "load_pairs",
]
