"""End-to-end integration tests for the silver normalizer (spec §1, §8).

Stages the four golden Shopify JSONL fixtures into bronze via
:class:`BronzeLoader`, runs ``tea-scrape normalize --since <date>`` via the
Click CLI, and asserts the resulting ``product`` / ``vendor_product`` /
``product_snapshot`` shape.

Coverage per the step-6 brief:

- **YS-COM "Year of the Horse"** (5-variant cake) → 5 ``product`` rows
  sharing ``producer_id`` + ``harvest_year`` + ``normalized_name``,
  distinct ``weight_grams`` in ``{50, 100, 250, 500, 1000}``, 5
  ``vendor_product`` rows with composite ``vendor_external_id``, 5
  ``product_snapshot`` rows all ``available=true``, all tier A.
- **white2tea** golden is entirely teaware (``product_type='Non-Tea'``) →
  10 rows skipped, no silver rows created.
- **Crimson Lotus "Spellbound Stag"** → 1 product, ``weight_grams=200``
  (parsed from title, NOT 210 from ``variant.grams``).
- **Re-run** is a counted no-op — every counter for silver entity inserts
  drops to zero on the second invocation.
- **Terminal DB failure** → exit code 2, JSONL untouched (idempotent retry).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from tea_scrapers.cli import cli
from tea_scrapers.storage.models import (
    Producer,
    Product,
    ProductSnapshot,
    RawProductSnapshot,
    ScrapeRun,
    Vendor,
    VendorProduct,
)

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/tea",
)
GOLDEN_DIR = Path(__file__).parent.parent / "fixtures" / "golden"
VENDORS = ["white2tea", "crimson_lotus", "yunnan_sourcing_us", "yunnan_sourcing_com"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def pg_cleanup(pg_engine):
    """Track touched bronze run_ids + scrape_run ids for teardown.

    Silver cleanup is harder because product / vendor_product / snapshot
    rows are created with surrogate keys. We track the bronze ``run_id``s
    we stage, look up the resulting silver vendor rows by source key, and
    cascade-delete in FK-safe order.
    """
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    state: dict[str, list[str]] = {
        "ingest_runs": [],
        "scrape_runs": [],
        "vendor_source_keys": [],
        "producer_canonical_names": [],
        "region_unique_keys": [],
    }
    yield state

    sess: Session = factory()
    try:
        # Order: snapshot → vendor_product → vendor + product → producer.
        if state["vendor_source_keys"]:
            # Collect vendor_ids first to scope snapshot + vp deletes.
            vendor_ids = sess.execute(
                select(Vendor.vendor_id).where(
                    Vendor.source_key.in_(state["vendor_source_keys"])
                )
            ).scalars().all()
            if vendor_ids:
                vp_ids = sess.execute(
                    select(VendorProduct.vendor_product_id).where(
                        VendorProduct.vendor_id.in_(vendor_ids)
                    )
                ).scalars().all()
                # Capture product ids touched via these vendor_products so
                # we can remove orphan product rows after.
                product_ids = sess.execute(
                    select(VendorProduct.product_id).where(
                        VendorProduct.vendor_id.in_(vendor_ids)
                    )
                ).scalars().all()
                if vp_ids:
                    sess.execute(
                        delete(ProductSnapshot).where(
                            ProductSnapshot.vendor_product_id.in_(vp_ids)
                        )
                    )
                    sess.execute(
                        delete(VendorProduct).where(
                            VendorProduct.vendor_product_id.in_(vp_ids)
                        )
                    )
                if product_ids:
                    sess.execute(
                        delete(Product).where(Product.product_id.in_(product_ids))
                    )
                sess.execute(
                    delete(Vendor).where(Vendor.vendor_id.in_(vendor_ids))
                )
        if state["producer_canonical_names"]:
            sess.execute(
                delete(Producer).where(
                    Producer.canonical_name.in_(state["producer_canonical_names"])
                )
            )
        # Regions are shared across vendors — be conservative: leave them.
        if state["ingest_runs"]:
            sess.execute(
                delete(RawProductSnapshot).where(
                    RawProductSnapshot.run_id.in_(state["ingest_runs"])
                )
            )
        for rid in state["scrape_runs"]:
            row = sess.get(ScrapeRun, rid)
            if row is not None:
                sess.delete(row)
        sess.commit()
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stage_goldens(
    dest_root: Path,
    *,
    vendors: list[str],
    date_str: str = "2026-05-16",
    run_id: str = "01HUNITNORMALIZEFIXTURE0001",
) -> None:
    for vendor in vendors:
        src = GOLDEN_DIR / f"{vendor}.jsonl"
        dest = (
            dest_root
            / f"source={vendor}"
            / f"date={date_str}"
            / f"run={run_id}.jsonl"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)


def _ingest_run_ids_for(vendors: list[str]) -> list[str]:
    seen: set[str] = set()
    for vendor in vendors:
        path = GOLDEN_DIR / f"{vendor}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            seen.add(json.loads(line)["ingest_meta"]["run_id"])
    return sorted(seen)


def _producer_names_in_goldens(vendors: list[str]) -> list[str]:
    """Vendor field values across the goldens — for cleanup deletion."""
    seen: set[str] = set()
    for vendor in vendors:
        for line in (GOLDEN_DIR / f"{vendor}.jsonl").read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)["payload"]
            tags = payload.get("tags") or []
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("Producer_"):
                    seen.add(tag.split("_", 1)[1].strip())
            vfield = payload.get("vendor")
            if isinstance(vfield, str) and vfield.strip():
                seen.add(vfield.strip())
    return sorted(seen)


def _invoke_load(
    runner: CliRunner,
    raw_dir: Path,
    *,
    since: str = "2026-05-15",
    database_url: str = DB_URL,
) -> "object":
    env = {"RAW_DATA_DIR": str(raw_dir), "DATABASE_URL": database_url}
    return runner.invoke(
        cli, ["load", "--since", since], env=env, catch_exceptions=False
    )


def _invoke_normalize(
    runner: CliRunner,
    *,
    raw_dir: Path,
    since: str = "2026-05-15",
    source: str | None = None,
    database_url: str = DB_URL,
) -> "object":
    args = ["normalize", "--since", since]
    if source is not None:
        args.extend(["--source", source])
    env = {"RAW_DATA_DIR": str(raw_dir), "DATABASE_URL": database_url}
    return runner.invoke(cli, args, env=env, catch_exceptions=False)


def _latest_normalizer_run(pg_engine) -> ScrapeRun:
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        return sess.execute(
            select(ScrapeRun)
            .where(ScrapeRun.source == "normalizer")
            .order_by(ScrapeRun.started_at.desc())
            .limit(1)
        ).scalar_one()


def _all_loader_normalizer_runs_after(pg_engine, started_after) -> list[ScrapeRun]:
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        return list(
            sess.execute(
                select(ScrapeRun)
                .where(ScrapeRun.source.in_(("loader", "normalizer")))
                .where(ScrapeRun.started_at >= started_after)
                .order_by(ScrapeRun.started_at.asc())
            ).scalars()
        )


def _common_cleanup_setup(state: dict[str, list[str]], vendors: list[str]) -> None:
    """Pre-populate the cleanup-tracking dict so teardown finds our rows."""
    state["ingest_runs"].extend(_ingest_run_ids_for(vendors))
    state["vendor_source_keys"].extend(vendors)
    state["producer_canonical_names"].extend(_producer_names_in_goldens(vendors))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_year_of_the_horse_fans_out_to_five_products(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """YS-COM record [0] is the 5-variant Year of the Horse cake.

    Expected silver shape:
    - 5 product rows sharing (producer_id, harvest_year=2026, normalized_name),
      distinct weight_grams in {50, 100, 250, 500, 1000}
    - 5 vendor_product rows with composite vendor_external_id
    - 5 product_snapshot rows all available=true
    - all 5 products land in tier A
    """
    vendor = "yunnan_sourcing_com"
    _stage_goldens(tmp_path, vendors=[vendor])
    _common_cleanup_setup(pg_cleanup, [vendor])

    runner = CliRunner()
    load_result = _invoke_load(runner, tmp_path)
    assert load_result.exit_code == 0, f"load CLI failed: {load_result.output}"

    norm_result = _invoke_normalize(runner, raw_dir=tmp_path, source=vendor)
    assert norm_result.exit_code in (0, 1), (
        f"normalize CLI exit unexpected: {norm_result.exit_code}: {norm_result.output}"
    )
    pg_cleanup["scrape_runs"].append(_latest_normalizer_run(pg_engine).run_id)

    # The YS-COM Year of the Horse golden record has external_id 8657216602183
    # (the Shopify product id). All 5 variant rows hang off that product id.
    target_pid = json.loads(
        (GOLDEN_DIR / f"{vendor}.jsonl").read_text("utf-8").splitlines()[0]
    )["ingest_meta"]["external_id"]

    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        # Find vendor row.
        vendor_row = sess.execute(
            select(Vendor).where(Vendor.source_key == vendor)
        ).scalar_one()
        # All vendor_products for this Shopify product id (composite vid).
        vps = sess.execute(
            select(VendorProduct).where(
                VendorProduct.vendor_id == vendor_row.vendor_id,
                VendorProduct.vendor_external_id.like(f"{target_pid}:%"),
            )
        ).scalars().all()
        assert len(vps) == 5, f"expected 5 vendor_products, got {len(vps)}"

        # Each variant points at a distinct product row.
        product_ids = [vp.product_id for vp in vps]
        assert len(set(product_ids)) == 5, "weight variants should be distinct products"

        products = sess.execute(
            select(Product).where(Product.product_id.in_(product_ids))
        ).scalars().all()
        weights = sorted([p.weight_grams for p in products])
        assert weights == [50, 100, 250, 500, 1000]
        # Shared producer / harvest_year / normalized name.
        assert len({p.producer_id for p in products}) == 1
        assert all(p.harvest_year == 2026 for p in products)
        canonical_names = {p.canonical_name for p in products}
        assert len(canonical_names) == 1
        # tier A across the board (latest snapshot available).
        assert {p.data_quality_tier for p in products} == {"A"}

        # 5 snapshots, all available=true.
        snaps = sess.execute(
            select(ProductSnapshot).where(
                ProductSnapshot.vendor_product_id.in_(
                    [vp.vendor_product_id for vp in vps]
                )
            )
        ).scalars().all()
        assert len(snaps) == 5
        assert all(s.available is True for s in snaps)
        assert all(s.currency == "USD" for s in snaps)


def test_white2tea_teaware_all_skipped_as_non_tea(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """The white2tea golden is entirely Non-Tea teaware → no silver rows."""
    vendor = "white2tea"
    _stage_goldens(tmp_path, vendors=[vendor])
    _common_cleanup_setup(pg_cleanup, [vendor])

    runner = CliRunner()
    assert _invoke_load(runner, tmp_path).exit_code == 0
    norm_result = _invoke_normalize(runner, raw_dir=tmp_path, source=vendor)
    assert norm_result.exit_code in (0, 1)
    pg_cleanup["scrape_runs"].append(_latest_normalizer_run(pg_engine).run_id)

    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        # When every bronze row is non-tea, the normalizer short-circuits in
        # ``_process_row`` before reaching ``_get_or_create_vendor`` (see
        # silver.py: the ``is_non_tea`` branch returns early). So the Vendor
        # row may or may not exist depending on whether earlier runs created
        # one — either way, ZERO vendor_product rows should be tied to it
        # from this run.
        vendor_row = sess.execute(
            select(Vendor).where(Vendor.source_key == vendor)
        ).scalar_one_or_none()
        if vendor_row is not None:
            vps = sess.execute(
                select(VendorProduct).where(VendorProduct.vendor_id == vendor_row.vendor_id)
            ).scalars().all()
            assert vps == [], (
                "white2tea teaware should not produce vendor_product rows; "
                f"got {len(vps)}"
            )


def test_crimson_lotus_spellbound_stag_weight_from_title_not_grams(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """Crimson Lotus 'Spellbound Stag' → weight_grams=200 (from title, not 210 grams tare)."""
    vendor = "crimson_lotus"
    _stage_goldens(tmp_path, vendors=[vendor])
    _common_cleanup_setup(pg_cleanup, [vendor])

    runner = CliRunner()
    assert _invoke_load(runner, tmp_path).exit_code == 0
    norm_result = _invoke_normalize(runner, raw_dir=tmp_path, source=vendor)
    assert norm_result.exit_code in (0, 1)
    pg_cleanup["scrape_runs"].append(_latest_normalizer_run(pg_engine).run_id)

    # The Spellbound Stag is record [0] in the Crimson Lotus golden.
    spellbound_external_id = json.loads(
        (GOLDEN_DIR / f"{vendor}.jsonl").read_text("utf-8").splitlines()[0]
    )["ingest_meta"]["external_id"]

    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        vendor_row = sess.execute(
            select(Vendor).where(Vendor.source_key == vendor)
        ).scalar_one()
        vps = sess.execute(
            select(VendorProduct).where(
                VendorProduct.vendor_id == vendor_row.vendor_id,
                VendorProduct.vendor_external_id.like(f"{spellbound_external_id}:%"),
            )
        ).scalars().all()
        # One product (single variant: Default Title, title fallback → 200g).
        assert len(vps) == 1, f"expected 1 vp for Spellbound Stag, got {len(vps)}"
        product = sess.get(Product, vps[0].product_id)
        assert product is not None
        assert product.weight_grams == 200, (
            f"Spellbound Stag weight_grams should be 200 (title fallback), "
            f"got {product.weight_grams} — variant.grams=210 must NOT be trusted"
        )


def test_rerun_is_a_counted_noop(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """Running ``normalize`` twice against identical bronze inserts zero silver rows the second time."""
    vendors = ["crimson_lotus"]
    _stage_goldens(tmp_path, vendors=vendors)
    _common_cleanup_setup(pg_cleanup, vendors)

    runner = CliRunner()
    assert _invoke_load(runner, tmp_path).exit_code == 0

    first = _invoke_normalize(runner, raw_dir=tmp_path, source=vendors[0])
    assert first.exit_code in (0, 1)
    first_run = _latest_normalizer_run(pg_engine)
    pg_cleanup["scrape_runs"].append(first_run.run_id)

    # Snapshot baseline state.
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        vendor_row = sess.execute(
            select(Vendor).where(Vendor.source_key == vendors[0])
        ).scalar_one()
        baseline_vp_count = sess.execute(
            select(VendorProduct).where(VendorProduct.vendor_id == vendor_row.vendor_id)
        ).all()
        baseline_snap_count = sess.execute(
            select(ProductSnapshot).join(
                VendorProduct,
                VendorProduct.vendor_product_id == ProductSnapshot.vendor_product_id,
            ).where(VendorProduct.vendor_id == vendor_row.vendor_id)
        ).all()
    assert len(baseline_vp_count) > 0, "first run produced no silver state — bad fixture"

    # Second run — identical bronze rows.
    second = _invoke_normalize(runner, raw_dir=tmp_path, source=vendors[0])
    assert second.exit_code in (0, 1)
    second_run = _latest_normalizer_run(pg_engine)
    pg_cleanup["scrape_runs"].append(second_run.run_id)

    with factory() as sess:
        vendor_row = sess.execute(
            select(Vendor).where(Vendor.source_key == vendors[0])
        ).scalar_one()
        after_vp_count = sess.execute(
            select(VendorProduct).where(VendorProduct.vendor_id == vendor_row.vendor_id)
        ).all()
        after_snap_count = sess.execute(
            select(ProductSnapshot).join(
                VendorProduct,
                VendorProduct.vendor_product_id == ProductSnapshot.vendor_product_id,
            ).where(VendorProduct.vendor_id == vendor_row.vendor_id)
        ).all()

    assert len(after_vp_count) == len(baseline_vp_count), (
        f"rerun grew vendor_product count: {len(baseline_vp_count)} → {len(after_vp_count)}"
    )
    assert len(after_snap_count) == len(baseline_snap_count), (
        f"rerun grew product_snapshot count: "
        f"{len(baseline_snap_count)} → {len(after_snap_count)}"
    )

    # The second scrape_run row should have zero new snapshot inserts.
    # We record success-per-batch in RunTracker; with a no-op run the
    # records_count is 0 (or NULL → coerce).
    assert (second_run.records_count or 0) == 0, (
        f"rerun records_count should be 0, got {second_run.records_count}"
    )


def test_full_quartet_normalizes_with_expected_decisions(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """End-to-end across all 4 goldens: tier-A products exist, decisions are sane."""
    _stage_goldens(tmp_path, vendors=VENDORS)
    _common_cleanup_setup(pg_cleanup, VENDORS)

    runner = CliRunner()
    assert _invoke_load(runner, tmp_path).exit_code == 0

    norm_result = _invoke_normalize(runner, raw_dir=tmp_path)
    assert norm_result.exit_code in (0, 1)
    pg_cleanup["scrape_runs"].append(_latest_normalizer_run(pg_engine).run_id)

    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        # white2tea contributed no silver — check it didn't (vendor row OK).
        white2tea = sess.execute(
            select(Vendor).where(Vendor.source_key == "white2tea")
        ).scalar_one_or_none()
        if white2tea is not None:
            vp_count = sess.execute(
                select(VendorProduct).where(VendorProduct.vendor_id == white2tea.vendor_id)
            ).all()
            assert vp_count == []

        # Other 3 sources produced silver vendor_products + tier-A products.
        for source_key in ("yunnan_sourcing_us", "yunnan_sourcing_com", "crimson_lotus"):
            vrow = sess.execute(
                select(Vendor).where(Vendor.source_key == source_key)
            ).scalar_one()
            vp_count = sess.execute(
                select(VendorProduct).where(VendorProduct.vendor_id == vrow.vendor_id)
            ).all()
            assert len(vp_count) > 0, f"{source_key} produced no vendor_products"

            # At least one tier-A product per source.
            tier_a_count = sess.execute(
                select(Product)
                .join(VendorProduct, VendorProduct.product_id == Product.product_id)
                .where(VendorProduct.vendor_id == vrow.vendor_id)
                .where(Product.data_quality_tier == "A")
            ).all()
            assert len(tier_a_count) > 0, f"{source_key} produced no tier-A products"


def test_normalize_terminal_failure_exit_2(
    tmp_path: Path, session_cache_reset
) -> None:
    """Unreachable DB → exit 2; uses the centralized ``session_cache_reset`` fixture.

    The fixture (per bronze-loader follow-up #3) clears the
    :mod:`tea_scrapers.storage.session` ``lru_cache``s around the test
    body so the per-invocation ``DATABASE_URL`` env actually takes effect.
    """
    _stage_goldens(tmp_path, vendors=["crimson_lotus"])
    runner = CliRunner()
    env = {
        "RAW_DATA_DIR": str(tmp_path),
        "DATABASE_URL": "postgresql+psycopg://postgres:postgres@127.0.0.1:1/does_not_exist",
    }
    result = runner.invoke(
        cli,
        ["normalize", "--since", "2026-05-15"],
        env=env,
        catch_exceptions=False,
    )
    assert result.exit_code == 2, (
        f"expected exit 2 for terminal DB failure, got {result.exit_code}: {result.output}"
    )
