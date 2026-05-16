"""Partitioned JSONL writer for raw scrape output (spec §5)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import TracebackType
from typing import IO

import structlog

from tea_scrapers.config import get_settings
from tea_scrapers.schemas.ingest import RawRecord


class JsonlWriter:
    """Append-only JSONL writer partitioned by source + UTC scrape date."""

    def __init__(
        self,
        run_id: str,
        base_dir: Path | None = None,
        log: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._run_id = run_id
        self._base_dir = base_dir or get_settings().raw_data_dir
        self._log = log or structlog.get_logger()
        self._fh: IO[str] | None = None
        self._path: Path | None = None
        self._count = 0

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def records_written(self) -> int:
        return self._count

    def write(self, record: RawRecord) -> None:
        if self._fh is None:
            self._open(record)
        payload_bytes = self._serialize(record)
        assert self._fh is not None
        self._fh.write(payload_bytes)
        self._fh.write("\n")
        self._count += 1
        self._log.debug(
            "scrape.record",
            external_id=record.ingest_meta.external_id,
            payload_bytes=len(payload_bytes),
            record_index=record.ingest_meta.record_index,
        )

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.flush()
            # fsync — cron runs unsupervised; an unflushed page cache on power loss is wasted work.
            os.fsync(self._fh.fileno())
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _open(self, record: RawRecord) -> None:
        utc_date = record.ingest_meta.scraped_at.utctimetuple()
        date_str = f"{utc_date.tm_year:04d}-{utc_date.tm_mon:02d}-{utc_date.tm_mday:02d}"
        directory = (
            self._base_dir
            / f"source={record.ingest_meta.source}"
            / f"date={date_str}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        self._path = directory / f"run={self._run_id}.jsonl"
        self._fh = self._path.open("a", encoding="utf-8")

    @staticmethod
    def _serialize(record: RawRecord) -> str:
        return json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )


__all__ = ["JsonlWriter"]
