"""Self-describing wrapper for raw JSONL records.

Spec reference: specs/tea_scrapers_v1_spec.md §5 (Raw Storage Layer).
The `payload` field carries the upstream object verbatim — see §11
("Don't mutate the payload"). Parsing happens downstream in normalize.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class IngestMeta(BaseModel):
    source: str
    scraped_at: datetime
    run_id: str
    endpoint: str
    record_index: int
    external_id: str  # vendor-side product ID


class RawRecord(BaseModel):
    ingest_meta: IngestMeta
    payload: dict[str, Any]
