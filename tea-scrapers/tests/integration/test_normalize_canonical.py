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

from tea_scrapers.normalize.attributes import ONE_SIDED_POLICY, OneSidedPolicy
from tea_scrapers.normalize.canonical import (
    AMBIGUOUS_GAP,
    AMBIGUOUS_TOP_CEILING,
    TRIGRAM_THRESHOLD,
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


def _digits() -> str:
    """Per-test unique token made of digits only.

    A hex ``uuid`` suffix can share letter trigrams with the single-letter
    words these fixtures vary on ("A" / "B" / "C"), which made the
    pre-H.2 top-ceiling test flaky (sim flipped across 0.85 depending on
    the suffix's first hex char). Digit-only words cannot collide.
    """
    return str(uuid.uuid4().int % 10**10).zfill(10)


def _sim(session: Session, a: str, b: str) -> float:
    return float(session.execute(text("SELECT similarity(:a, :b)"), {"a": a, "b": b}).scalar_one())


def test_product_two_high_similarity_candidates_reuse_via_top_ceiling(
    session: Session,
) -> None:
    """≥2 trigram candidates BOTH with top_sim ≥ 0.95 → reuse the top hit.

    Seeds differ by a single character ("A" vs "B") in a long otherwise-shared
    string, so pg_trgm similarity against the query ("C") is ≥ 0.95 for both —
    which trips ``AMBIGUOUS_TOP_CEILING`` (dominant-candidate reuse) BEFORE the
    ``AMBIGUOUS_GAP`` fall-through can fire. The matcher must reuse one of the
    seeds, not over-create.

    Seeds are inserted via the ORM directly because feeding them through the
    matcher would dedup the second seed against the first via trigram.
    """
    matcher = CanonicalMatcher()
    tok = _digits()
    stem = "Ambiguous Ceiling Shared Long Product Name Yiwu Gushu Spring Harvest Selection"
    seed_a = Product(canonical_name=f"{stem} A {tok}", harvest_year=2024, weight_grams=357)
    seed_b = Product(canonical_name=f"{stem} B {tok}", harvest_year=2024, weight_grams=357)
    session.add_all([seed_a, seed_b])
    session.flush()
    query = f"{stem} C {tok}"
    # Precondition: both seeds clear the ceiling (self-validating fixture).
    assert _sim(session, seed_a.canonical_name, query) >= AMBIGUOUS_TOP_CEILING
    assert _sim(session, seed_b.canonical_name, query) >= AMBIGUOUS_TOP_CEILING

    result = matcher.match_or_create_product(
        _make_fields(query, harvest_year=2024, weight_grams=357),
        producer_id=None,
        region_id=None,
        session=session,
    )
    # An AMBIGUOUS_CREATED decision here would indicate the top-ceiling check
    # has regressed (it must precede the gap check).
    assert result.decision == ProductDecision.TRIGRAM
    assert result.product_id in (seed_a.product_id, seed_b.product_id)


def test_product_ambiguous_gap_falls_through_to_over_create(session: Session) -> None:
    """top_sim in [0.85, 0.95) and runner-up within AMBIGUOUS_GAP → over-create.

    Fills the §12 silver-normalizer polish #7 gap: the V1 LLM-tiebreaker stub
    path (``AMBIGUOUS_CREATED``) had no test.
    """
    matcher = CanonicalMatcher()
    tok = _digits()
    seed_a = Product(
        canonical_name=f"Gap Yiwu Spring Selection A {tok}", harvest_year=2024, weight_grams=357
    )
    seed_b = Product(
        canonical_name=f"Gap Yiwu Spring Selection B {tok}", harvest_year=2024, weight_grams=357
    )
    session.add_all([seed_a, seed_b])
    session.flush()
    query = f"Gap Yiwu Spring Selection C {tok}"
    sims = sorted(
        (_sim(session, seed_a.canonical_name, query), _sim(session, seed_b.canonical_name, query)),
        reverse=True,
    )
    assert TRIGRAM_THRESHOLD <= sims[1] <= sims[0] < AMBIGUOUS_TOP_CEILING
    assert sims[0] - sims[1] < AMBIGUOUS_GAP

    result = matcher.match_or_create_product(
        _make_fields(query, harvest_year=2024, weight_grams=357),
        producer_id=None,
        region_id=None,
        session=session,
    )
    assert result.decision == ProductDecision.AMBIGUOUS_CREATED
    assert result.product_id not in (seed_a.product_id, seed_b.product_id)
    assert result.guard_vetoes == ()


# ---------------------------------------------------------------------------
# Attribute guard (ROADMAP H.2)
# ---------------------------------------------------------------------------


def _seed_producer(session: Session) -> int:
    name = f"Guard Test Producer {uuid.uuid4().hex[:8]}"
    p = Producer(canonical_name=name, aliases=[name.lower()])
    session.add(p)
    session.flush()
    return p.producer_id


def _seed_product(
    session: Session, name: str, *, producer_id: int | None, year: int | None, weight: int | None
) -> Product:
    p = Product(
        canonical_name=name, producer_id=producer_id, harvest_year=year, weight_grams=weight
    )
    session.add(p)
    session.flush()
    return p


def test_guard_vetoes_sheng_shou_mismatch(session: Session) -> None:
    """Real YS pair (cassette): same name except Raw vs Ripe, trigram ≈0.90.

    Pre-H.2 the trigram step merged these. The guard must veto and create.
    """
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    raw = _seed_product(
        session,
        f'2024 Yunnan Sourcing "Bang Wai Mountain {tok}" Raw Pu-erh Tea Cake',
        producer_id=pid,
        year=2024,
        weight=357,
    )
    query = f'2024 Yunnan Sourcing "Bang Wai Mountain {tok}" Ripe Pu-erh Tea Cake'
    assert _sim(session, raw.canonical_name, query) >= TRIGRAM_THRESHOLD

    result = matcher.match_or_create_product(
        _make_fields(query, harvest_year=2024, weight_grams=357),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert result.decision == ProductDecision.VETO_CREATED
    assert result.product_id != raw.product_id
    assert [(v.candidate_product_id, v.reasons) for v in result.guard_vetoes] == [
        (raw.product_id, ("style:mismatch",))
    ]
    # The raw vendor title is what got stored.
    assert session.get(Product, result.product_id).canonical_name == query


def test_guard_vetoes_recipe_code_mismatch(session: Session) -> None:
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    seed = _seed_product(
        session,
        f'2022 Menghai "7572" Ripe Pu-erh Tea Cake {tok}',
        producer_id=pid,
        year=2022,
        weight=357,
    )
    query = f'2022 Menghai "7542" Ripe Pu-erh Tea Cake {tok}'
    assert _sim(session, seed.canonical_name, query) >= TRIGRAM_THRESHOLD

    result = matcher.match_or_create_product(
        _make_fields(query, harvest_year=2022, weight_grams=357),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert result.decision == ProductDecision.VETO_CREATED
    assert result.guard_vetoes[0].reasons == ("recipe_code:mismatch",)


def test_guard_veto_removes_candidate_before_ambiguity_check(session: Session) -> None:
    """Without the guard these two candidates are ambiguous (gap < 0.10).

    The Raw seed is vetoed, leaving one Ripe candidate → reuse (TRIGRAM).
    """
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    ripe = _seed_product(
        session,
        f'2024 Yunnan Sourcing "Bang Wai Mountain {tok}" Ripe Puerh Tea Cake',
        producer_id=pid,
        year=2024,
        weight=357,
    )
    raw = _seed_product(
        session,
        f'2024 Yunnan Sourcing "Bang Wai Mountain {tok}" Raw Pu-erh Tea Cake',
        producer_id=pid,
        year=2024,
        weight=357,
    )
    query = f'2024 Yunnan Sourcing "Bang Wai Mountain {tok}" Ripe Pu-erh Tea Cake'
    s_ripe = _sim(session, ripe.canonical_name, query)
    s_raw = _sim(session, raw.canonical_name, query)
    assert TRIGRAM_THRESHOLD <= s_raw < s_ripe < AMBIGUOUS_TOP_CEILING
    assert s_ripe - s_raw < AMBIGUOUS_GAP  # would be AMBIGUOUS_CREATED pre-guard

    result = matcher.match_or_create_product(
        _make_fields(query, harvest_year=2024, weight_grams=357),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert result.decision == ProductDecision.TRIGRAM
    assert result.product_id == ripe.product_id
    assert [v.candidate_product_id for v in result.guard_vetoes] == [raw.product_id]


def test_guard_one_sided_style_vetoes_by_default(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One side says Ripe, the other is silent → veto under the default policy.

    Flipping ``ONE_SIDED_POLICY["style"]`` to PASS lets the same pair reuse,
    proving the module constant is the single knob (spec §12 sub-q #7).
    """
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    seed = _seed_product(
        session,
        f"2019 Menghai Classic {tok} Pu-erh Tea Cake",
        producer_id=pid,
        year=2019,
        weight=357,
    )
    query = f"2019 Menghai Classic {tok} Ripe Pu-erh Tea Cake"
    assert _sim(session, seed.canonical_name, query) >= TRIGRAM_THRESHOLD
    fields = _make_fields(query, harvest_year=2019, weight_grams=357)

    vetoed = matcher.match_or_create_product(
        fields, producer_id=pid, region_id=None, session=session
    )
    assert vetoed.decision == ProductDecision.VETO_CREATED
    assert vetoed.guard_vetoes[0].reasons == ("style:one_sided",)
    session.delete(session.get(Product, vetoed.product_id))
    session.flush()

    monkeypatch.setitem(ONE_SIDED_POLICY, "style", OneSidedPolicy.PASS)
    passed = matcher.match_or_create_product(
        fields, producer_id=pid, region_id=None, session=session
    )
    assert passed.decision == ProductDecision.TRIGRAM
    assert passed.product_id == seed.product_id


# ---------------------------------------------------------------------------
# Inventory-suffix near-duplicates (ROADMAP H.2)
# ---------------------------------------------------------------------------


def test_seattle_inventory_listing_matches_base_listing(session: Session) -> None:
    """Base first, then ``:: Seattle Inventory`` twin → SUFFIX_EXACT reuse."""
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    base = f'2025 "Mountain Shadow {tok}" Shou / Ripe Puerh Tea - 200g Cake'
    r_base = matcher.match_or_create_product(
        _make_fields(base, harvest_year=2025, weight_grams=200),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert r_base.decision == ProductDecision.CREATED

    suffixed = base + " :: Seattle Inventory"
    r_sfx = matcher.match_or_create_product(
        _make_fields(suffixed, harvest_year=2025, weight_grams=200),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert r_sfx.decision == ProductDecision.SUFFIX_EXACT
    assert r_sfx.product_id == r_base.product_id
    # Stored name is untouched (raw title preserved).
    assert session.get(Product, r_base.product_id).canonical_name == base


def test_base_listing_matches_earlier_seattle_listing(session: Session) -> None:
    """Reverse order: suffixed row already stored, base arrives → reuse it."""
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    # Real double-space-before-:: shape from the cassette.
    suffixed = f"2023 Bangdong Old Tree {tok} Sheng / Raw Puerh Tea  :: Seattle Inventory"
    r_sfx = matcher.match_or_create_product(
        _make_fields(suffixed, harvest_year=2023, weight_grams=200),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert r_sfx.decision == ProductDecision.CREATED
    assert session.get(Product, r_sfx.product_id).canonical_name == suffixed

    r_base = matcher.match_or_create_product(
        _make_fields(
            f"2023 Bangdong Old Tree {tok} Sheng / Raw Puerh Tea",
            harvest_year=2023,
            weight_grams=200,
        ),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert r_base.decision == ProductDecision.SUFFIX_EXACT
    assert r_base.product_id == r_sfx.product_id


def test_whitespace_only_difference_is_exact_not_suffix_exact(session: Session) -> None:
    """Review nit #1: a stored double-space title with no suffix must be EXACT.

    Stored ``LOWER(canonical_name)`` is not whitespace-collapsed while
    ``:norm`` is, so ``plain`` is false — but no suffix is involved on either
    side, so this must not be labelled SUFFIX_EXACT.
    """
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    stored = _seed_product(
        session,
        f'Yi  Wu Mountain "Cha Gao {tok}" Instant Ripe Pu-erh Tea Resin',
        producer_id=pid,
        year=None,
        weight=100,
    )
    result = matcher.match_or_create_product(
        _make_fields(
            f'Yi Wu Mountain "Cha Gao {tok}" Instant Ripe Pu-erh Tea Resin', weight_grams=100
        ),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert result.decision == ProductDecision.EXACT
    assert result.product_id == stored.product_id


def test_nbsp_suffix_stripped_on_stored_side(session: Session) -> None:
    """Review nit #4: the shared regex strips an NBSP-spaced suffix in Postgres too."""
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    stored = _seed_product(
        session,
        f"2023 Bangdong Old Tree {tok} Sheng / Raw Puerh Tea\u00a0::\u00a0Seattle Inventory",
        producer_id=pid,
        year=2023,
        weight=200,
    )
    result = matcher.match_or_create_product(
        _make_fields(
            f"2023 Bangdong Old Tree {tok} Sheng / Raw Puerh Tea",
            harvest_year=2023,
            weight_grams=200,
        ),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert result.decision == ProductDecision.SUFFIX_EXACT
    assert result.product_id == stored.product_id


def test_suffix_stripped_before_trigram_similarity(session: Session) -> None:
    """Suffixed seed + near-miss base query: raw sim ≈0.74 (miss), stripped ≈0.95."""
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    seed = _seed_product(
        session,
        f'2025 "Mountain Shadow {tok}" Shou / Ripe Puerh Tea - 200g Cake :: Seattle Inventory',
        producer_id=pid,
        year=2025,
        weight=200,
    )
    query = f'2025 "Mountain Shadow {tok}" Shou / Ripe Puerh Tea - 200g Cakes'
    assert _sim(session, seed.canonical_name, query) < TRIGRAM_THRESHOLD  # pre-H.2 miss

    result = matcher.match_or_create_product(
        _make_fields(query, harvest_year=2025, weight_grams=200),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert result.decision == ProductDecision.TRIGRAM
    assert result.product_id == seed.product_id


def test_suffix_match_still_blocked_by_weight(session: Session) -> None:
    """Stripping the suffix never bypasses the (year, weight) block."""
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    base = f'2025 "Mountain Shadow {tok}" Shou / Ripe Puerh Tea - 200g Cake'
    r20 = matcher.match_or_create_product(
        _make_fields(base, harvest_year=2025, weight_grams=20),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    r200 = matcher.match_or_create_product(
        _make_fields(base + " :: Seattle Inventory", harvest_year=2025, weight_grams=200),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert r200.decision == ProductDecision.CREATED
    assert r200.product_id != r20.product_id


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


# ---------------------------------------------------------------------------
# Veto visibility in normalize run stats (ROADMAP H.2)
# ---------------------------------------------------------------------------


class _NullTracker:
    def record_success(self, n: int = 1) -> None:
        pass

    def record_error(self, msg: str) -> None:
        pass


def test_guard_vetoes_surface_in_normalize_stats(session: Session) -> None:
    """Raw then Ripe twin through the silver runner → veto counted in stats."""
    import contextlib
    import datetime as dt

    from tea_scrapers.normalize.silver import NormalizeStats, SilverNormalizer
    from tea_scrapers.storage.models import RawProductSnapshot

    tok = _digits()
    source = f"guard_stats_{tok}"
    normalizer = SilverNormalizer(
        since=dt.date(2026, 1, 1),
        tracker=_NullTracker(),  # type: ignore[arg-type]
        run_id="test",
        session_factory=lambda: contextlib.nullcontext(session),
        vendor_configs={},
    )

    def _row(pid: int, style: str) -> RawProductSnapshot:
        return RawProductSnapshot(
            source=source,
            external_id=str(pid),
            scraped_at=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
            run_id="test",
            payload_hash=f"h{pid}",
            payload={
                "id": pid,
                "title": f'2024 Yunnan Sourcing "Bang Wai Mountain {tok}" {style} Pu-erh Tea Cake',
                "vendor": f"Guard Stats Producer {tok}",
                "product_type": f"{style} Pu-erh Tea",
                "tags": [],
                "variants": [
                    {"id": pid * 10, "option1": "357 Grams", "available": True, "price": "50.00"}
                ],
            },
        )

    stats = NormalizeStats()
    normalizer._process_bronze_row(_row(1, "Raw"), stats, session)
    normalizer._process_bronze_row(_row(2, "Ripe"), stats, session)

    assert stats.by_decision[ProductDecision.CREATED.value] == 1
    assert stats.by_decision[ProductDecision.VETO_CREATED.value] == 1
    assert stats.inserted_product == 2
    assert stats.guard_vetoes == 1
    assert stats.guard_vetoes_by_reason == {"style:mismatch": 1}
    assert stats.by_source[source]["guard_vetoes"] == 1


# ---------------------------------------------------------------------------
# H.2 follow-up (H.1 evidence): name-year fallback + item-number guard
# ---------------------------------------------------------------------------


def test_guard_name_year_fallback_vetoes_when_both_years_null(session: Session) -> None:
    """Real CL pair (H.1 prod-0204): the year is not at title start, so the
    mapper stores harvest_year=NULL on both and the SQL year block passes.
    Trigram ≈0.94 merged them pre-fix; the name-year fallback must veto."""
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    seed = _seed_product(
        session,
        f'Spring 2017 "Slumbering Dragon {tok}" Sheng / Raw Puerh from Crimson Lotus Tea',
        producer_id=pid,
        year=None,
        weight=200,
    )
    query = f'Spring 2018 "Slumbering Dragon {tok}" Sheng / Raw Puerh from Crimson Lotus Tea'
    assert _sim(session, seed.canonical_name, query) >= TRIGRAM_THRESHOLD

    result = matcher.match_or_create_product(
        _make_fields(query, harvest_year=None, weight_grams=200),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert result.decision == ProductDecision.VETO_CREATED
    assert [(v.candidate_product_id, v.reasons) for v in result.guard_vetoes] == [
        (seed.product_id, ("harvest_year:mismatch",))
    ]
    # Matching-time guard only: the stored column keeps the mapper's NULL.
    assert session.get(Product, result.product_id).harvest_year is None


def test_guard_name_year_fallback_same_year_still_reuses(session: Session) -> None:
    """Control: same mid-title year, spelling-only difference → trigram reuse."""
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    seed = _seed_product(
        session,
        f'Spring 2017 "Slumbering Dragon {tok}" Sheng / Raw Puerh from Crimson Lotus Tea',
        producer_id=pid,
        year=None,
        weight=200,
    )
    query = f'Spring 2017 "Slumbering Dragon {tok}" Sheng / Raw Pu-erh from Crimson Lotus Tea'
    assert _sim(session, seed.canonical_name, query) >= TRIGRAM_THRESHOLD

    result = matcher.match_or_create_product(
        _make_fields(query, harvest_year=None, weight_grams=200),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert result.decision == ProductDecision.TRIGRAM
    assert result.product_id == seed.product_id
    assert result.guard_vetoes == ()


def test_guard_item_number_mismatch_vetoes_sampler_parts(session: Session) -> None:
    """Real YS pair (H.1 prod-0189): "... Sampler for 2023 - Part 1" vs "Part 2"."""
    matcher = CanonicalMatcher()
    tok = _digits()
    pid = _seed_producer(session)
    seed = _seed_product(
        session,
        f"Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler {tok} for 2023 - Part 1",
        producer_id=pid,
        year=None,
        weight=None,
    )
    query = f"Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler {tok} for 2023 - Part 2"
    assert _sim(session, seed.canonical_name, query) >= TRIGRAM_THRESHOLD

    result = matcher.match_or_create_product(
        _make_fields(query, harvest_year=None, weight_grams=None),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert result.decision == ProductDecision.VETO_CREATED
    assert result.guard_vetoes[0].reasons == ("item_number:mismatch",)


def test_name_year_fallback_end_to_end_through_mapper(session: Session) -> None:
    """Bronze → mapper → matcher with real CL-shaped payloads: the mapper
    leaves harvest_year NULL (title starts with "Spring"), the guard vetoes,
    and both silver rows keep harvest_year NULL."""
    import contextlib
    import datetime as dt

    from tea_scrapers.normalize.silver import NormalizeStats, SilverNormalizer
    from tea_scrapers.storage.models import RawProductSnapshot

    tok = _digits()
    source = f"year_fallback_{tok}"
    normalizer = SilverNormalizer(
        since=dt.date(2026, 1, 1),
        tracker=_NullTracker(),  # type: ignore[arg-type]
        run_id="test",
        session_factory=lambda: contextlib.nullcontext(session),
        vendor_configs={},
    )

    def _row(pid: int, year: int) -> RawProductSnapshot:
        return RawProductSnapshot(
            source=source,
            external_id=str(pid),
            scraped_at=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
            run_id="test",
            payload_hash=f"h{pid}",
            payload={
                "id": pid,
                "title": (
                    f'Spring {year} "Slumbering Dragon {tok}" Sheng / Raw Puerh '
                    "from Crimson Lotus Tea"
                ),
                "vendor": f"Year Fallback Producer {tok}",
                "product_type": "Puerh Tea",
                "tags": [],
                "variants": [
                    {"id": pid * 10, "option1": "200g", "available": True, "price": "50.00"}
                ],
            },
        )

    stats = NormalizeStats()
    normalizer._process_bronze_row(_row(1, 2017), stats, session)
    normalizer._process_bronze_row(_row(2, 2018), stats, session)

    assert stats.by_decision[ProductDecision.CREATED.value] == 1
    assert stats.by_decision[ProductDecision.VETO_CREATED.value] == 1
    assert stats.guard_vetoes_by_reason == {"harvest_year:mismatch": 1}
    years = session.execute(
        text("SELECT harvest_year FROM product WHERE canonical_name LIKE :p"),
        {"p": f"%Slumbering Dragon {tok}%"},
    ).scalars().all()
    assert years == [None, None]
