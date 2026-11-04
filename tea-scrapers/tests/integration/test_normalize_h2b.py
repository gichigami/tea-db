"""ROADMAP H.2b against real Postgres: alias seeding, pinyin / skeleton keys in
the producer + product matchers, and the one-sided blocking knob.

Every test runs in a rolled-back session. The shared ``tea`` database may
already hold seeded producers with these real names (an earlier
``tea-scrape normalize``), so each test first deletes the rows it is about to
assert on — inside the same transaction, so nothing persists.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tea_scrapers.normalize.canonical import CanonicalMatcher, ProductDecision
from tea_scrapers.normalize.producer_aliases import (
    load_producer_aliases,
    seed_producer_aliases,
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
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "producer_aliases"
    / "sample_producer_aliases.yaml"
)
REAL_NAMES = [
    "Menghai Tea Factory",
    "Menghai Tea Factory (Da Yi Brand)",
    "Dayi",
    "Xiaguan Tea Factory",
    "white2tea",
    "Crimson Lotus Tea",
    "金家棋",
    "Jin Jia Qi",
    "Hai Lang Hao",
    "Hai Langhao",
    "Hailanghao",
]


@pytest.fixture(scope="module")
def pg_engine():
    try:
        e = create_engine(DB_URL, pool_pre_ping=True, future=True)
        with e.connect():
            pass
    except Exception as exc:  # noqa: BLE001 - any connect failure means skip
        pytest.skip(f"Postgres not reachable at {DB_URL}: {exc}")
    return e


@pytest.fixture
def session(pg_engine) -> Session:
    sess = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)()
    try:
        # Clear producers whose names / aliases these tests assert on (rolled
        # back at teardown). Products / vendor_products that reference them
        # are detached first so the FK allows the delete.
        keys = [n.lower() for n in REAL_NAMES] + ["dayi", "da yi", "大益", "w2t", "jin jia qi"]
        ids = [
            r.producer_id
            for r in sess.execute(
                text(
                    "SELECT producer_id FROM producer WHERE LOWER(canonical_name) = ANY(:k) "
                    "OR aliases && CAST(:k AS text[])"
                ),
                {"k": keys},
            )
        ]
        if ids:
            sess.execute(
                text("UPDATE product SET producer_id = NULL WHERE producer_id = ANY(:i)"),
                {"i": ids},
            )
            sess.execute(text("DELETE FROM producer WHERE producer_id = ANY(:i)"), {"i": ids})
        yield sess
    finally:
        sess.rollback()
        sess.close()


def _hint(name: str) -> ProducerHint:
    return ProducerHint(name=name, source="tag")


def _fields(title: str, *, year: int | None, weight: int | None) -> ProductFields:
    return ProductFields(
        shopify_product_id="1",
        canonical_name=title,
        normalized_name=normalize_name(title),
        producer_hint=None,
        region_hint=None,
        harvest_year=year,
        tea_type=None,
        tea_style=None,
        format=None,
        cultivar=None,
        weight_grams=weight,
        variant=VariantFields(
            shopify_variant_id="1",
            weight_grams=weight,
            available=True,
            price_cents=1000,
            currency="USD",
        ),
        vendor_url=None,
        description_hash="dummy",
        is_non_tea=False,
    )


def _producer(session: Session, name: str, aliases: list[str] | None = None) -> int:
    p = Producer(canonical_name=name, aliases=aliases or [name.lower()])
    session.add(p)
    session.flush()
    return p.producer_id


def _aliases(session: Session, pid: int) -> list[str]:
    return list(
        session.execute(
            text("SELECT aliases FROM producer WHERE producer_id = :p"), {"p": pid}
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Part 2 — alias seeding
# ---------------------------------------------------------------------------


def test_seed_inserts_then_is_idempotent(session: Session) -> None:
    table = load_producer_aliases(FIXTURE)
    first = seed_producer_aliases(session, table)
    assert first.inserted == 4 and first.updated == 0 and not first.conflicts
    pid = session.execute(
        text("SELECT producer_id FROM producer WHERE canonical_name = 'Menghai Tea Factory'")
    ).scalar_one()
    stored = _aliases(session, pid)
    assert {"dayi", "da yi", "大益", "勐海茶厂", "meng hai cha chang"} <= set(stored)

    again = seed_producer_aliases(session, table)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 4)
    assert _aliases(session, pid) == stored


def test_seed_unions_into_existing_vendor_tag_row_without_renaming(session: Session) -> None:
    # A YS ``Producer_`` tag created this row before any curation existed.
    pid = _producer(session, "Menghai Tea Factory (Da Yi Brand)")
    stats = seed_producer_aliases(session, load_producer_aliases(FIXTURE))
    assert stats.updated >= 1
    row = session.get(Producer, pid)
    session.refresh(row)
    assert row.canonical_name == "Menghai Tea Factory (Da Yi Brand)"
    assert "dayi" in row.aliases and "大益" in row.aliases


def test_seed_never_merges_a_preexisting_split(session: Session) -> None:
    canon = _producer(session, "Menghai Tea Factory")
    split = _producer(session, "Dayi")
    stats = seed_producer_aliases(session, load_producer_aliases(FIXTURE))
    (conflict,) = [c for c in stats.conflicts if c["canonical"] == "Menghai Tea Factory"]
    assert conflict["target_producer_id"] == canon
    assert conflict["other_producer_ids"] == [split]
    assert "dayi" in conflict["skipped_keys"]
    assert "dayi" not in _aliases(session, canon)  # left on the split row only
    assert session.execute(
        text("SELECT count(*) FROM producer WHERE producer_id IN (:a, :b)"),
        {"a": canon, "b": split},
    ).scalar_one() == 2


@pytest.mark.parametrize("raw", ["Dayi", "DaYi", "Da-Yi", "大益", "勐海茶厂", "Da Yi"])
def test_seeded_aliases_resolve_in_matcher(session: Session, raw: str) -> None:
    seed_producer_aliases(session, load_producer_aliases(FIXTURE))
    pid = session.execute(
        text("SELECT producer_id FROM producer WHERE canonical_name = 'Menghai Tea Factory'")
    ).scalar_one()
    assert CanonicalMatcher().match_or_create_producer(_hint(raw), session) == pid


def test_seeded_abbreviation_resolves(session: Session) -> None:
    seed_producer_aliases(session, load_producer_aliases(FIXTURE))
    m = CanonicalMatcher()
    assert m.match_or_create_producer(_hint("W2T"), session) == m.match_or_create_producer(
        _hint("white2tea"), session
    )


# ---------------------------------------------------------------------------
# Part 1 — pinyin / skeleton in the matchers
# ---------------------------------------------------------------------------


def test_hanzi_producer_created_first_then_pinyin_spellings_join(session: Session) -> None:
    m = CanonicalMatcher()
    pid = m.match_or_create_producer(_hint("金家棋"), session)
    assert "jin jia qi" in _aliases(session, pid)
    assert CanonicalMatcher().match_or_create_producer(_hint("Jin Jia Qi"), session) == pid
    assert CanonicalMatcher().match_or_create_producer(_hint("Jinjiaqi"), session) == pid


def test_producer_skeleton_hit_must_be_unique(session: Session) -> None:
    a = _producer(session, "Hai Lang Hao")
    b = _producer(session, "Hai Langhao")
    # Both share the skeleton "hailanghao" and trigram stays < 0.85, so the
    # only route to a or b would be guessing between them → create instead.
    pid = CanonicalMatcher().match_or_create_producer(_hint("Hailanghao"), session)
    assert pid not in (a, b)
    # With a single skeleton owner the same spelling joins it.
    session.execute(text("DELETE FROM producer WHERE producer_id IN (:b, :p)"), {"b": b, "p": pid})
    assert CanonicalMatcher().match_or_create_producer(_hint("Hailanghao"), session) == a


def test_producer_transliteration_can_be_disabled(session: Session) -> None:
    pid = _producer(session, "Jin Jia Qi")
    other = CanonicalMatcher(transliterate=False).match_or_create_producer(_hint("金家棋"), session)
    assert other != pid
    # Re-enabled, the hanzi keys {"金家棋", "jin jia qi"} overlap both rows'
    # aliases; the alias step is deterministic (lowest producer_id).
    assert CanonicalMatcher().match_or_create_producer(_hint("金家棋"), session) == pid


def _seed_product(session: Session, name: str, *, pid: int | None, year, weight) -> int:
    p = Product(canonical_name=name, producer_id=pid, harvest_year=year, weight_grams=weight)
    session.add(p)
    session.flush()
    return p.product_id


@pytest.mark.parametrize(
    ("stored", "query", "year", "weight"),
    [
        ("Dahongpao", "Da Hong Pao", None, 25),  # H.1 prod-0349 (white2tea)
        ("Classic Tie Guan Yin", "Classic Tieguanyin", None, 8),  # prod-0348
        (
            '2025 Yunnan Sourcing "Bing Dao Lao Zhai" Raw Pu-erh Tea Cake',
            '2025 Yunnan Sourcing "Bingdao Lao Zhai" Raw Pu-erh Tea Cake',
            2025,
            200,
        ),  # prod-0339
        (
            "2023 Yiwu Xiao Gaogan Sheng / Raw Puerh Tea - 200g Cake",
            "2023 Yi Wu Xiao Gaogan Sheng / Raw Puerh Tea - 200g Cake",
            2023,
            200,
        ),  # prod-0343
    ],
)
def test_product_skeleton_exact(session: Session, stored, query, year, weight) -> None:
    pid = _producer(session, f"H2b Producer {uuid.uuid4().hex[:8]}")
    tag = uuid.uuid4().int % 10**8
    seed = _seed_product(session, f"{stored} {tag}", pid=pid, year=year, weight=weight)
    r = CanonicalMatcher().match_or_create_product(
        _fields(f"{query} {tag}", year=year, weight=weight),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert (r.decision, r.product_id) == (ProductDecision.SKELETON_EXACT, seed)


def test_product_hanzi_title_matches_pinyin_title(session: Session) -> None:
    pid = _producer(session, f"H2b Producer {uuid.uuid4().hex[:8]}")
    seed = _seed_product(session, "Lao Ban Zhang Gushu", pid=pid, year=2005, weight=357)
    r = CanonicalMatcher().match_or_create_product(
        _fields("老班章 Gushu", year=2005, weight=357),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert (r.decision, r.product_id) == (ProductDecision.SKELETON_EXACT, seed)


def test_product_skeleton_respects_recipe_code(session: Session) -> None:
    pid = _producer(session, f"H2b Producer {uuid.uuid4().hex[:8]}")
    _seed_product(session, "2023 大益 7262 熟饼", pid=pid, year=2023, weight=357)
    r = CanonicalMatcher().match_or_create_product(
        _fields("2023 大益 7572 熟饼", year=2023, weight=357),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert r.decision in (ProductDecision.CREATED, ProductDecision.VETO_CREATED)


# ---------------------------------------------------------------------------
# Part 3 — one-sided blocking knob (default veto)
# ---------------------------------------------------------------------------

SEATTLE = '2021 "The Sprawl" Sheng / Raw Puerh Tea'  # H.1 prod-0018


def test_one_sided_weight_vetoes_by_default(session: Session) -> None:
    pid = _producer(session, f"H2b Producer {uuid.uuid4().hex[:8]}")
    base = _seed_product(session, SEATTLE, pid=pid, year=2021, weight=200)
    r = CanonicalMatcher().match_or_create_product(
        _fields(f"{SEATTLE} :: Seattle Inventory", year=2021, weight=None),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert r.product_id != base and r.decision == ProductDecision.CREATED


def test_one_sided_weight_exact_only_reuses_unique_twin(session: Session) -> None:
    pid = _producer(session, f"H2b Producer {uuid.uuid4().hex[:8]}")
    base = _seed_product(session, SEATTLE, pid=pid, year=2021, weight=200)
    m = CanonicalMatcher(one_sided_blocking={"weight_grams": "exact_only"})
    r = m.match_or_create_product(
        _fields(f"{SEATTLE} :: Seattle Inventory", year=2021, weight=None),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert (r.decision, r.product_id) == (ProductDecision.ONE_SIDED_EXACT, base)


def test_one_sided_weight_exact_only_refuses_when_several_weights(session: Session) -> None:
    pid = _producer(session, f"H2b Producer {uuid.uuid4().hex[:8]}")
    _seed_product(session, SEATTLE, pid=pid, year=2021, weight=200)
    _seed_product(session, SEATTLE, pid=pid, year=2021, weight=357)
    m = CanonicalMatcher(one_sided_blocking={"weight_grams": "exact_only"})
    r = m.match_or_create_product(
        _fields(f"{SEATTLE} :: Seattle Inventory", year=2021, weight=None),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert r.decision == ProductDecision.CREATED


def test_one_sided_exact_only_never_relaxes_a_real_mismatch(session: Session) -> None:
    pid = _producer(session, f"H2b Producer {uuid.uuid4().hex[:8]}")
    base = _seed_product(session, SEATTLE, pid=pid, year=2021, weight=200)
    m = CanonicalMatcher(one_sided_blocking={"weight_grams": "exact_only"})
    r = m.match_or_create_product(
        _fields(SEATTLE, year=2021, weight=25),
        producer_id=pid,
        region_id=None,
        session=session,
    )
    assert r.product_id != base


def test_invalid_one_sided_blocking_rejected() -> None:
    with pytest.raises(ValueError):
        CanonicalMatcher(one_sided_blocking={"weight_grams": "pass"})
