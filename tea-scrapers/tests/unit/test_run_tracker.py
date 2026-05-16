from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tea_scrapers.storage.models import ScrapeRun
from tea_scrapers.storage.run_tracker import RunTracker

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/tea",
)


@pytest.fixture(scope="module")
def engine():
    try:
        e = create_engine(DB_URL, pool_pre_ping=True, future=True)
        with e.connect():
            pass
    except Exception as exc:
        pytest.skip(f"Postgres not reachable at {DB_URL}: {exc}")
    return e


@pytest.fixture
def session_factory(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    created: list[str] = []

    @contextmanager
    def cm() -> Iterator[Session]:
        s = factory()
        try:
            # Track ScrapeRun rows added through this session so we can clean up.
            original_add = s.add

            def tracking_add(obj, **kw):
                if isinstance(obj, ScrapeRun):
                    created.append(obj.run_id)
                return original_add(obj, **kw)

            s.add = tracking_add  # type: ignore[method-assign]
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    yield cm, factory, created

    cleanup = factory()
    try:
        for rid in created:
            row = cleanup.get(ScrapeRun, rid)
            if row is not None:
                cleanup.delete(row)
        cleanup.commit()
    finally:
        cleanup.close()


def _fetch(factory, run_id: str) -> ScrapeRun | None:
    s = factory()
    try:
        return s.get(ScrapeRun, run_id)
    finally:
        s.close()


def test_clean_exit_marks_success(session_factory):
    cm, factory, _ = session_factory
    with RunTracker(source="unit_test_src", mode="full", session_factory=cm) as run:
        run.record_success()
        run.record_success()
        run_id = run.run_id

    row = _fetch(factory, run_id)
    assert row is not None
    assert row.status == "success"
    assert row.records_count == 2
    assert row.errors_count == 0
    assert row.finished_at is not None
    assert row.error_summary is None


def test_recorded_errors_mark_partial(session_factory):
    cm, factory, _ = session_factory
    with RunTracker(
        source="unit_test_src", mode="incremental", session_factory=cm
    ) as run:
        run.record_success()
        run.record_error("malformed payload: variant missing 'available'")
        run_id = run.run_id

    row = _fetch(factory, run_id)
    assert row is not None
    assert row.status == "partial"
    assert row.records_count == 1
    assert row.errors_count == 1
    assert row.error_summary is not None
    assert "malformed payload" in row.error_summary


def test_unhandled_exception_marks_failed(session_factory):
    cm, factory, _ = session_factory
    holder: dict[str, str] = {}

    class _BoomError(RuntimeError):
        pass

    with pytest.raises(_BoomError):
        with RunTracker(source="unit_test_src", mode="full", session_factory=cm) as run:
            holder["id"] = run.run_id
            run.record_success()
            raise _BoomError("network exploded")

    row = _fetch(factory, holder["id"])
    assert row is not None
    assert row.status == "failed"
    assert row.records_count == 1
    assert row.error_summary is not None
    assert "_BoomError" in row.error_summary
    assert "network exploded" in row.error_summary


def test_running_row_inserted_immediately(session_factory):
    cm, factory, _ = session_factory
    mid: dict[str, str | None] = {}

    with RunTracker(source="unit_test_src", mode="full", session_factory=cm) as run:
        peek = _fetch(factory, run.run_id)
        mid["status"] = peek.status if peek else None

    assert mid["status"] == "running"


def test_run_id_defaults_to_ulid(session_factory):
    cm, _, _ = session_factory
    with RunTracker(source="unit_test_src", mode="full", session_factory=cm) as run:
        assert len(run.run_id) == 26  # ULID Crockford-base32 length
