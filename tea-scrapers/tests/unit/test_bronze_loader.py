"""Unit tests for :mod:`tea_scrapers.load.bronze`.

These tests cover the pure-Python parts of the loader: ``payload_hash``
canonicalization, file discovery + ``--since`` filtering, parse-error
isolation, payload-identity preservation in ``_build_row``, and tracker
streaming in ``run()``. Postgres-backed paths live in the integration
suite.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tea_scrapers.load.bronze import BronzeLoader, LoadStats, payload_hash
from tea_scrapers.schemas.ingest import RawRecord

# ---------------------------------------------------------------------------
# payload_hash
# ---------------------------------------------------------------------------


def test_payload_hash_stable_across_key_order() -> None:
    """Same logical content, different insertion order → same hash."""
    a = {"title": "Spring 2024 Bingdao", "price": "120.00", "available": True}
    b = {"available": True, "price": "120.00", "title": "Spring 2024 Bingdao"}
    assert payload_hash(a) == payload_hash(b)


def test_payload_hash_changes_on_nested_mutation() -> None:
    """Flipping one nested field produces a different hash."""
    base = {
        "id": 123,
        "variants": [
            {"sku": "A1", "available": True, "price": "50.00"},
            {"sku": "A2", "available": True, "price": "95.00"},
        ],
    }
    mutated = json.loads(json.dumps(base))
    mutated["variants"][1]["available"] = False
    assert payload_hash(base) != payload_hash(mutated)


def test_payload_hash_handles_unicode() -> None:
    """Hanzi tea names hash stably without UnicodeEncodeError."""
    payload = {
        "title": "白茶寿眉 2018",
        "vendor": "白毫银针",
        "tags": ["白茶", "老白茶"],
    }
    # Two independent calls return the same hash and don't raise.
    h1 = payload_hash(payload)
    h2 = payload_hash(dict(payload))
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_payload_hash_recurses_into_list_of_dicts() -> None:
    """Key order within nested ``variants`` dicts doesn't matter."""
    a = {
        "id": 1,
        "variants": [
            {"sku": "X", "price": "10.00", "available": True},
            {"sku": "Y", "price": "20.00", "available": False},
        ],
    }
    b = {
        "id": 1,
        "variants": [
            {"available": True, "price": "10.00", "sku": "X"},
            {"available": False, "price": "20.00", "sku": "Y"},
        ],
    }
    assert payload_hash(a) == payload_hash(b)


# ---------------------------------------------------------------------------
# Discovery + filtering
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False))
            fh.write("\n")


def _record_dict(source: str, idx: int, ext_id: str | None = None) -> dict[str, Any]:
    return {
        "ingest_meta": {
            "source": source,
            "scraped_at": "2026-05-16T00:00:00Z",
            "run_id": "01HUNIT00000000000000000000",
            "endpoint": f"https://example.com/{source}/products.json?page=1",
            "record_index": idx,
            "external_id": ext_id or f"ext-{idx}",
        },
        "payload": {"id": idx, "title": f"Tea {idx}"},
    }


def _noop_tracker() -> MagicMock:
    tracker = MagicMock()
    tracker.record_error = MagicMock()
    tracker.record_success = MagicMock()
    return tracker


def _loader(
    tmp_path: Path,
    *,
    since: dt.date,
    tracker: MagicMock | None = None,
    session_factory: Any = None,
    batch_size: int = 500,
) -> BronzeLoader:
    return BronzeLoader(
        since=since,
        raw_data_dir=tmp_path,
        tracker=tracker or _noop_tracker(),
        run_id="01HUNIT00000000000000000000",
        session_factory=session_factory or (lambda: _NullSessionCM()),
        batch_size=batch_size,
    )


class _NullSession:
    """Stand-in ``Session`` that records executed statements but performs no DB I/O."""

    def __init__(self) -> None:
        self.executed: list[Any] = []

    def execute(self, stmt: Any) -> Any:
        self.executed.append(stmt)
        result = MagicMock()
        # By default behave as "everything inserted" — tests that need
        # different semantics override this via a custom session_factory.
        result.scalars.return_value.all.return_value = []
        return result


class _NullSessionCM:
    def __init__(self, session: _NullSession | None = None) -> None:
        self.session = session or _NullSession()

    def __enter__(self) -> _NullSession:
        return self.session

    def __exit__(self, *exc: Any) -> None:
        return None


def test_discover_files_filters_by_since(tmp_path: Path) -> None:
    """Only date partitions on/after ``since`` are returned."""
    _write_jsonl(
        tmp_path / "source=vendorA/date=2026-05-10/run=01.jsonl",
        [_record_dict("vendorA", 1)],
    )
    _write_jsonl(
        tmp_path / "source=vendorA/date=2026-05-15/run=02.jsonl",
        [_record_dict("vendorA", 2)],
    )
    _write_jsonl(
        tmp_path / "source=vendorB/date=2026-05-16/run=03.jsonl",
        [_record_dict("vendorB", 3)],
    )

    loader = _loader(tmp_path, since=dt.date(2026, 5, 15))
    discovered = list(loader._discover_files())

    dates = {d for _src, d, _path in discovered}
    sources = {src for src, _d, _path in discovered}
    assert dates == {dt.date(2026, 5, 15), dt.date(2026, 5, 16)}
    assert sources == {"vendorA", "vendorB"}
    assert len(discovered) == 2


