"""Integration tests for :func:`tea_scrapers.normalize.tier.assign_tiers`.

A/B/C transitions are intrinsically SQL — the assignment is one CTE-based
``UPDATE ... FROM`` over the touched product IDs. Stubbing it would test
the stub, not the production behavior, so we run against a real local
Postgres (the same test-fixture container the canonical / bronze loader
suites use).

Tier rules under test (design §3):

- **A** = product's latest ``product_snapshot`` has ``available = true``.
- **B** = had ``available = true`` within the last 24 months, but the
  latest snapshot is ``available = false``.
- **C** = never available, or last available > 24 months ago.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tea_scrapers.normalize.tier import assign_tiers
from tea_scrapers.storage.models import (
    Product,
    ProductSnapshot,
    Vendor,
    VendorProduct,
)

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


def _make_vendor(session: Session, suffix: str) -> int:
    v = Vendor(
        source_key=f"tier_test_{suffix}",
        display_name=f"Tier Test {suffix}",
        base_url=None,
    )
    session.add(v)
    session.flush()
    return v.vendor_id


def _make_product(session: Session, name: str) -> int:
    p = Product(
        canonical_name=name,
        producer_id=None,
        region_id=None,
        harvest_year=None,
        weight_grams=100,
    )
    session.add(p)
    session.flush()
    return p.product_id


def _make_vp(session: Session, vendor_id: int, product_id: int, ext_id: str) -> int:
    vp = VendorProduct(
        vendor_id=vendor_id,
        product_id=product_id,
        vendor_external_id=ext_id,
        vendor_url=None,
    )
    session.add(vp)
    session.flush()
    return vp.vendor_product_id


def _add_snapshot(
    session: Session,
    vp_id: int,
    *,
    scraped_at: dt.datetime,
    available: bool,
) -> None:
    session.add(
        ProductSnapshot(
            vendor_product_id=vp_id,
            scraped_at=scraped_at,
            available=available,
            price_cents=1000,
            currency="USD",
            description_hash="dummy",
        )
    )


def test_currently_available_lands_in_tier_a(session: Session) -> None:
    suffix = uuid.uuid4().hex[:8]
    vendor_id = _make_vendor(session, suffix)
    product_id = _make_product(session, f"Currently Stocked {suffix}")
    vp_id = _make_vp(session, vendor_id, product_id, f"ext_{suffix}")
    now = dt.datetime.now(dt.timezone.utc)
    _add_snapshot(session, vp_id, scraped_at=now, available=True)
    session.flush()

    stats = assign_tiers(session, touched_product_ids={product_id})
    assert stats.promoted_a == 1
    assert stats.demoted_b == 0
    assert stats.demoted_c == 0

    tier = session.execute(
        text("SELECT data_quality_tier FROM product WHERE product_id = :pid"),
        {"pid": product_id},
    ).scalar_one()
    assert tier == "A"


def test_recently_unavailable_lands_in_tier_b(session: Session) -> None:
    """Latest snapshot is unavailable, but available within last 24 months."""
    suffix = uuid.uuid4().hex[:8]
    vendor_id = _make_vendor(session, suffix)
    product_id = _make_product(session, f"Recently Discontinued {suffix}")
    vp_id = _make_vp(session, vendor_id, product_id, f"ext_{suffix}")
    now = dt.datetime.now(dt.timezone.utc)
    # 6 months ago: available; today: not available → tier B.
    _add_snapshot(session, vp_id, scraped_at=now - dt.timedelta(days=180), available=True)
    _add_snapshot(session, vp_id, scraped_at=now, available=False)
    session.flush()

    stats = assign_tiers(session, touched_product_ids={product_id})
    assert stats.demoted_b == 1

    tier = session.execute(
        text("SELECT data_quality_tier FROM product WHERE product_id = :pid"),
        {"pid": product_id},
    ).scalar_one()
    assert tier == "B"


def test_long_discontinued_lands_in_tier_c(session: Session) -> None:
    """Last available > 24 months ago → tier C."""
    suffix = uuid.uuid4().hex[:8]
    vendor_id = _make_vendor(session, suffix)
    product_id = _make_product(session, f"Long Discontinued {suffix}")
    vp_id = _make_vp(session, vendor_id, product_id, f"ext_{suffix}")
    now = dt.datetime.now(dt.timezone.utc)
    # Last available 30 months ago → outside the B window → C.
    _add_snapshot(
        session, vp_id, scraped_at=now - dt.timedelta(days=30 * 30), available=True
    )
    _add_snapshot(session, vp_id, scraped_at=now, available=False)
    session.flush()

    stats = assign_tiers(session, touched_product_ids={product_id})
    assert stats.demoted_c == 1

    tier = session.execute(
        text("SELECT data_quality_tier FROM product WHERE product_id = :pid"),
        {"pid": product_id},
    ).scalar_one()
    assert tier == "C"


def test_never_available_lands_in_tier_c(session: Session) -> None:
    """A product with snapshots that were never available → tier C."""
    suffix = uuid.uuid4().hex[:8]
    vendor_id = _make_vendor(session, suffix)
    product_id = _make_product(session, f"Never Stocked {suffix}")
    vp_id = _make_vp(session, vendor_id, product_id, f"ext_{suffix}")
    now = dt.datetime.now(dt.timezone.utc)
    _add_snapshot(
        session, vp_id, scraped_at=now - dt.timedelta(days=10), available=False
    )
    _add_snapshot(session, vp_id, scraped_at=now, available=False)
    session.flush()

    stats = assign_tiers(session, touched_product_ids={product_id})
    assert stats.demoted_c == 1


def test_a_to_b_transition_on_resweep(session: Session) -> None:
    """A product that was tier A, then goes out of stock → tier B on next sweep."""
    suffix = uuid.uuid4().hex[:8]
    vendor_id = _make_vendor(session, suffix)
    product_id = _make_product(session, f"Transitioning {suffix}")
    vp_id = _make_vp(session, vendor_id, product_id, f"ext_{suffix}")
    now = dt.datetime.now(dt.timezone.utc)

    # Round 1: available now → tier A.
    _add_snapshot(session, vp_id, scraped_at=now - dt.timedelta(days=1), available=True)
    session.flush()
    stats_a = assign_tiers(session, touched_product_ids={product_id})
    assert stats_a.promoted_a == 1

    # Round 2: add an unavailable snapshot at "now" → tier should flip to B.
    _add_snapshot(session, vp_id, scraped_at=now, available=False)
    session.flush()
    stats_b = assign_tiers(session, touched_product_ids={product_id})
    assert stats_b.demoted_b == 1
    assert stats_b.promoted_a == 0

    tier = session.execute(
        text("SELECT data_quality_tier FROM product WHERE product_id = :pid"),
        {"pid": product_id},
    ).scalar_one()
    assert tier == "B"


def test_unchanged_tier_not_counted_as_promotion(session: Session) -> None:
    """Re-running the sweep over a product whose tier doesn't change → unchanged."""
    suffix = uuid.uuid4().hex[:8]
    vendor_id = _make_vendor(session, suffix)
    product_id = _make_product(session, f"Stable A {suffix}")
    vp_id = _make_vp(session, vendor_id, product_id, f"ext_{suffix}")
    now = dt.datetime.now(dt.timezone.utc)
    _add_snapshot(session, vp_id, scraped_at=now, available=True)
    session.flush()

    first = assign_tiers(session, touched_product_ids={product_id})
    assert first.promoted_a == 1
    # Re-run — same state, tier already A → no change rows returned.
    second = assign_tiers(session, touched_product_ids={product_id})
    assert second.promoted_a == 0
    assert second.demoted_b == 0
    assert second.demoted_c == 0
    assert second.unchanged == 1


def test_empty_touched_set_is_noop(session: Session) -> None:
    stats = assign_tiers(session, touched_product_ids=set())
    assert stats.promoted_a == 0
    assert stats.demoted_b == 0
    assert stats.demoted_c == 0
    assert stats.unchanged == 0


def test_tier_a_uses_latest_snapshot_not_any_available(session: Session) -> None:
    """If the latest snapshot is unavailable, product is NOT tier A even if older snapshots were available."""
    suffix = uuid.uuid4().hex[:8]
    vendor_id = _make_vendor(session, suffix)
    product_id = _make_product(session, f"Was Available {suffix}")
    vp_id = _make_vp(session, vendor_id, product_id, f"ext_{suffix}")
    now = dt.datetime.now(dt.timezone.utc)
    _add_snapshot(session, vp_id, scraped_at=now - dt.timedelta(days=5), available=True)
    _add_snapshot(session, vp_id, scraped_at=now, available=False)
    session.flush()

    stats = assign_tiers(session, touched_product_ids={product_id})
    # Latest is unavailable → not A; available within 24mo → B.
    assert stats.promoted_a == 0
    assert stats.demoted_b == 1
