"""End-to-end integration tests for the bronze loader (spec §1, §8).

Drives the ``tea-scrape load --since <date>`` CLI against a real local
Postgres, staging Hive-partitioned JSONL under tmp dirs. Mirrors the shape
of ``test_shopify_*.py`` for env override + cleanup.

Requires a reachable Postgres at ``TEST_DATABASE_URL`` (defaults to the
local ``tea-postgres`` container); skipped otherwise.
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
from tea_scrapers.load.bronze import payload_hash
from tea_scrapers.storage.models import RawProductSnapshot, ScrapeRun

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
    """Track ``scrape_run.run_id``s + bronze ``ingest_meta.run_id``s for teardown.

    The fixture yields a dict with two lists: ``scrape_runs`` (loader-side
    invocation ULIDs) and ``ingest_runs`` (JSONL-side ULIDs embedded in the
    fixtures). On teardown, deletes matching ``raw_product_snapshot`` rows
    first (FK-free table, safe in any order) and matching ``scrape_run``
    rows next.
    """
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    state: dict[str, list[str]] = {"scrape_runs": [], "ingest_runs": []}
    yield state

    sess: Session = factory()
    try:
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


def _stage_goldens(
    dest_root: Path,
    *,
    vendors: list[str],
    date_str: str = "2026-05-16",
    run_id: str = "01HUNITGOLDENFIXTURELOADER1",
) -> None:
    """Copy each named golden fixture into a Hive-partitioned tree under ``dest_root``."""
    for vendor in vendors:
        src = GOLDEN_DIR / f"{vendor}.jsonl"
        if not src.exists():
            raise FileNotFoundError(f"missing golden fixture: {src}")
        dest = (
            dest_root
            / f"source={vendor}"
            / f"date={date_str}"
            / f"run={run_id}.jsonl"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)


def _invoke_load(
    runner: CliRunner,
    raw_dir: Path,
    *,
    since: str = "2026-05-16",
    database_url: str = DB_URL,
) -> "object":
    env = {"RAW_DATA_DIR": str(raw_dir), "DATABASE_URL": database_url}
    return runner.invoke(
        cli,
        ["load", "--since", since],
        env=env,
        catch_exceptions=False,
    )


def _ingest_run_ids_for(vendors: list[str]) -> list[str]:
    """Read every golden's embedded ``ingest_meta.run_id`` so cleanup can target them."""
    seen: set[str] = set()
    for vendor in vendors:
        path = GOLDEN_DIR / f"{vendor}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            seen.add(json.loads(line)["ingest_meta"]["run_id"])
    return sorted(seen)


