"""JSONL → bronze ``raw_product_snapshot`` loader (spec §1, §8).

Reads Hive-partitioned JSONL produced by the scrapers
(``data/raw/source={vendor}/date={YYYY-MM-DD}/run={ulid}.jsonl``) into the
bronze table. Re-runnable: dedup is enforced by the
``uq_raw_source_external_hash`` unique constraint via
``ON CONFLICT DO NOTHING``. The loader never re-hits external sources, never
mutates the upstream payload, and treats per-record parse failures as
non-fatal (spec §11).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pydantic
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tea_scrapers.schemas.ingest import RawRecord
from tea_scrapers.storage.models import RawProductSnapshot
from tea_scrapers.storage.run_tracker import RunTracker
from tea_scrapers.storage.session import get_session

# ``data/raw/source=<key>/date=YYYY-MM-DD/run=<ulid>.jsonl`` — the writer
# always produces this shape (see ``JsonlWriter._open``). We parse the
# partition segments rather than re-derive them from the JSONL contents so
# the loader can stream-decide which files to read without opening them.
_PARTITION_RE = re.compile(
    r"^source=(?P<source>[^/]+)/date=(?P<date>\d{4}-\d{2}-\d{2})/run=[^/]+\.jsonl$"
)


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable SHA-256 of the canonical JSON encoding of a bronze payload.

    Canonical form: ``sort_keys=True``, compact separators, UTF-8,
    ``ensure_ascii=False``. Always hash the *original* JSONL payload — NEVER
    a value round-tripped through Postgres JSONB. Postgres normalizes
    whitespace and may reorder keys on read, so a JSONB round-trip would
    silently produce a different hash for the same logical payload, breaking
    dedup.

    No ``default=`` fallback — payloads round-trip through Pydantic and are
    JSON-native by construction. A non-serializable type would be a real bug
    and the ``TypeError`` from ``json.dumps`` should propagate.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass
class LoadStats:
    """Cumulative counts from a :class:`BronzeLoader` run."""

    files_scanned: int = 0
    records_read: int = 0
    inserted: int = 0
    skipped_dedup: int = 0
    parse_errors: int = 0
    by_vendor: dict[str, dict[str, int]] = field(default_factory=dict)

    def _vendor_bucket(self, source: str) -> dict[str, int]:
        bucket = self.by_vendor.get(source)
        if bucket is None:
            bucket = {
                "files_scanned": 0,
                "records_read": 0,
                "inserted": 0,
                "skipped_dedup": 0,
                "parse_errors": 0,
            }
            self.by_vendor[source] = bucket
        return bucket


class BronzeLoader:
    """Stream JSONL records into the bronze ``raw_product_snapshot`` table.

    One instance per CLI invocation. Iterates Hive-partitioned files under
    ``raw_data_dir``, batches per-vendor, and inserts with
    ``ON CONFLICT DO NOTHING`` against the ``uq_raw_source_external_hash``
    constraint so re-runs are idempotent.
    """

    def __init__(
        self,
        *,
        since: dt.date,
        raw_data_dir: Path,
        tracker: RunTracker,
        run_id: str,
        session_factory: Callable[[], AbstractContextManager[Session]] = get_session,
        batch_size: int = 500,
        log: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._since = since
        self._raw_data_dir = raw_data_dir
        self._tracker = tracker
        self._run_id = run_id
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._log = log or structlog.get_logger(__name__)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> LoadStats:
        stats = LoadStats()
        current_source: str | None = None
        batch: list[dict[str, Any]] = []

        for source, _scrape_date, path in self._discover_files():
            stats.files_scanned += 1
            stats._vendor_bucket(source)["files_scanned"] += 1

            # Flush any pending batch when switching vendors — batches stay
            # per-vendor so by_vendor stats and the constraint scoping stay
            # clean (plan §11 anti-pattern: cross-vendor batch contamination).
            if current_source is not None and source != current_source and batch:
                self._flush(batch, current_source, stats)
                batch = []
            current_source = source

            for record in self._iter_records(path, source, stats):
                stats.records_read += 1
                stats._vendor_bucket(source)["records_read"] += 1
                batch.append(self._build_row(record))
                if len(batch) >= self._batch_size:
                    self._flush(batch, source, stats)
                    batch = []

        # Drain the tail batch for the last vendor we processed.
        if batch and current_source is not None:
            self._flush(batch, current_source, stats)

        self._log.info(
            "load.run.complete",
            files_scanned=stats.files_scanned,
            records_read=stats.records_read,
            inserted=stats.inserted,
            skipped_dedup=stats.skipped_dedup,
            parse_errors=stats.parse_errors,
            by_vendor=stats.by_vendor,
        )
        return stats

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _discover_files(self) -> Iterator[tuple[str, dt.date, Path]]:
        """Yield ``(source, scrape_date, path)`` for JSONL on/after ``since``.

        Sorted by ``(date, source, filename)`` so the run is deterministic
        and per-vendor batches are contiguous in the outer loop.
        """
        if not self._raw_data_dir.exists():
            return

        candidates: list[tuple[dt.date, str, Path]] = []
        for path in self._raw_data_dir.glob("source=*/date=*/run=*.jsonl"):
            rel = path.relative_to(self._raw_data_dir).as_posix()
            m = _PARTITION_RE.match(rel)
            if m is None:
                # A stray file in the partition tree (e.g. a manual `.bak`
                # backup) — skip silently, log at debug for forensics.
                self._log.debug("load.file.skipped_unparseable", path=str(path))
                continue
            try:
                scrape_date = dt.date.fromisoformat(m.group("date"))
            except ValueError:
                self._log.debug("load.file.skipped_bad_date", path=str(path))
                continue
            if scrape_date < self._since:
                continue
            candidates.append((scrape_date, m.group("source"), path))

        candidates.sort(key=lambda t: (t[0], t[1], t[2].name))
        for scrape_date, source, path in candidates:
            yield source, scrape_date, path

    def _iter_records(
        self, path: Path, source: str, stats: LoadStats
    ) -> Iterator[RawRecord]:
        """Yield :class:`RawRecord` instances; log + skip malformed lines.

        Per spec §11, a per-record parse failure is non-fatal — we record an
        error on the tracker and continue. Filesystem-level errors
        (``OSError``, etc.) propagate.
        """
        with path.open("r", encoding="utf-8") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    yield RawRecord.model_validate_json(stripped)
                except (json.JSONDecodeError, pydantic.ValidationError) as exc:
                    stats.parse_errors += 1
                    stats._vendor_bucket(source)["parse_errors"] += 1
                    summary = (
                        f"{path}:{lineno}: {type(exc).__name__}: "
                        f"{str(exc)[:200]}"
                    )
                    self._tracker.record_error(summary)
                    self._log.warning(
                        "load.record.parse_error",
                        path=str(path),
                        lineno=lineno,
                        error_type=type(exc).__name__,
                    )

    def _build_row(self, record: RawRecord) -> dict[str, Any]:
        """Build the insert dict for a bronze row.

        ``record.payload`` is passed by identity — never copied, never walked
        (spec §11 anti-pattern: payload mutation). ``payload_hash`` is
        computed from the in-memory dict; downstream we must never recompute
        it from a JSONB round-trip (see :func:`payload_hash` docstring).
        """
        meta = record.ingest_meta
        return {
            "source": meta.source,
            "external_id": meta.external_id,
            "scraped_at": meta.scraped_at,
            "run_id": meta.run_id,
            "payload": record.payload,
            "payload_hash": payload_hash(record.payload),
        }

    def _flush(
        self, rows: list[dict[str, Any]], source: str, stats: LoadStats
    ) -> None:
        """Insert one batch and update stats + tracker.

        Per-batch transaction. ``OperationalError`` / ``InterfaceError`` from
        the session_factory propagate (terminal — the caller finalizes the
        run as failed). ``IntegrityError`` here would be unexpected because
        ``ON CONFLICT DO NOTHING`` swallows the constraint hit; we still
        catch it defensively, log, and treat the batch as skipped so a single
        bad row can't poison the whole run.
        """
        batch_size = len(rows)
        try:
            inserted, skipped = self._flush_batch(rows)
        except IntegrityError as exc:
            # Should not fire: ON CONFLICT DO NOTHING already covers the
            # dedup constraint. If we land here, something else (an FK, a
            # NOT NULL drift after a migration) tripped — record and move on
            # rather than aborting the whole run.
            self._log.error(
                "load.batch.integrity_error",
                source=source,
                batch_size=batch_size,
                error=str(exc)[:200],
            )
            self._tracker.record_error(f"IntegrityError in batch for {source}: {exc}")
            stats.parse_errors += batch_size
            stats._vendor_bucket(source)["parse_errors"] += batch_size
            return

        stats.inserted += inserted
        stats.skipped_dedup += skipped
        bucket = stats._vendor_bucket(source)
        bucket["inserted"] += inserted
        bucket["skipped_dedup"] += skipped

        # Stream mid-flight progress to the tracker so `scrape_run.records_count`
        # grows visibly during long runs (mirrors ShopifyScraper precedent).
        if inserted > 0:
            self._tracker.record_success(inserted)

        self._log.info(
            "load.batch.flushed",
            source=source,
            batch_size=batch_size,
            inserted=inserted,
            skipped_dedup=skipped,
        )

    def _flush_batch(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        """Run the actual ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` SQL."""
        with self._session_factory() as session:
            stmt = (
                pg_insert(RawProductSnapshot)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_raw_source_external_hash")
                .returning(RawProductSnapshot.snapshot_id)
            )
            returned = session.execute(stmt).scalars().all()
        inserted = len(returned)
        skipped = len(rows) - inserted
        return inserted, skipped


__all__ = ["BronzeLoader", "LoadStats", "payload_hash"]
