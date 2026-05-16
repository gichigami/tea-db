from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tea_scrapers.schemas.ingest import IngestMeta, RawRecord
from tea_scrapers.storage.raw import JsonlWriter


def _record(scraped_at: dt.datetime, index: int = 0, source: str = "demo_src") -> RawRecord:
    return RawRecord(
        ingest_meta=IngestMeta(
            source=source,
            scraped_at=scraped_at,
            run_id="01HXTESTULID00000000000000",
            endpoint="https://example.test/api?page=1",
            record_index=index,
            external_id=f"ext-{index}",
        ),
        payload={"name": "tea", "nested": {"a": 1}, "unicode": "茶"},
    )


def test_partition_path_matches_spec(tmp_path: Path):
    when = dt.datetime(2026, 5, 16, 14, 30, tzinfo=dt.timezone.utc)
    run_id = "01HXY4Z9TESTULID0000000000"
    with JsonlWriter(run_id=run_id, base_dir=tmp_path) as writer:
        writer.write(_record(when, source="yunnan_sourcing_us"))
    expected = tmp_path / "source=yunnan_sourcing_us" / "date=2026-05-16" / f"run={run_id}.jsonl"
    assert writer.path == expected
    assert expected.exists()


def test_records_round_trip_through_jsonl(tmp_path: Path):
    when = dt.datetime(2026, 5, 16, 14, 30, tzinfo=dt.timezone.utc)
    records = [_record(when, index=i) for i in range(3)]
    with JsonlWriter(run_id="01HXTESTULID00000000000000", base_dir=tmp_path) as writer:
        for r in records:
            writer.write(r)
    assert writer.records_written == 3

    assert writer.path is not None
    lines = writer.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    roundtripped = [RawRecord.model_validate_json(line) for line in lines]
    for original, parsed in zip(records, roundtripped, strict=True):
        assert parsed.ingest_meta.external_id == original.ingest_meta.external_id
        assert parsed.payload == original.payload


def test_empty_run_produces_no_file(tmp_path: Path):
    run_id = "01HXEMPTYULID000000000000A"
    with JsonlWriter(run_id=run_id, base_dir=tmp_path) as writer:
        pass
    assert writer.records_written == 0
    assert writer.path is None
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_utc_date_partition_for_naive_after_midnight(tmp_path: Path):
    # 2026-05-16 23:30 in UTC+10 is 2026-05-16 13:30 UTC — partition is UTC date.
    when = dt.datetime(2026, 5, 16, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=10)))
    with JsonlWriter(run_id="01HXUTCTEST00000000000000A", base_dir=tmp_path) as writer:
        writer.write(_record(when))
    assert writer.path is not None
    assert "date=2026-05-16" in writer.path.as_posix()


def test_payload_is_not_mutated(tmp_path: Path):
    when = dt.datetime(2026, 5, 16, tzinfo=dt.timezone.utc)
    record = _record(when)
    original_payload = json.loads(json.dumps(record.payload))
    with JsonlWriter(run_id="01HXNOMUTATETEST00000000A", base_dir=tmp_path) as writer:
        writer.write(record)
    assert record.payload == original_payload


@pytest.mark.parametrize("source", ["yunnan_sourcing_us", "steepster", "teadb", "reddit_puer"])
def test_source_appears_verbatim_in_path(tmp_path: Path, source: str):
    when = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    with JsonlWriter(run_id="01HXSRCTEST00000000000000A", base_dir=tmp_path) as writer:
        writer.write(_record(when, source=source))
    assert writer.path is not None
    assert f"source={source}" in writer.path.as_posix()