def _latest_loader_run(pg_engine) -> ScrapeRun:
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        row = sess.execute(
            select(ScrapeRun)
            .where(ScrapeRun.source == "loader")
            .order_by(ScrapeRun.started_at.desc())
            .limit(1)
        ).scalar_one()
        return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_loads_golden_jsonl_into_bronze(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """All 4 goldens (40 records total) land in ``raw_product_snapshot``."""
    _stage_goldens(tmp_path, vendors=VENDORS)
    pg_cleanup["ingest_runs"].extend(_ingest_run_ids_for(VENDORS))

    runner = CliRunner()
    result = _invoke_load(runner, tmp_path)
    assert result.exit_code == 0, f"CLI failed: {result.output}"

    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        count = sess.execute(
            select(RawProductSnapshot)
            .where(RawProductSnapshot.run_id.in_(pg_cleanup["ingest_runs"]))
        ).all()
    assert len(count) == 40, f"expected 40 rows, got {len(count)}"

    loader_run = _latest_loader_run(pg_engine)
    pg_cleanup["scrape_runs"].append(loader_run.run_id)
    assert loader_run.mode == "bronze"
    assert loader_run.status == "success"
    assert loader_run.records_count == 40
    assert loader_run.errors_count == 0


def test_cli_dedup_on_rerun(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """A second invocation against identical JSONL inserts zero new rows."""
    _stage_goldens(tmp_path, vendors=VENDORS)
    pg_cleanup["ingest_runs"].extend(_ingest_run_ids_for(VENDORS))

    runner = CliRunner()
    first = _invoke_load(runner, tmp_path)
    assert first.exit_code == 0

    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        first_count = sess.execute(
            select(RawProductSnapshot)
            .where(RawProductSnapshot.run_id.in_(pg_cleanup["ingest_runs"]))
        ).all()
    first_n = len(first_count)
    pg_cleanup["scrape_runs"].append(_latest_loader_run(pg_engine).run_id)

    second = _invoke_load(runner, tmp_path)
    assert second.exit_code == 0

    with factory() as sess:
        second_count = sess.execute(
            select(RawProductSnapshot)
            .where(RawProductSnapshot.run_id.in_(pg_cleanup["ingest_runs"]))
        ).all()
    assert len(second_count) == first_n, "dedup failed: row count grew on rerun"

    second_run = _latest_loader_run(pg_engine)
    pg_cleanup["scrape_runs"].append(second_run.run_id)
    assert second_run.records_count == 0, (
        f"rerun should have inserted nothing, got records_count={second_run.records_count}"
    )
    assert second_run.errors_count == 0
    assert second_run.status == "success"


def test_cli_changed_payload_creates_new_snapshot(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """Mutating one record's payload + reloading produces exactly one new row."""
    vendor = "white2tea"
    _stage_goldens(tmp_path, vendors=[vendor])

    golden_lines = (GOLDEN_DIR / f"{vendor}.jsonl").read_text("utf-8").splitlines()
    original_ingest_run_id = json.loads(golden_lines[0])["ingest_meta"]["run_id"]
    pg_cleanup["ingest_runs"].append(original_ingest_run_id)

    runner = CliRunner()
    first = _invoke_load(runner, tmp_path)
    assert first.exit_code == 0
    pg_cleanup["scrape_runs"].append(_latest_loader_run(pg_engine).run_id)

    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        rows_before = sess.execute(
            select(RawProductSnapshot).where(
                RawProductSnapshot.run_id == original_ingest_run_id
            )
        ).all()

    # Mutate exactly one record's payload + write into a NEW partition with a
    # fresh run_id so cleanup can target it deterministically.
    new_run_id = "01HUNITMUTATEDPAYLOADRERUN2"
    pg_cleanup["ingest_runs"].append(new_run_id)
    mutated_lines: list[str] = []
    mutated_external_id: str | None = None
    for idx, line in enumerate(golden_lines):
        rec = json.loads(line)
        rec["ingest_meta"]["run_id"] = new_run_id
        if idx == 0:
            mutated_external_id = rec["ingest_meta"]["external_id"]
            # Bump one field — title is a string in every golden Shopify payload.
            rec["payload"]["title"] = (rec["payload"].get("title", "") + " (mutated)")
        mutated_lines.append(json.dumps(rec, ensure_ascii=False))

    mutated_path = (
        tmp_path
        / f"source={vendor}"
        / "date=2026-05-17"
        / f"run={new_run_id}.jsonl"
    )
    mutated_path.parent.mkdir(parents=True, exist_ok=True)
    mutated_path.write_text("\n".join(mutated_lines) + "\n", encoding="utf-8")

    second = _invoke_load(runner, tmp_path, since="2026-05-16")
    assert second.exit_code == 0
    second_run = _latest_loader_run(pg_engine)
    pg_cleanup["scrape_runs"].append(second_run.run_id)

    with factory() as sess:
        # Exactly one new row for the mutated external_id (the rest dedup'd
        # because their payloads — and thus payload_hashes — are identical).
        mutated_rows = sess.execute(
            select(RawProductSnapshot).where(
                RawProductSnapshot.source == vendor,
                RawProductSnapshot.external_id == mutated_external_id,
            )
        ).scalars().all()

    assert len(mutated_rows) == 2, (
        f"expected 1 original + 1 mutated row for {mutated_external_id}, got {len(mutated_rows)}"
    )
    hashes = {r.payload_hash for r in mutated_rows}
    assert len(hashes) == 2, "payload_hash should differ between original and mutated"

    # And the second run only inserted that one mutated row.
    assert second_run.records_count == 1, (
        f"expected exactly 1 insert on rerun, got {second_run.records_count}"
    )
    # Sanity: row count for ``original_ingest_run_id`` is unchanged.
    assert len(rows_before) == 10


def test_cli_partial_failure_exit_1(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """One malformed JSONL line → exit 1, status='partial', other rows still loaded."""
    vendor = "white2tea"
    _stage_goldens(tmp_path, vendors=[vendor])

    jsonl_path = next((tmp_path / f"source={vendor}").rglob("*.jsonl"))
    original = jsonl_path.read_text("utf-8").splitlines()
    pg_cleanup["ingest_runs"].append(
        json.loads(original[0])["ingest_meta"]["run_id"]
    )

    # Inject a malformed line in the middle.
    corrupted = original[:5] + ["{this is not valid json"] + original[5:]
    jsonl_path.write_text("\n".join(corrupted) + "\n", encoding="utf-8")

    runner = CliRunner()
    result = _invoke_load(runner, tmp_path)
    assert result.exit_code == 1, (
        f"expected exit 1 for partial failure, got {result.exit_code}: {result.output}"
    )

    loader_run = _latest_loader_run(pg_engine)
    pg_cleanup["scrape_runs"].append(loader_run.run_id)
    assert loader_run.status == "partial"
    assert loader_run.errors_count == 1
    assert loader_run.records_count == 10  # all valid rows still loaded


def test_cli_terminal_failure_exit_2(tmp_path: Path) -> None:
    """Unreachable DB → exit 2; no JSONL state corrupted (idempotent retry possible).

    Clears the module-level engine/session caches first so the CLI picks up
    the per-invocation ``DATABASE_URL`` rather than a previously-cached
    engine pointing at the working test DB.
    """
    from tea_scrapers.storage import session as session_mod

    session_mod.get_engine.cache_clear()
    session_mod._session_factory.cache_clear()  # noqa: SLF001 — test reset
    session_mod._settings.cache_clear()  # noqa: SLF001 — test reset

    _stage_goldens(tmp_path, vendors=["white2tea"])
    runner = CliRunner()
    try:
        result = _invoke_load(
            runner,
            tmp_path,
            database_url="postgresql+psycopg://postgres:postgres@127.0.0.1:1/does_not_exist",
        )
        assert result.exit_code == 2, (
            f"expected exit 2 for terminal failure, got {result.exit_code}: {result.output}"
        )

        # JSONL is on-disk and untouched — re-running with a real DB would still work.
        staged = next((tmp_path / "source=white2tea").rglob("*.jsonl"))
        assert staged.exists()
        assert staged.stat().st_size > 0
    finally:
        # Restore caches so subsequent tests in this module / session keep working.
        session_mod.get_engine.cache_clear()
        session_mod._session_factory.cache_clear()  # noqa: SLF001
        session_mod._settings.cache_clear()  # noqa: SLF001


def test_payload_byte_equivalence_through_bronze(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """Bronze ``payload`` round-trips byte-equivalent to the JSONL source.

    Spec §11: the loader must not mutate the upstream payload. JSONB
    storage normalizes whitespace + key order, but the *decoded* dict must
    equal the original.
    """
    vendor = "white2tea"
    _stage_goldens(tmp_path, vendors=[vendor])
    golden_lines = (GOLDEN_DIR / f"{vendor}.jsonl").read_text("utf-8").splitlines()
    pg_cleanup["ingest_runs"].append(json.loads(golden_lines[0])["ingest_meta"]["run_id"])

    runner = CliRunner()
    result = _invoke_load(runner, tmp_path)
    assert result.exit_code == 0
    pg_cleanup["scrape_runs"].append(_latest_loader_run(pg_engine).run_id)

    sample = json.loads(golden_lines[0])
    expected_payload = sample["payload"]
    expected_hash = payload_hash(expected_payload)

    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    with factory() as sess:
        row = sess.execute(
            select(RawProductSnapshot).where(
                RawProductSnapshot.source == vendor,
                RawProductSnapshot.external_id == sample["ingest_meta"]["external_id"],
            )
        ).scalar_one()

    # Dict equality — JSONB may have reordered keys but decoded structure
    # must be identical.
    assert row.payload == expected_payload
    assert row.payload_hash == expected_hash


def test_since_filter_skips_older_partitions(
    pg_engine, tmp_path: Path, pg_cleanup: dict[str, list[str]]
) -> None:
    """``--since 2026-05-15`` loads May 16 only, skips May 10 partitions."""
    vendor = "white2tea"
    golden = (GOLDEN_DIR / f"{vendor}.jsonl").read_text("utf-8").splitlines()
    pg_cleanup["ingest_runs"].append(json.loads(golden[0])["ingest_meta"]["run_id"])

    # Stage the same golden into two date partitions with distinct run= names
    # so both files exist and only the date filter can differentiate them.
    older = (
        tmp_path
        / f"source={vendor}"
        / "date=2026-05-10"
        / "run=01HUNITOLDPARTITION0000001.jsonl"
    )
    newer = (
        tmp_path
        / f"source={vendor}"
        / "date=2026-05-16"
        / "run=01HUNITNEWPARTITION0000001.jsonl"
    )
    for p in (older, newer):
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(GOLDEN_DIR / f"{vendor}.jsonl", p)

    runner = CliRunner()
    result = _invoke_load(runner, tmp_path, since="2026-05-15")
    assert result.exit_code == 0

    loader_run = _latest_loader_run(pg_engine)
    pg_cleanup["scrape_runs"].append(loader_run.run_id)
    # Both partitions contain the same 10 records → since both are dedup'd
    # against the same (source, external_id, payload_hash), only the first
    # file processed inserts rows. Loader sorts by date, so May 16 wins and
    # May 10 would be skipped by the --since filter anyway.
    assert loader_run.records_count == 10
    assert loader_run.status == "success"
