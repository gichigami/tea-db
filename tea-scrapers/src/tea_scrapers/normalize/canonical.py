"""Canonical product + producer ID matching (spec §8).

The product matcher follows the 4-step ladder from §8:

1. Exact match on ``(producer_id, harvest_year, lower(canonical_name),
   weight_grams)`` — using ``IS NOT DISTINCT FROM`` to make nullable
   comparisons match cleanly.
2. Trigram similarity ≥0.85 on ``canonical_name`` filtered by
   ``producer_id``. Driven by the ``%`` operator with a session-scoped
   ``set_limit(0.85)`` so the GIN trigram index actually fires (a
   ``similarity()`` predicate would not).
3. **Ambiguous** (≥2 candidates within 0.10 of each other) → V1 stub:
   we don't have an LLM tiebreaker yet (ml-engineer V1.5), so we log
   the ambiguity, bump a counter, and **fall through to step 4**.
   Rationale (per data-engineer brief): merging is unrecoverable;
   de-duping an over-created row is a single UPDATE. Asymmetric risk →
   bias toward over-creation in V1.
4. Otherwise create.

The producer matcher is structurally similar but with three differences:

- Alias-first: ``WHERE :norm = ANY(aliases)`` before canonical-name exact.
- No LLM step: tie-breaks pick the lowest ``producer_id`` deterministically
  (producer_id stability matters more than perfect recall in V1).
- Newly seen raw-form spellings (different case, accents) are appended to
  ``aliases`` on every hit so the matcher learns over time.

Note: ``product.canonical_name`` is intentionally NOT UNIQUE — the matcher
key is the 4-tuple. Future readers should not "fix" this by adding a unique
constraint.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from tea_scrapers.normalize.shopify_mapper import (
    ProducerHint,
    ProductFields,
    RegionHint,
    normalize_name,
)
from tea_scrapers.storage.models import Producer, Product, Region

# Module-level matcher threshold. Set both in-Python (so test assertions
# can reference it) and at the Postgres session level (so the GIN trigram
# index uses it as the cutoff in ``%`` queries).
TRIGRAM_THRESHOLD = 0.85
# Above this top similarity, a single dominant candidate is "obvious" enough
# that we reuse without flagging as ambiguous. Inside [0.85, 0.95) with ≥2
# candidates within 0.10 of each other, we flag.
AMBIGUOUS_TOP_CEILING = 0.95
AMBIGUOUS_GAP = 0.10


class ProductDecision(str, Enum):
    EXACT = "exact"
    TRIGRAM = "trigram"
    CREATED = "created"
    AMBIGUOUS_CREATED = "ambiguous_created"


@dataclass
class ProductMatchResult:
    product_id: int
    decision: ProductDecision


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _producer_norm(name: str) -> str:
    """NFC + lowercase + collapse whitespace.

    Stored already-normalized in ``producer.aliases`` so the ``ANY(aliases)``
    check is a simple equality scan rather than a per-row normalize call.
    """
    import re

    return re.sub(r"\s+", " ", _nfc(name).lower()).strip()


class CanonicalMatcher:
    """Stateful matcher — caches producer/region rows for the duration of a run.

    The cache is intentionally session-naive (a plain dict keyed by string
    name). Bronze rows are processed in a single batched transaction in
    :class:`SilverNormalizer`, so cache lifetime ≈ transaction lifetime.
    Multi-writer concurrent normalizer runs would need pessimistic locking;
    V1 cron is single-writer — see §12 OQ.
    """

    def __init__(self, log: structlog.stdlib.BoundLogger | None = None) -> None:
        self._log = log or structlog.get_logger(__name__)
        self._producer_cache: dict[str, int] = {}
        self._region_cache: dict[tuple[str | None, ...], int] = {}

    # ------------------------------------------------------------------
    # Producer
    # ------------------------------------------------------------------

    def match_or_create_producer(
        self, hint: ProducerHint | None, session: Session
    ) -> int | None:
        """Return the canonical ``producer_id`` for ``hint.name``, creating if needed.

        Returns ``None`` when the hint itself is missing — the caller may
        still want to create a product (e.g. a no-vendor-tag teaware row),
        though in practice the runner skips those upstream.
        """
        if hint is None or not hint.name or not hint.name.strip():
            return None

        raw = hint.name.strip()
        norm = _producer_norm(raw)
        cached = self._producer_cache.get(norm)
        if cached is not None:
            return cached

        # Make sure the trigram threshold is set for this session before
        # we issue any ``%`` query. Idempotent; cheap to re-issue.
        self._ensure_trgm_limit(session)

        # Step 1: alias hit. ``aliases`` is TEXT[]; store normalized form so
        # we can compare via ``ANY(...)`` directly without per-row normalize.
        row = session.execute(
            text(
                "SELECT producer_id, canonical_name, aliases "
                "FROM producer WHERE :norm = ANY(aliases) "
                "ORDER BY producer_id ASC LIMIT 1"
            ),
            {"norm": norm},
        ).first()
        if row is not None:
            self._maybe_append_alias(session, row.producer_id, row.aliases, norm)
            self._producer_cache[norm] = row.producer_id
            return row.producer_id

        # Step 2: canonical-name exact (lowercased compare).
        row = session.execute(
            text(
                "SELECT producer_id, canonical_name, aliases FROM producer "
                "WHERE LOWER(canonical_name) = :norm ORDER BY producer_id ASC LIMIT 1"
            ),
            {"norm": norm},
        ).first()
        if row is not None:
            self._maybe_append_alias(session, row.producer_id, row.aliases, norm)
            self._producer_cache[norm] = row.producer_id
            return row.producer_id

        # Step 3: trigram similarity ≥ threshold. Index-driven via ``%``.
        rows = session.execute(
            text(
                "SELECT producer_id, canonical_name, aliases, "
                "similarity(canonical_name, :name) AS sim "
                "FROM producer WHERE canonical_name % :name "
                "ORDER BY sim DESC, producer_id ASC LIMIT 5"
            ),
            {"name": raw},
        ).all()
        if rows:
            top = rows[0]
            if len(rows) > 1:
                self._log.info(
                    "producer.ambiguous",
                    raw=raw,
                    candidates=[(r.producer_id, r.canonical_name, float(r.sim)) for r in rows],
                    chosen=top.producer_id,
                )
            self._maybe_append_alias(session, top.producer_id, top.aliases, norm)
            self._producer_cache[norm] = top.producer_id
            return top.producer_id

        # Step 4: create.
        producer = Producer(canonical_name=raw, aliases=[norm])
        session.add(producer)
        session.flush()
        self._producer_cache[norm] = producer.producer_id
        return producer.producer_id

    def _maybe_append_alias(
        self,
        session: Session,
        producer_id: int,
        existing_aliases: list[str] | None,
        new_alias: str,
    ) -> None:
        """Append ``new_alias`` to ``producer.aliases`` if not already present."""
        current = list(existing_aliases or [])
        if new_alias in current:
            return
        session.execute(
            text(
                "UPDATE producer SET aliases = COALESCE(aliases, ARRAY[]::TEXT[]) "
                "|| ARRAY[:new]::TEXT[] WHERE producer_id = :pid"
            ),
            {"new": new_alias, "pid": producer_id},
        )

    # ------------------------------------------------------------------
    # Region
    # ------------------------------------------------------------------

    def match_or_create_region(
        self, hint: RegionHint | None, session: Session
    ) -> int | None:
        """Get-or-create a ``region`` row from a :class:`RegionHint`.

        V1 region fan-out is intentionally shallow — see ``_region_from_tags``
        in :mod:`shopify_mapper`. Most rows will be ``(China, Yunnan)`` or
        nothing. The 5-tuple unique constraint covers nullables via
        ``IS NOT DISTINCT FROM``-style logic at the Postgres level (the
        unique key treats NULLs as not-equal in older PG versions; v15+ has
        ``NULLS NOT DISTINCT`` but we're not depending on it — instead the
        cache key tuple normalizes).
        """
        if hint is None or hint.country is None:
            return None

        key = (
            hint.country,
            hint.province,
            hint.county,
            hint.mountain,
            hint.village,
        )
        cached = self._region_cache.get(key)
        if cached is not None:
            return cached

        # Exact match on the 5-tuple.
        row = session.execute(
            text(
                "SELECT region_id FROM region "
                "WHERE country IS NOT DISTINCT FROM :country "
                "  AND province IS NOT DISTINCT FROM :province "
                "  AND county IS NOT DISTINCT FROM :county "
                "  AND mountain IS NOT DISTINCT FROM :mountain "
                "  AND village IS NOT DISTINCT FROM :village "
                "LIMIT 1"
            ),
            {
                "country": hint.country,
                "province": hint.province,
                "county": hint.county,
                "mountain": hint.mountain,
                "village": hint.village,
            },
        ).first()
        if row is not None:
            self._region_cache[key] = row.region_id
            return row.region_id

        region = Region(
            country=hint.country,
            province=hint.province,
            county=hint.county,
            mountain=hint.mountain,
            village=hint.village,
        )
        session.add(region)
        session.flush()
        self._region_cache[key] = region.region_id
        return region.region_id

    # ------------------------------------------------------------------
    # Product (4-step ladder)
    # ------------------------------------------------------------------

    def match_or_create_product(
        self,
        fields: ProductFields,
        *,
        producer_id: int | None,
        region_id: int | None,
        session: Session,
    ) -> ProductMatchResult:
        """Match the (producer, year, name, weight) tuple → existing or new product."""
        # Step 1: exact 4-tuple.
        # ``IS NOT DISTINCT FROM`` treats NULL == NULL as true. Required
        # because producer_id / harvest_year / weight_grams may all be NULL.
        row = session.execute(
            text(
                "SELECT product_id FROM product "
                "WHERE producer_id IS NOT DISTINCT FROM :producer_id "
                "  AND harvest_year IS NOT DISTINCT FROM :harvest_year "
                "  AND LOWER(canonical_name) = :norm "
                "  AND weight_grams IS NOT DISTINCT FROM :weight_grams "
                "ORDER BY product_id ASC LIMIT 1"
            ),
            {
                "producer_id": producer_id,
                "harvest_year": fields.harvest_year,
                "norm": fields.normalized_name,
                "weight_grams": fields.weight_grams,
            },
        ).first()
        if row is not None:
            return ProductMatchResult(product_id=row.product_id, decision=ProductDecision.EXACT)

        # Step 2: trigram on canonical_name filtered by producer. Producer is
        # nullable; if we have no producer hint, restrict to producer-less
        # rows so we don't cross-pollinate against rows with strong producer
        # identity (e.g. "Mountain Shadow" by Crimson Lotus must not match
        # a producer-less name collision from a different vendor's catalog).
        self._ensure_trgm_limit(session)

        if producer_id is not None:
            sim_rows = session.execute(
                text(
                    "SELECT product_id, canonical_name, "
                    "  similarity(canonical_name, :name) AS sim "
                    "FROM product "
                    "WHERE producer_id = :producer_id "
                    "  AND canonical_name % :name "
                    "  AND harvest_year IS NOT DISTINCT FROM :harvest_year "
                    "  AND weight_grams IS NOT DISTINCT FROM :weight_grams "
                    "ORDER BY sim DESC, product_id ASC LIMIT 5"
                ),
                {
                    "producer_id": producer_id,
                    "name": fields.canonical_name,
                    "harvest_year": fields.harvest_year,
                    "weight_grams": fields.weight_grams,
                },
            ).all()
        else:
            sim_rows = session.execute(
                text(
                    "SELECT product_id, canonical_name, "
                    "  similarity(canonical_name, :name) AS sim "
                    "FROM product "
                    "WHERE producer_id IS NULL "
                    "  AND canonical_name % :name "
                    "  AND harvest_year IS NOT DISTINCT FROM :harvest_year "
                    "  AND weight_grams IS NOT DISTINCT FROM :weight_grams "
                    "ORDER BY sim DESC, product_id ASC LIMIT 5"
                ),
                {
                    "name": fields.canonical_name,
                    "harvest_year": fields.harvest_year,
                    "weight_grams": fields.weight_grams,
                },
            ).all()

        if sim_rows:
            top = sim_rows[0]
            top_sim = float(top.sim)
            # Step 3: ambiguity check — log + fall through to over-create.
            # (V1 LLM tiebreaker stub: see §12 OQ.)
            if (
                len(sim_rows) >= 2
                and top_sim < AMBIGUOUS_TOP_CEILING
                and (top_sim - float(sim_rows[1].sim)) < AMBIGUOUS_GAP
            ):
                self._log.info(
                    "silver_match_ambiguity",
                    name=fields.canonical_name,
                    producer_id=producer_id,
                    candidates=[
                        (r.product_id, r.canonical_name, float(r.sim))
                        for r in sim_rows
                    ],
                    decision="over_create_v1_stub",
                )
                created = self._create_product(
                    fields,
                    producer_id=producer_id,
                    region_id=region_id,
                    session=session,
                )
                return ProductMatchResult(
                    product_id=created, decision=ProductDecision.AMBIGUOUS_CREATED
                )
            # Single dominant candidate — reuse.
            return ProductMatchResult(
                product_id=top.product_id, decision=ProductDecision.TRIGRAM
            )

        # Step 4: create.
        created = self._create_product(
            fields,
            producer_id=producer_id,
            region_id=region_id,
            session=session,
        )
        return ProductMatchResult(product_id=created, decision=ProductDecision.CREATED)

    def _create_product(
        self,
        fields: ProductFields,
        *,
        producer_id: int | None,
        region_id: int | None,
        session: Session,
    ) -> int:
        product = Product(
            canonical_name=fields.canonical_name,
            producer_id=producer_id,
            region_id=region_id,
            tea_type=fields.tea_type,
            tea_style=fields.tea_style,
            harvest_year=fields.harvest_year,
            cultivar=fields.cultivar,
            format=fields.format,
            weight_grams=fields.weight_grams,
        )
        session.add(product)
        session.flush()
        return product.product_id

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    _trgm_initialized_sessions: "set[int]"

    def _ensure_trgm_limit(self, session: Session) -> None:
        """``SELECT set_limit(0.85)`` once per session.

        ``set_limit(real)`` is the per-session pg_trgm similarity threshold
        that the ``%`` operator consults; setting it once per session is
        enough because :class:`SilverNormalizer` uses one session per batch
        and runs all matching inside it. Re-setting is harmless but
        wasteful, so we cache by session ``id()``.
        """
        if not hasattr(self, "_trgm_initialized_sessions"):
            self._trgm_initialized_sessions = set()
        sid = id(session)
        if sid in self._trgm_initialized_sessions:
            return
        # ``pg_trgm.set_limit`` is declared as ``set_limit(real)``; psycopg
        # binds Python ``float`` as ``double precision`` which Postgres won't
        # implicitly cast (function-resolution is strict for non-preferred
        # types). Cast at the SQL level rather than fighting the binder.
        session.execute(
            text("SELECT set_limit(CAST(:lim AS real))").bindparams(
                bindparam("lim", TRIGRAM_THRESHOLD)
            )
        )
        self._trgm_initialized_sessions.add(sid)

    def forget_session(self, session: Session) -> None:
        """Drop the session from the trgm-limit cache (called at session end)."""
        if hasattr(self, "_trgm_initialized_sessions"):
            self._trgm_initialized_sessions.discard(id(session))


__all__ = [
    "AMBIGUOUS_GAP",
    "AMBIGUOUS_TOP_CEILING",
    "CanonicalMatcher",
    "ProductDecision",
    "ProductMatchResult",
    "TRIGRAM_THRESHOLD",
    "normalize_name",
]