def test_discover_files_ignores_non_jsonl(tmp_path: Path) -> None:
    """A stray ``.txt`` in a date partition is not picked up."""
    _write_jsonl(
        tmp_path / "source=vendorA/date=2026-05-16/run=01.jsonl",
        [_record_dict("vendorA", 1)],
    )
    stray = tmp_path / "source=vendorA/date=2026-05-16/notes.txt"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("this should never be parsed as JSONL\n", encoding="utf-8")

    loader = _loader(tmp_path, since=dt.date(2026, 5, 1))
    paths = [p for _src, _d, p in loader._discover_files()]
    assert paths == [tmp_path / "source=vendorA/date=2026-05-16/run=01.jsonl"]


# ---------------------------------------------------------------------------
# _iter_records + parse-error isolation
# ---------------------------------------------------------------------------


def test_iter_records_skips_malformed_json(tmp_path: Path) -> None:
    """A valid line + a malformed JSON line + a schema-invalid line →
    yields 1 record, records 2 errors on the tracker."""
    valid_line = json.dumps(_record_dict("vendorA", 0, ext_id="A0"))
    malformed_line = "{this-is-not-json"
    # Valid JSON but missing required fields → pydantic.ValidationError.
    bad_schema_line = json.dumps({"ingest_meta": {"source": "vendorA"}, "payload": {}})

    path = tmp_path / "source=vendorA/date=2026-05-16/run=01.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([valid_line, malformed_line, bad_schema_line, ""]),
        encoding="utf-8",
    )

    tracker = _noop_tracker()
    loader = _loader(tmp_path, since=dt.date(2026, 5, 1), tracker=tracker)
    stats = LoadStats()
    records = list(loader._iter_records(path, "vendorA", stats))

    assert len(records) == 1
    assert records[0].ingest_meta.external_id == "A0"
    assert stats.parse_errors == 2
    assert tracker.record_error.call_count == 2


def test_build_row_passes_payload_verbatim(tmp_path: Path) -> None:
    """``_build_row`` must pass ``record.payload`` by identity, not by copy.

    Spec §11 anti-pattern: payload mutation. The loader is the only thing
    between JSONL and Postgres — if it copies or re-encodes the dict here,
    downstream byte-equivalence guarantees evaporate.
    """
    golden = (
        Path(__file__).parent.parent
        / "fixtures"
        / "golden"
        / "white2tea.jsonl"
    )
    line = golden.read_text(encoding="utf-8").splitlines()[0]
    record = RawRecord.model_validate_json(line)

    loader = _loader(tmp_path, since=dt.date(2026, 5, 1))
    row = loader._build_row(record)

    assert row["payload"] is record.payload  # identity, not equality
    assert row["source"] == record.ingest_meta.source
    assert row["external_id"] == record.ingest_meta.external_id
    assert row["scraped_at"] == record.ingest_meta.scraped_at
    assert row["run_id"] == record.ingest_meta.run_id
    assert row["payload_hash"] == payload_hash(record.payload)


# ---------------------------------------------------------------------------
# run() — tracker streaming
# ---------------------------------------------------------------------------


def test_run_streams_record_success_per_batch(tmp_path: Path) -> None:
    """``run()`` calls ``tracker.record_success`` once per non-empty batch."""
    # Stage 5 records under one vendor, force batch_size=2 → expect 3 flushes
    # (2 + 2 + 1) and 3 record_success calls with counts 2, 2, 1.
    records = [_record_dict("vendorA", i, ext_id=f"A{i}") for i in range(5)]
    path = tmp_path / "source=vendorA/date=2026-05-16/run=01.jsonl"
    _write_jsonl(path, records)

    tracker = _noop_tracker()

    @contextmanager
    def session_cm() -> Iterator[_NullSession]:
        sess = _NullSession()

        def fake_execute(stmt: Any) -> Any:
            sess.executed.append(stmt)
            # ``pg_insert(...).values([rows])`` stores the batch on
            # ``_multi_values`` as a one-tuple of a list of column→value
            # dicts. Count that to fabricate a RETURNING result whose
            # length equals the batch size (i.e. "everything inserted").
            multi = stmt._multi_values  # noqa: SLF001 — test introspection
            row_count = len(multi[0]) if multi else 0
            result = MagicMock()
            result.scalars.return_value.all.return_value = list(range(row_count))
            return result

        sess.execute = fake_execute  # type: ignore[method-assign]
        yield sess

    loader = _loader(
        tmp_path,
        since=dt.date(2026, 5, 1),
        tracker=tracker,
        session_factory=session_cm,
        batch_size=2,
    )
    stats = loader.run()

    assert stats.records_read == 5
    assert stats.inserted == 5
    assert stats.skipped_dedup == 0
    # 3 batches: 2 + 2 + 1
    assert tracker.record_success.call_count == 3
    counts = [c.args[0] for c in tracker.record_success.call_args_list]
    assert counts == [2, 2, 1]


# ---------------------------------------------------------------------------
# Module sanity
# ---------------------------------------------------------------------------


def test_no_broad_except_in_bronze_module() -> None:
    """Spec §11: ``except Exception:`` is forbidden in loader source."""
    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "tea_scrapers"
        / "load"
        / "bronze.py"
    ).read_text(encoding="utf-8")
    assert "except Exception" not in src, (
        "bronze.py must use narrow exception catches (spec §11 anti-pattern)"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"a": 1},
        {"unicode": "白茶"},
        {"nested": {"x": [1, 2, {"y": 3}]}},
    ],
)
def test_payload_hash_is_hex_sha256(payload: dict[str, Any]) -> None:
    h = payload_hash(payload)
    assert len(h) == 64
    int(h, 16)  # raises if non-hex
