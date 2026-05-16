"""Scrape-run lifecycle persisted to ``scrape_run`` (spec §4, §8)."""

from __future__ import annotations

import datetime as dt
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any

import structlog
from ulid import ULID

from tea_scrapers.storage.models import ScrapeRun
from tea_scrapers.storage.session import get_session


class RunTracker(AbstractContextManager["RunTracker"]):
    """Single-scrape-run bookkeeping; inserts a 'running' row on enter and finalizes on exit."""

    def __init__(
        self,
        source: str,
        mode: str,
        run_id: str | None = None,
        session_factory: "Any" = None,
        log: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self.run_id = run_id or str(ULID())
        self.source = source
        self.mode = mode
        self._session_factory = session_factory or get_session
        self._log = log or structlog.get_logger()
        self._started_at: dt.datetime | None = None
        self._records = 0
        self._errors = 0
        self._error_samples: list[str] = []
        self._token: Any = None

    @property
    def records_count(self) -> int:
        return self._records

    @property
    def errors_count(self) -> int:
        return self._errors

    def record_success(self, n: int = 1) -> None:
        self._records += n

    def record_error(self, summary: str) -> None:
        self._errors += 1
        # Cap retained samples to keep ``error_summary`` from unbounded growth.
        if len(self._error_samples) < 20:
            self._error_samples.append(summary)

    def __enter__(self) -> "RunTracker":
        self._started_at = dt.datetime.now(dt.timezone.utc)
        self._token = structlog.contextvars.bind_contextvars(
            run_id=self.run_id, source=self.source, mode=self.mode
        )
        self._insert_running_row()
        self._log.info(
            "scrape.run.start",
            run_id=self.run_id,
            source=self.source,
            mode=self.mode,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        finished_at = dt.datetime.now(dt.timezone.utc)
        assert self._started_at is not None
        duration_s = (finished_at - self._started_at).total_seconds()

        status = self._terminal_status(exc)
        error_summary = self._render_error_summary(exc)

        try:
            self._finalize_row(finished_at=finished_at, status=status, summary=error_summary)
        finally:
            self._log.info(
                "scrape.run.end",
                run_id=self.run_id,
                source=self.source,
                mode=self.mode,
                status=status,
                records_count=self._records,
                errors_count=self._errors,
                duration_s=round(duration_s, 3),
            )
            structlog.contextvars.unbind_contextvars("run_id", "source", "mode")
            self._token = None
        return None  # propagate any in-flight exception

    def _terminal_status(self, exc: BaseException | None) -> str:
        if exc is not None:
            return "failed"
        if self._errors > 0:
            return "partial"
        return "success"

    def _render_error_summary(self, exc: BaseException | None) -> str | None:
        parts: list[str] = []
        if exc is not None:
            parts.append(f"terminal: {type(exc).__name__}: {exc}")
        parts.extend(self._error_samples)
        if not parts:
            return None
        joined = "\n".join(parts)
        return joined[:4000]

    def _insert_running_row(self) -> None:
        with self._session_factory() as session:
            session.add(
                ScrapeRun(
                    run_id=self.run_id,
                    source=self.source,
                    mode=self.mode,
                    started_at=self._started_at,
                    status="running",
                    records_count=0,
                    errors_count=0,
                )
            )

    def _finalize_row(
        self,
        finished_at: dt.datetime,
        status: str,
        summary: str | None,
    ) -> None:
        with self._session_factory() as session:
            row = session.get(ScrapeRun, self.run_id)
            if row is None:
                # Insert path failed earlier; persist a terminal row so the failure is visible.
                row = ScrapeRun(
                    run_id=self.run_id,
                    source=self.source,
                    mode=self.mode,
                    started_at=self._started_at or finished_at,
                    status=status,
                )
                session.add(row)
            row.finished_at = finished_at
            row.status = status
            row.records_count = self._records
            row.errors_count = self._errors
            row.error_summary = summary


__all__ = ["RunTracker"]
