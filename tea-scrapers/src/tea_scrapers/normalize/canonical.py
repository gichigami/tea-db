"""Canonical product + producer ID matching (spec §8).

The product matcher follows the 4-step ladder from §8:

1. Exact match on ``(producer_id, harvest_year, lower(canonical_name),
   weight_grams)`` — using ``IS NOT DISTINCT FROM`` to make nullable
   comparisons match cleanly. Falls back to the suffix-stripped matching
   key (``SUFFIX_EXACT``) so ``"... :: Seattle Inventory"`` joins its base.
1.5 **Skeleton exact** (ROADMAP H.2b, ``TRANSLITERATE_MATCHING_KEYS``):
   within the same producer / year / weight block, the space- and
   punctuation-insensitive pinyin skeleton (:mod:`translit`) of the
   suffix-stripped name equals the query's (``"Bing Dao"`` / ``"Bingdao"``,
   ``"Pu-erh"`` / ``"Puerh"``, ``"老班章"`` / ``"Lao Ban Zhang"``). Unlike
   step 1, the attribute guard runs on skeleton hits (``SKELETON_EXACT``).
   The same block scan implements the optional one-sided year / weight
   relaxation of ``ONE_SIDED_BLOCKING`` (default: off = V1 veto).
2. Trigram similarity ≥0.85 on ``canonical_name`` filtered by
   ``producer_id`` (query text transliterated to pinyin when it contains
   hanzi). Driven by the ``%`` operator with a session-scoped
   ``set_limit(0.85)`` so the GIN trigram index actually fires (a
   ``similarity()`` predicate would not).
2.5 **Attribute guard** (ROADMAP H.2; :mod:`attributes`): each trigram
   candidate whose parsed recipe code, item number (``No.15`` / ``Part 2``
   / ``BP-12``) or sheng/shou conflicts with the query (mismatch, or
   one-sided per ``ONE_SIDED_POLICY``) is vetoed. When both rows have a
   NULL ``harvest_year`` (SQL-blocked equal), the guard compares the
   vintage year parsed from anywhere in the name instead
   (``"Spring 2017 X"`` vs ``"Spring 2018 X"``) — matching-time only, the
   stored column is untouched. If every candidate is vetoed → create
   (``VETO_CREATED``).
3. **Ambiguous** (≥2 candidates within 0.10 of each other) → V1 stub:
   we don't have an LLM tiebreaker yet (ml-engineer V1.5), so we log
   the ambiguity, bump a counter, and **fall through to step 4**.
   Rationale (per data-engineer brief): merging is unrecoverable;
   de-duping an over-created row is a single UPDATE. Asymmetric risk →
   bias toward over-creation in V1.
4. Otherwise create.

The producer matcher is structurally similar but with three differences:

- Alias-first: ``aliases && :keys`` before canonical-name exact, where
  ``:keys`` is the normalized name plus (H.2b) its pinyin key when it
  contains hanzi. Curated aliases are seeded into the same column at
  normalize start (:mod:`producer_aliases`). Then (H.2b) a skeleton step:
  the space/punctuation-insensitive skeleton against every stored alias and
  canonical name — reused only when exactly one producer has it.
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

from tea_scrapers.normalize.attributes import (
    INVENTORY_SUFFIX_PATTERN,
    ONE_SIDED_POLICY,
    WHITESPACE_RUN_PATTERN,
    GuardAttributes,
    OneSidedPolicy,
    check_attribute_guard,
    has_inventory_suffix,
    matching_key,
    strip_inventory_suffix,
)
from tea_scrapers.normalize.shopify_mapper import (
    ProducerHint,
    ProductFields,
    RegionHint,
    normalize_name,
)
from tea_scrapers.normalize.translit import (
    SKELETON_STRIP_SQL_PATTERN,
    has_cjk,
    pinyin_key,
    usable_skeleton,
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

# ROADMAP H.2b part 1 (spec §12 hybrid sub-q #9). Hanzi → pinyin + skeleton
# keys at match time only; stored names are never rewritten. H.1 harness
# (``python -m tea_scrapers.eval.matcher_variants``): product recall at
# P ≥ 0.98 0.310 → 0.406, producer 0.157 → 0.255, zero new false merges.
TRANSLITERATE_MATCHING_KEYS = True

# ROADMAP H.2b part 3 (spec §12 hybrid sub-q #7): what the product matcher
# does when ``harvest_year`` / ``weight_grams`` is known on one side only.
#   "veto"       — V1 ``IS NOT DISTINCT FROM`` blocking (DEFAULT, both).
#   "exact_only" — allow the one-sided pair, but only on a (skeleton-)exact
#                  name hit that is the UNIQUE such row and passes the guard
#                  (``ONE_SIDED_EXACT``); never through trigram.
# Kept at "veto" pending owner review: the evidence for weight
# (12 ``:: Seattle Inventory`` twins whose variant has no parseable weight,
# all labeled low-confidence) is real but thin; see the spec entry.
ONE_SIDED_BLOCKING: dict[str, str] = {
    "harvest_year": "veto",
    "weight_grams": "veto",
}
_ONE_SIDED_BLOCKING_VALUES = frozenset({"veto", "exact_only"})


class ProductDecision(str, Enum):
    EXACT = "exact"
    # Exact on the suffix-stripped matching key (e.g. a ``:: Seattle
    # Inventory`` re-listing joining its base listing). ROADMAP H.2.
    SUFFIX_EXACT = "suffix_exact"
    # Same block, equal pinyin skeleton (space / punctuation / script
    # insensitive), guard passed. ROADMAP H.2b.
    SKELETON_EXACT = "skeleton_exact"
    # Exact / skeleton name hit allowed across a one-sided year or weight by
    # ``ONE_SIDED_BLOCKING = "exact_only"`` (off by default).
    ONE_SIDED_EXACT = "one_sided_exact"
    TRIGRAM = "trigram"
    CREATED = "created"
    AMBIGUOUS_CREATED = "ambiguous_created"
    # Trigram produced candidates but the attribute guard vetoed all of them.
    # A confident non-match, not an ambiguity — does NOT affect exit code.
    VETO_CREATED = "veto_created"


# Decisions that insert a new ``product`` row (for stats bucketing).
CREATED_DECISIONS = frozenset(
    {
        ProductDecision.CREATED,
        ProductDecision.AMBIGUOUS_CREATED,
        ProductDecision.VETO_CREATED,
    }
)


@dataclass(frozen=True)
class GuardVeto:
    """One trigram candidate rejected by the attribute guard."""

    candidate_product_id: int
    similarity: float
    reasons: tuple[str, ...]  # e.g. ("style:mismatch",)


@dataclass
class ProductMatchResult:
    product_id: int
    decision: ProductDecision
    guard_vetoes: tuple[GuardVeto, ...] = ()


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

    def __init__(
        self,
        log: structlog.stdlib.BoundLogger | None = None,
        *,
        transliterate: bool | None = None,
        one_sided_blocking: dict[str, str] | None = None,
    ) -> None:
        self._log = log or structlog.get_logger(__name__)
        self._producer_cache: dict[str, int] = {}
        self._region_cache: dict[tuple[str | None, ...], int] = {}
        self._translit = (
            TRANSLITERATE_MATCHING_KEYS if transliterate is None else transliterate
        )
        blocking = {**ONE_SIDED_BLOCKING, **(one_sided_blocking or {})}
        bad = {k: v for k, v in blocking.items() if v not in _ONE_SIDED_BLOCKING_VALUES}
        if bad or set(blocking) != set(ONE_SIDED_BLOCKING):
            raise ValueError(f"invalid one_sided_blocking: {blocking!r}")
        self._one_sided = blocking
        # product_id → block key of its suffix-stripped canonical_name.
        # Names are immutable once created, so this never goes stale.
        self._block_key_cache: dict[int, str | None] = {}

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

        # Step 1: alias hit. ``aliases`` is TEXT[] of normalized forms (plus
        # the pinyin key of any hanzi alias), so array overlap is a direct
        # comparison without per-row normalize.
        keys = self._producer_keys(raw, norm)
        row = session.execute(
            text(
                "SELECT producer_id, canonical_name, aliases "
                "FROM producer WHERE aliases && CAST(:keys AS text[]) "
                "ORDER BY producer_id ASC LIMIT 1"
            ),
            {"keys": keys},
        ).first()
        if row is not None:
            self._maybe_append_alias(session, row.producer_id, row.aliases, keys)
            self._producer_cache[norm] = row.producer_id
            return row.producer_id

        # Step 2: canonical-name exact (lowercased compare).
        row = session.execute(
            text(
                "SELECT producer_id, canonical_name, aliases FROM producer "
                "WHERE LOWER(canonical_name) = ANY(CAST(:keys AS text[])) "
                "ORDER BY producer_id ASC LIMIT 1"
            ),
            {"keys": keys},
        ).first()
        if row is not None:
            self._maybe_append_alias(session, row.producer_id, row.aliases, keys)
            self._producer_cache[norm] = row.producer_id
            return row.producer_id

        # Step 2.5 (H.2b): skeleton exact against every stored alias and
        # canonical name. Stored aliases are lower-cased already; the SQL
        # strip pattern is shared with ``translit.skeleton_key``. Reused only
        # when exactly one producer carries the skeleton.
        skel = usable_skeleton(raw) if self._translit else None
        if skel is not None:
            rows = session.execute(
                text(
                    "SELECT producer_id, canonical_name, aliases FROM producer p "
                    "WHERE regexp_replace(LOWER(p.canonical_name), :pat, '', 'g') = :skel "
                    "   OR EXISTS (SELECT 1 FROM unnest(p.aliases) AS a "
                    "              WHERE regexp_replace(a, :pat, '', 'g') = :skel) "
                    "ORDER BY producer_id ASC LIMIT 2"
                ),
                {"pat": SKELETON_STRIP_SQL_PATTERN, "skel": skel},
            ).all()
            if len(rows) == 1:
                top = rows[0]
                self._log.info(
                    "producer.skeleton_match", raw=raw, producer_id=top.producer_id
                )
                self._maybe_append_alias(session, top.producer_id, top.aliases, keys)
                self._producer_cache[norm] = top.producer_id
                return top.producer_id
            if rows:
                self._log.info(
                    "producer.skeleton_ambiguous",
                    raw=raw,
                    candidates=[(r.producer_id, r.canonical_name) for r in rows],
                )

        # Step 3: trigram similarity ≥ threshold. Index-driven via ``%``.
        # A hanzi name is scored on its pinyin key (H.2b).
        trgm_name = pinyin_key(raw) if self._translit and has_cjk(raw) else raw
        rows = session.execute(
            text(
                "SELECT producer_id, canonical_name, aliases, "
                "similarity(canonical_name, :name) AS sim "
                "FROM producer WHERE canonical_name % :name "
                "ORDER BY sim DESC, producer_id ASC LIMIT 5"
            ),
            {"name": trgm_name},
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
            self._maybe_append_alias(session, top.producer_id, top.aliases, keys)
            self._producer_cache[norm] = top.producer_id
            return top.producer_id

        # Step 4: create.
        producer = Producer(canonical_name=raw, aliases=keys)
        session.add(producer)
        session.flush()
        self._producer_cache[norm] = producer.producer_id
        return producer.producer_id

    def _producer_keys(self, raw: str, norm: str) -> list[str]:
        """Normalized name + (H.2b) its pinyin key when it contains hanzi."""
        keys = [norm]
        if self._translit and has_cjk(raw):
            py = pinyin_key(raw)
            if py and py not in keys:
                keys.append(py)
        return keys

    def _maybe_append_alias(
        self,
        session: Session,
        producer_id: int,
        existing_aliases: list[str] | None,
        new_aliases: list[str],
    ) -> None:
        """Append any of ``new_aliases`` not already in ``producer.aliases``."""
        current = list(existing_aliases or [])
        new = [a for a in new_aliases if a not in current]
        if not new:
            return
        session.execute(
            text(
                "UPDATE producer SET aliases = COALESCE(aliases, ARRAY[]::TEXT[]) "
                "|| CAST(:new AS text[]) WHERE producer_id = :pid"
            ),
            {"new": new, "pid": producer_id},
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
        """Match the (producer, year, name, weight) tuple → existing or new product.

        Matching uses :func:`attributes.matching_key` (inventory suffix such
        as ``" :: Seattle Inventory"`` stripped) on BOTH sides; the stored
        ``canonical_name`` of a created row is still the raw vendor title.
        """
        sfx = INVENTORY_SUFFIX_PATTERN
        key = matching_key(fields.canonical_name)
        match_name = strip_inventory_suffix(fields.canonical_name)

        # Step 1: exact 4-tuple.
        # ``IS NOT DISTINCT FROM`` treats NULL == NULL as true. Required
        # because producer_id / harvest_year / weight_grams may all be NULL.
        # Second OR-arm: suffix-stripped, whitespace-collapsed key on the
        # stored side (mirrors ``matching_key``; the suffix regex string is
        # shared with Python). Plain hits win the ORDER BY so pre-H.2
        # behavior is unchanged whenever a plain exact row exists.
        row = session.execute(
            text(
                "SELECT product_id, (LOWER(canonical_name) = :norm) AS plain, "
                "  (canonical_name ~* :sfx) AS stored_has_suffix "
                "FROM product "
                "WHERE producer_id IS NOT DISTINCT FROM :producer_id "
                "  AND harvest_year IS NOT DISTINCT FROM :harvest_year "
                "  AND weight_grams IS NOT DISTINCT FROM :weight_grams "
                "  AND (LOWER(canonical_name) = :norm "
                "       OR btrim(regexp_replace(LOWER(regexp_replace("
                "            canonical_name, :sfx, '', 'i')), :ws, ' ', 'g')) = :key) "
                "ORDER BY plain DESC, product_id ASC LIMIT 1"
            ),
            {
                "producer_id": producer_id,
                "harvest_year": fields.harvest_year,
                "norm": fields.normalized_name,
                "weight_grams": fields.weight_grams,
                "sfx": sfx,
                "ws": WHITESPACE_RUN_PATTERN,
                "key": key,
            },
        ).first()
        if row is not None:
            # SUFFIX_EXACT only when a suffix was actually involved on either
            # side. A non-plain hit without any suffix is a whitespace-only
            # difference (stored "Yi  Wu ..." double space vs the collapsed
            # :norm) — that's an ordinary exact match on the normalized name.
            suffix_involved = row.stored_has_suffix or has_inventory_suffix(
                fields.canonical_name
            )
            if row.plain or not suffix_involved:
                return ProductMatchResult(
                    product_id=row.product_id, decision=ProductDecision.EXACT
                )
            self._log.info(
                "silver_match_suffix_stripped",
                name=fields.canonical_name,
                producer_id=producer_id,
                product_id=row.product_id,
            )
            return ProductMatchResult(
                product_id=row.product_id, decision=ProductDecision.SUFFIX_EXACT
            )

        # Step 1.5 (H.2b): skeleton exact + optional one-sided relaxation.
        block_hit = self._match_in_block(fields, producer_id=producer_id, session=session)
        if block_hit is not None:
            return block_hit

        # Step 2: trigram on canonical_name filtered by producer. Producer is
        # nullable; if we have no producer hint, restrict to producer-less
        # rows so we don't cross-pollinate against rows with strong producer
        # identity (e.g. "Mountain Shadow" by Crimson Lotus must not match
        # a producer-less name collision from a different vendor's catalog).
        #
        # Similarity is computed on the suffix-stripped stored name against
        # the suffix-stripped query. The WHERE keeps a GIN-indexable
        # superset (``%`` OR the suffix regex, both ``gin_trgm_ops``-able)
        # and then rechecks ``%`` on the stripped expression.
        self._ensure_trgm_limit(session)

        producer_clause = (
            "producer_id = :producer_id" if producer_id is not None else "producer_id IS NULL"
        )
        sim_rows = session.execute(
            text(
                "SELECT product_id, canonical_name, harvest_year, weight_grams, "
                "  similarity(regexp_replace(canonical_name, :sfx, '', 'i'), :name) AS sim "
                "FROM product "
                f"WHERE {producer_clause} "
                "  AND (canonical_name % :name OR canonical_name ~* :sfx) "
                "  AND regexp_replace(canonical_name, :sfx, '', 'i') % :name "
                "  AND harvest_year IS NOT DISTINCT FROM :harvest_year "
                "  AND weight_grams IS NOT DISTINCT FROM :weight_grams "
                "ORDER BY sim DESC, product_id ASC LIMIT 5"
            ),
            {
                "producer_id": producer_id,
                "name": (
                    pinyin_key(match_name)
                    if self._translit and has_cjk(match_name)
                    else match_name
                ),
                "sfx": sfx,
                "harvest_year": fields.harvest_year,
                "weight_grams": fields.weight_grams,
            },
        ).all()

        # Step 2.5: attribute guard (spec §12 hybrid step 2). Any parsed-and-
        # conflicting recipe code / item number / sheng-shou vetoes the
        # candidate before ambiguity / reuse is considered. Year / weight are
        # already SQL-blocked here (the guard is shared with the hybrid path),
        # EXCEPT when both harvest_years are NULL: ``from_name`` then falls
        # back to the name-parsed vintage year, which can still conflict.
        vetoes: list[GuardVeto] = []
        if sim_rows:
            query_attrs = GuardAttributes.from_name(
                fields.canonical_name,
                harvest_year=fields.harvest_year,
                weight_grams=fields.weight_grams,
            )
            surviving = []
            for r in sim_rows:
                verdict = check_attribute_guard(
                    query_attrs,
                    GuardAttributes.from_name(
                        r.canonical_name,
                        harvest_year=r.harvest_year,
                        weight_grams=r.weight_grams,
                    ),
                )
                if verdict.vetoed:
                    vetoes.append(
                        GuardVeto(
                            candidate_product_id=r.product_id,
                            similarity=float(r.sim),
                            reasons=tuple(c.reason for c in verdict.conflicts),
                        )
                    )
                else:
                    surviving.append(r)
            if vetoes:
                self._log.info(
                    "silver_match_guard_veto",
                    name=fields.canonical_name,
                    producer_id=producer_id,
                    vetoed=[
                        (v.candidate_product_id, round(v.similarity, 4), list(v.reasons))
                        for v in vetoes
                    ],
                    surviving=len(surviving),
                )
            if not surviving:
                created = self._create_product(
                    fields,
                    producer_id=producer_id,
                    region_id=region_id,
                    session=session,
                )
                return ProductMatchResult(
                    product_id=created,
                    decision=ProductDecision.VETO_CREATED,
                    guard_vetoes=tuple(vetoes),
                )
            sim_rows = surviving

        if sim_rows:
            top = sim_rows[0]
            top_sim = float(top.sim)
            # Step 3: ambiguity check — log + fall through to over-create.
            # (V1 LLM tiebreaker stub: see §12 OQ.) Only guard-surviving
            # candidates count toward ambiguity.
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
                    product_id=created,
                    decision=ProductDecision.AMBIGUOUS_CREATED,
                    guard_vetoes=tuple(vetoes),
                )
            # Single dominant candidate — reuse.
            return ProductMatchResult(
                product_id=top.product_id,
                decision=ProductDecision.TRIGRAM,
                guard_vetoes=tuple(vetoes),
            )

        # Step 4: create.
        created = self._create_product(
            fields,
            producer_id=producer_id,
            region_id=region_id,
            session=session,
        )
        return ProductMatchResult(product_id=created, decision=ProductDecision.CREATED)

    def _block_key(self, name: str) -> str | None:
        stripped = strip_inventory_suffix(name)
        if self._translit:
            return usable_skeleton(stripped)
        return matching_key(stripped) or None

    def _match_in_block(
        self,
        fields: ProductFields,
        *,
        producer_id: int | None,
        session: Session,
    ) -> ProductMatchResult | None:
        """Step 1.5: Python-side key comparison within the producer block.

        Only the skeleton needs Python (pinyin cannot run in SQL), so the
        block — same producer, and year / weight equal or (if relaxed by
        ``ONE_SIDED_BLOCKING``) one-sided — is fetched and compared here.
        Blocks are small (a producer's rows for one vintage and size).
        """
        relax_year = self._one_sided["harvest_year"] == "exact_only"
        relax_weight = self._one_sided["weight_grams"] == "exact_only"
        if not self._translit and not (relax_year or relax_weight):
            return None
        qkey = self._block_key(fields.canonical_name)
        if qkey is None:
            return None

        def pred(col: str, param: str, relaxed: bool) -> str:
            base = f"{col} IS NOT DISTINCT FROM :{param}"
            if not relaxed:
                return base
            return f"({base} OR {col} IS NULL OR CAST(:{param} AS integer) IS NULL)"

        producer_clause = (
            "producer_id = :producer_id" if producer_id is not None else "producer_id IS NULL"
        )
        rows = session.execute(
            text(
                "SELECT product_id, canonical_name, harvest_year, weight_grams FROM product "
                f"WHERE {producer_clause} "
                f"  AND {pred('harvest_year', 'harvest_year', relax_year)} "
                f"  AND {pred('weight_grams', 'weight_grams', relax_weight)} "
                "ORDER BY product_id ASC"
            ),
            {
                "producer_id": producer_id,
                "harvest_year": fields.harvest_year,
                "weight_grams": fields.weight_grams,
            },
        ).all()
        strict_hits, relaxed_hits = [], []
        for r in rows:
            if r.product_id not in self._block_key_cache:
                self._block_key_cache[r.product_id] = self._block_key(r.canonical_name)
            if self._block_key_cache[r.product_id] != qkey:
                continue
            one_sided = (
                r.harvest_year != fields.harvest_year or r.weight_grams != fields.weight_grams
            )
            (relaxed_hits if one_sided else strict_hits).append(r)

        query_attrs = GuardAttributes.from_name(
            fields.canonical_name,
            harvest_year=fields.harvest_year,
            weight_grams=fields.weight_grams,
        )

        def guarded(cands: list, policy: dict[str, OneSidedPolicy] | None) -> list:
            return [
                r
                for r in cands
                if not check_attribute_guard(
                    query_attrs,
                    GuardAttributes.from_name(
                        r.canonical_name,
                        harvest_year=r.harvest_year,
                        weight_grams=r.weight_grams,
                    ),
                    policy=policy,
                ).vetoed
            ]

        if self._translit:
            ok = guarded(strict_hits, None)
            if ok:
                self._log.info(
                    "silver_match_skeleton",
                    name=fields.canonical_name,
                    producer_id=producer_id,
                    product_id=ok[0].product_id,
                )
                return ProductMatchResult(
                    product_id=ok[0].product_id, decision=ProductDecision.SKELETON_EXACT
                )
        if relaxed_hits and not strict_hits:
            policy = {
                **ONE_SIDED_POLICY,
                **({"harvest_year": OneSidedPolicy.PASS} if relax_year else {}),
                **({"weight_grams": OneSidedPolicy.PASS} if relax_weight else {}),
            }
            ok = guarded(relaxed_hits, policy)
            if len(ok) == 1:
                self._log.info(
                    "silver_match_one_sided_exact",
                    name=fields.canonical_name,
                    producer_id=producer_id,
                    product_id=ok[0].product_id,
                )
                return ProductMatchResult(
                    product_id=ok[0].product_id, decision=ProductDecision.ONE_SIDED_EXACT
                )
            if len(ok) > 1:
                self._log.info(
                    "silver_match_one_sided_ambiguous",
                    name=fields.canonical_name,
                    candidates=[r.product_id for r in ok],
                )
        return None

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
    "CREATED_DECISIONS",
    "ONE_SIDED_BLOCKING",
    "TRANSLITERATE_MATCHING_KEYS",
    "TRIGRAM_THRESHOLD",
    "CanonicalMatcher",
    "GuardVeto",
    "ProductDecision",
    "ProductMatchResult",
    "normalize_name",
]
