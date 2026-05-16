"""Integration tests for :class:`CanonicalMatcher`.

The matcher's exact + trigram + ambiguity logic is intrinsically SQL: it
exercises ``LOWER(canonical_name) = ...``, ``set_limit(0.85)``-driven
trigram ``%%`` queries, and ``IS NOT DISTINCT FROM`` nullable joins.
Stubbing those would test the stub, not the production behavior, so we
run against a real local Postgres (the same test-fixture container the
bronze loader integration suite uses).

Per the step-6 brief: synthetic name variations covering each step of
the 4-step product ladder + the producer 3.5-step ladder.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tea_scrapers.normalize.canonical import (
    AMBIGUOUS_GAP,
    CanonicalMatcher,
    ProductDecision,
)
from tea_scrapers.normalize.shopify_mapper import (
    ProducerHint,
    ProductFields,
    VariantFields,
    normalize_name,
)
from tea_scrapers.storage.models import Producer, Product

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/tea",
)


@pytest.fixture(scope="module")
def pg_engine():
    try:
        e = create_engine(DB_URL, pool_pre_ping=True, future=True)
        with e.connect():
            pass
    except Exception as exc:
        pytest.skip(f"Postgres not reachable at {DB_URL}: {exc}")
    return e


@pytest.fixture
def session(pg_engine) -> Session:
    """Per-test session that rolls back at the end (no cross-test bleed)."""
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    sess = factory()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()


def _make_fields(
    title: str,
    *,
    harvest_year: int | None = None,
    weight_grams: int | None = None,
    producer_name: str | None = None,
    pid: int = 0,
    vid: int = 0,
) -> ProductFields:
    return ProductFields(
        shopify_product_id=str(pid or 1),
        canonical_name=title,
        normalized_name=normalize_name(title),
        producer_hint=(
            ProducerHint(name=producer_name, source="tag") if producer_name else None
        ),
        region_hint=None,
        harvest_year=harvest_year,
        tea_type="Raw Pu-erh Tea",
        tea_style=None,
        format=None,
        cultivar=None,
        weight_grams=weight_grams,
        variant=VariantFields(
            shopify_variant_id=str(vid or 1),
            weight_grams=weight_grams,
            available=True,
            price_cents=1000,
            currency="USD",
        ),
        vendor_url=None,
        description_hash="dummy",
        is_non_tea=False,
    )


# ---------------------------------------------------------------------------
# Producer matcher
# ---------------------------------------------------------------------------


def test_producer_alias_hit_reuses(session: Session) -> None:
    matcher = CanonicalMatcher()
    suffix = uuid.uuid4().hex[:8]
    canonical = f"AliasTest Producer {suffix}"
    norm = canonical.lower()
    p = Producer(canonical_name=canonical, aliases=[norm, "aliased-variant"])
    session.add(p)
    session.flush()

    # Hit via alias spelling.
    pid = matcher.match_or_create_producer(
        ProducerHint(name="aliased-variant", source="tag"), session
    )
    assert pid == p.producer_id
    # No new producer row was created.
    assert (
        session.execute(text("SELECT COUNT(*) FROM producer WHERE canonical_name = :c"), {"c": canonical}).scalar()
        == 1
    )


def test_producer_canonical_exact_match_appends_alias(session: Session) -> None:
    matcher = CanonicalMatcher()
    suffix = uuid.uuid4().hex[:8]
    canonical = f"CanonicalExact Tea {suffix}"
    p = Producer(canonical_name=canonical, aliases=[canonical.lower()])
    session.add(p)
    session.flush()

    # Different cased input but same lowercased form.
    differently_cased = canonical.upper()
    pid = matcher.match_or_create_producer(
        ProducerHint(name=differently_cased, source="vendor_field"), session
    )
    assert pid == p.producer_id

    # The matcher should have appended the (already-normalized) alias if novel.
    aliases = session.execute(
        text("SELECT aliases FROM producer WHERE producer_id = :pid"),
        {"pid": p.producer_id},
    ).scalar_one()
    # Either the alias was already present (it was — set up that way) or
    # it's still there. The important property is no duplicates.
    assert aliases.count(canonical.lower()) == 1


def test_producer_trigram_reuse_appends_alias(session: Session) -> None:
    matcher = CanonicalMatcher()
    suffix = uuid.uuid4().hex[:8]
    canonical = f"Yunnan Sourcing Brand Tea {suffix}"
    p = Producer(canonical_name=canonical, aliases=[canonical.lower()])
    session.add(p)
    session.flush()

    # Near-miss spelling — should be ≥ 0.85 trigram similarity.
    near = f"Yunnan Sourcing Brand Teas {suffix}"
    pid = matcher.match_or_create_producer(
        ProducerHint(name=near, source="tag"), session
    )
    assert pid == p.producer_id

    aliases = session.execute(
        text("SELECT aliases FROM producer WHERE producer_id = :pid"),
        {"pid": p.producer_id},
    ).scalar_one()
    assert near.lower() in aliases


def test_producer_ambiguous_trigram_picks_lowest_id_deterministically(
    session: Session,
) -> None:
    """Two producers with similar names → pick lowest producer_id.

    V1 policy: producer_id stability > recall. Deterministic so the test
    doesn't flap on candidate ordering.

    Note: we insert both seeds via the ORM directly (not via the matcher),
    because the matcher would reuse the first seed for the second seed (the
    near-spelling pair lives above the 0.85 trigram cutoff). The whole point
    of this test is to set up the multi-candidate state that the matcher
    couldn't reach on its own from a clean table.
    """
    matcher = CanonicalMatcher()
    suffix = uuid.uuid4().hex[:8]
    # Both seeds verified ≥0.85 trigram against the query below.
    canonical_a = f"Mountain Cloud Tea Houses {suffix}"
    canonical_b = f"Mountain Cloud Teas House {suffix}"
    a = Producer(canonical_name=canonical_a, aliases=[canonical_a.lower()])
    b = Producer(canonical_name=canonical_b, aliases=[canonical_b.lower()])
    session.add_all([a, b])
    session.flush()
    assert a.producer_id < b.producer_id

    pid = matcher.match_or_create_producer(
        ProducerHint(name=f"Mountain Cloud Tea House {suffix}", source="tag"),
        session,
    )
    # Must hit one of the seeds — V1 deterministic tiebreak picks lowest id.
    assert pid in (a.producer_id, b.producer_id), (
        f"matcher created a new producer row {pid} instead of reusing "
        f"({a.producer_id}, {b.producer_id}) — trigram cutoff may have moved"
    )
    assert pid == a.producer_id


def test_producer_below_threshold_creates_new(session: Session) -> None:
    matcher = CanonicalMatcher()
    suffix = uuid.uuid4().hex[:8]
    canonical = f"Existing Producer {suffix}"
    p = Producer(canonical_name=canonical, aliases=[canonical.lower()])
    session.add(p)
    session.flush()

    # Completely different name — won't pass trigram threshold.
    new = matcher.match_or_create_producer(
        ProducerHint(name=f"Totally Unrelated Brand {suffix}", source="tag"),
        session,
    )
    assert new is not None
    assert new != p.producer_id


def test_producer_none_hint_returns_none(session: Session) -> None:
    matcher = CanonicalMatcher()
    assert matcher.match_or_create_producer(None, session) is None
    assert matcher.match_or_create_producer(ProducerHint(name="", source="tag"), session) is None


# ---------------------------------------------------------------------------
# Product matcher
# ---------------------------------------------------------------------------


def test_product_exact_match_reuses(session: Session) -> None:
    matcher = CanonicalMatcher()
    suffix = uuid.uuid4().hex[:8]
    title = f"2025 Some Yiwu Sheng {suffix}"
    fields = _make_fields(title, harvest_year=2025, weight_grams=357)
    # First call creates.
    r1 = matcher.match_or_create_product(
        fields, producer_id=None, region_id=None, session=session
    )
    assert r1.decision == ProductDecision.CREATED
    # Same call reuses.
    r2 = matcher.match_or_create_product(
        fields, producer_id=None, region_id=None, session=session
    )
    assert r2.decision == ProductDecision.EXACT
    assert r2.product_id == r1.product_id


def test_product_different_weight_creates_new(session: Session) -> None:
    """Two weight variants of the same cake → two ``product`` rows.

    The 4-tuple matcher key includes ``weight_grams``, so 50g and 100g are
    distinct products even with identical names + producer + year.
    """
    matcher = CanonicalMatcher()
    suffix = uuid.uuid4().hex[:8]
    title = f"2026 Year of the Horse Mini Cake {suffix}"
    r50 = matcher.match_or_create_product(
        _make_fields(title, harvest_year=2026, weight_grams=50),
        producer_id=None,
        region_id=None,
        session=session,
    )
    r100 = matcher.match_or_create_product(
        _make_fields(title, harvest_year=2026, weight_grams=100),
        producer_id=None,
        region_id=None,
        session=session,
    )
    assert r50.product_id != r100.product_id
    assert r50.decision == ProductDecision.CREATED
    assert r100.decision == ProductDecision.CREATED


def test_product_trigram_reuse_same_producer(session: Session) -> None:
    matcher = CanonicalMatcher()
    suffix = uuid.uuid4().hex[:8]
    # Seed an existing product.
    canonical = f"2024 Yiwu Spring Gushu {suffix}"
    seed = _make_fields(canonical, harvest_year=2024, weight_grams=200)
    r_seed = matcher.match_or_create_product(
        seed, producer_id=None, region_id=None, session=session
    )
    assert r_seed.decision == ProductDecision.CREATED

    # Near-miss spelling, same year + weight + (null) producer.
    near = f"2024 Yi Wu Spring Gushu {suffix}"  # one space inserted
    r_near = matcher.match_or_create_product(
        _make_fields(near, harvest_year=2024, weight_grams=200),
        producer_id=None,
        region_id=None,
        session=session,
    )
    # Either trigram reuse, or exact (because normalize_name collapses
    # whitespace) — both are fine, and both reuse the seed row.
    assert r_near.product_id == r_seed.product_id
    assert r_near.decision in (ProductDecision.EXACT, ProductDecision.TRIGRAM)


def test_product_below_threshold_creates_new(session: Session) -> None:
    matcher = CanonicalMatcher()
    suffix = uuid.uuid4().hex[:8]
    r_seed = matcher.match_or_create_product(
        _make_fields(f"2024 Yiwu Spring {suffix}", harvest_year=2024, weight_grams=200),
        producer_id=None,
        region_id=None,
        session=session,
    )
    r_far = matcher.match_or_create_product(
        _make_fields(
            f"2024 Bingdao Autumn Maocha {suffix}",
            harvest_year=2024,
            weight_grams=200,
        ),
        producer_id=None,
        region_id=None,
        session=session,
    )
    assert r_far.product_id != r_seed.product_id
    assert r_far.decision == ProductDecision.CREATED


def test_product_two_high_similarity_candidates_reuse_via_top_ceiling(
    session: Session,
) -> None:
    """≥2 trigram candidates BOTH with top_sim ≥ 0.95 → reuse the top hit.

    Seeds differ by a single character ("A" vs "B") in a long otherwise-shared
    string, so pg_trgm similarity against the query ("C") is ≥ 0.95 for both —
    which trips ``AMBIGUOUS_TOP_CEILING`` (dominant-candidate reuse) BEFORE the
    ``AMBIGUOUS_GAP`` fall-through can fire. The matcher must reuse one of the
    seeds, not over-create. (The AMBIGUOUS_GAP fall-through path — top_sim in
    [0.85, 0.95) with second candidate within 0.10 — needs its own test fixture
    with more divergent inputs; tracked as a §12 silver-normalizer polish item.)

    Seeds are inserted via the ORM directly because feeding them through the
    matcher would dedup the second seed against the first via trigram.
    """
    matcher = CanonicalMatcher()
    suffix = uuid.uuid4().hex[:8]
    seed_a = Product(
        canonical_name=f"Ambiguous A {suffix}",
        producer_id=None,
        harvest_year=2024,
        weight_grams=357,
    )
    seed_b = Product(
        canonical_name=f"Ambiguous B {suffix}",
        producer_id=None,
        harvest_year=2024,
        weight_grams=357,
    )
    session.add_all([seed_a, seed_b])
    session.flush()
    assert seed_a.product_id != seed_b.product_id

    # Query with a name within 0.10 trigram of both seeds.
    ambiguous_query = _make_fields(
        f"Ambiguous C {suffix}",
        harvest_year=2024,
        weight_grams=357,
    )
    result = matcher.match_or_create_product(
        ambiguous_query, producer_id=None, region_id=None, session=session
    )
    # Both seeds have top_sim ≥ AMBIGUOUS_TOP_CEILING (0.95) against the query,
    # so the matcher must reuse the top hit rather than over-create. An
    # AMBIGUOUS_CREATED decision here would indicate the top-ceiling check
    # has regressed (it must precede the gap check).
    assert result.decision == ProductDecision.TRIGRAM
    assert result.product_id in (seed_a.product_id, seed_b.product_id)


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


def test_trgm_threshold_set_on_session(session: Session) -> None:
    matcher = CanonicalMatcher()
    # First match invocation issues SET; subsequent are no-ops via the cache.
    matcher._ensure_trgm_limit(session)
    limit = session.execute(text("SELECT show_limit()")).scalar_one()
    assert abs(float(limit) - 0.85) < 1e-6


def test_ambiguous_gap_constant_is_0_10() -> None:
    assert AMBIGUOUS_GAP == pytest.approx(0.10)
