"""End-to-end VCR-replayed scrape of Steepster (one vendor slug, 10 teas).

Spec reference: ``specs/tea_scrapers_v1_spec.md`` §6.2 + §9.

The cassette in ``fixtures/cassettes/steepster_crimson_lotus_tea.yaml.gz`` was
recorded once against the live community site with ``max_teas_per_vendor=10``;
CI replays it offline. The gzip extension routes through
:class:`GzipFilesystemPersister` (see ``tests/_vcr_gzip.py``). To re-record
(annually, or when steepster's markup shifts)::

    VCR_RECORD_MODE=once pytest \\
        tests/integration/test_steepster.py::test_cli_full_run_against_cassette

and verify ``zcat fixtures/cassettes/steepster_crimson_lotus_tea.yaml.gz |
grep -iE 'set-cookie|authorization'`` produces no matches before committing
(spec §9 leak-audit procedure).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tea_scrapers.cli import cli
from tea_scrapers.schemas.ingest import RawRecord
from tea_scrapers.storage.models import ScrapeRun

CASSETTE = "steepster_crimson_lotus_tea.yaml.gz"
SOURCE_KEY = "steepster"
VENDOR_SLUG = "crimson-lotus-tea"
MAX_TEAS = 10
GOLDEN_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "golden" / f"{SOURCE_KEY}.jsonl"
)
DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/tea",
)


# ---------------------------------------------------------------------------
# Helpers (mirror tests/integration/test_shopify_yunnansourcing_us.py)
# ---------------------------------------------------------------------------


def _read_jsonl_path(raw_dir: Path) -> Path:
    matches = list(raw_dir.rglob("*.jsonl"))
    assert len(matches) == 1, (
        f"expected exactly one JSONL under {raw_dir}, found {len(matches)}: {matches}"
    )
    return matches[0]


def _read_records(raw_dir: Path) -> list[RawRecord]:
    path = _read_jsonl_path(raw_dir)
    lines = path.read_text(encoding="utf-8").splitlines()
    return [RawRecord.model_validate_json(line) for line in lines]


def _invoke_cli(runner: CliRunner, raw_dir: Path) -> "object":
    # ``STEEPSTER_RATE_LIMIT_RPS`` overrides the YAML default (0.1) so the
    # cassette replay finishes in seconds rather than the ~110s a strict
    # crawl-delay walk would cost on 11 mock requests. The rate limiter has
    # no awareness of VCR interception, so without this override every
    # replay would sleep against a wall clock just to send a fake request.
    # The committed YAML default remains the polite-citizen 0.1 rps.
    env = {
        "RAW_DATA_DIR": str(raw_dir),
        "DATABASE_URL": DB_URL,
        "STEEPSTER_RATE_LIMIT_RPS": "100",
    }
    return runner.invoke(
        cli,
        [
            "ingest",
            "steepster",
            "--vendor",
            VENDOR_SLUG,
            "--max-teas",
            str(MAX_TEAS),
            "--mode",
            "full",
        ],
        env=env,
        catch_exceptions=False,
    )


@contextmanager
def _scoped_settings_cwd(target_dir: Path) -> Iterator[None]:
    """``load_steepster_config()`` reads ``Path("config/vendors.yaml")``; chdir to repo root."""
    prev = os.getcwd()
    os.chdir(target_dir)
    try:
        yield
    finally:
        os.chdir(prev)


def _repo_root() -> Path:
    # tests/integration/test_X.py → up 3 → tea-scrapers/
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Postgres skip plumbing (mirrors shopify integration tests)
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
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    created: list[str] = []
    yield created
    sess: Session = factory()
    try:
        for rid in created:
            row = sess.get(ScrapeRun, rid)
            if row is not None:
                sess.delete(row)
        sess.commit()
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_full_run_against_cassette(vcr_cassette, tmp_path: Path):
    """Cassette-driven scrape produces well-formed JSONL with exit code 0."""
    raw_dir = tmp_path / "raw"
    runner = CliRunner()
    with vcr_cassette.use_cassette(CASSETTE), _scoped_settings_cwd(_repo_root()):
        result = _invoke_cli(runner, raw_dir)

    assert result.exit_code == 0, f"CLI failed: {result.output}"

    records = _read_records(raw_dir)
    assert len(records) == MAX_TEAS, (
        f"expected {MAX_TEAS} records under --max-teas={MAX_TEAS}, "
        f"got {len(records)}"
    )

    # Every record validates and meets the §6.2 schema.
    for r in records:
        assert r.ingest_meta.source == SOURCE_KEY
        assert r.ingest_meta.external_id  # non-empty steepster tea_id
        assert r.ingest_meta.endpoint.startswith(
            f"https://steepster.com/teas/{VENDOR_SLUG}/"
        )
        assert isinstance(r.payload, dict)
        assert r.payload["vendor_slug"] == VENDOR_SLUG
        assert r.payload["steepster_id"] == r.ingest_meta.external_id
        assert "tasting_notes" in r.payload
        assert isinstance(r.payload["tasting_notes"], list)


def test_no_plaintext_author_field_in_notes(vcr_cassette, tmp_path: Path):
    """Anti-pattern check: per-note ``author_hash`` is the ONLY author marker.

    The §6.2 brief is explicit: "Hash author names rather than capturing
    them, for downstream privacy hygiene." This test pins the structural
    invariant: every note in the JSONL has an ``author_hash`` (sha256-
    prefixed) and **no plaintext author/username field**. We deliberately
    scope to the per-note structure (rather than substring-matching usernames
    across the whole JSONL) because reviewers often mention other reviewers
    by name in the body text — that's content the reviewer typed, not
    something the scraper synthesized, and stripping it would violate the
    "capture every record verbatim" anti-pattern (§11).
    """
    raw_dir = tmp_path / "raw"
    runner = CliRunner()
    with vcr_cassette.use_cassette(CASSETTE), _scoped_settings_cwd(_repo_root()):
        result = _invoke_cli(runner, raw_dir)
        assert result.exit_code == 0

    forbidden_keys = {"author", "author_name", "username", "user", "user_name"}
    records = _read_records(raw_dir)

    assert sum(len(r.payload["tasting_notes"]) for r in records) > 0, (
        "cassette has zero tasting notes — re-record before asserting hash-only invariant"
    )

    for r in records:
        for note in r.payload["tasting_notes"]:
            # Hash present and well-formed.
            assert "author_hash" in note, f"note missing author_hash: {note!r}"
            assert note["author_hash"].startswith("sha256:")
            assert len(note["author_hash"]) == len("sha256:") + 64
            # No plaintext author key under any common name.
            leaked_keys = forbidden_keys & set(note.keys())
            assert not leaked_keys, (
                f"plaintext author key(s) on note: {leaked_keys!r}. "
                "Hash author names at scrape time (spec §6.2)."
            )


def test_author_hashes_use_sha256_prefix(vcr_cassette, tmp_path: Path):
    raw_dir = tmp_path / "raw"
    runner = CliRunner()
    with vcr_cassette.use_cassette(CASSETTE), _scoped_settings_cwd(_repo_root()):
        result = _invoke_cli(runner, raw_dir)
    assert result.exit_code == 0

    records = _read_records(raw_dir)
    seen_hashes: set[str] = set()
    for r in records:
        for note in r.payload["tasting_notes"]:
            h = note["author_hash"]
            assert h.startswith("sha256:"), f"author_hash missing prefix: {h!r}"
            assert len(h) == len("sha256:") + 64, (
                f"author_hash wrong length: {h!r}"
            )
            seen_hashes.add(h)
    # Cassette has more than one author across 10 teas — otherwise the
    # hash invariants above are testing a single value.
    assert len(seen_hashes) > 1


def test_replay_is_deterministic_across_runs(vcr_cassette, tmp_path: Path):
    """Two back-to-back replays produce content-identical JSONL.

    Per spec §11 "Don't mutate the payload": the scraper output (modulo
    `scraped_at` + `run_id` in `ingest_meta`) must be stable across runs of
    the same cassette. Without this, the bronze loader's `payload_hash`
    dedup invariant would break.
    """
    runner = CliRunner()

    def _run(into: Path) -> list[dict]:
        with vcr_cassette.use_cassette(CASSETTE), _scoped_settings_cwd(_repo_root()):
            result = _invoke_cli(runner, into)
        assert result.exit_code == 0
        path = _read_jsonl_path(into)
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    first = _run(tmp_path / "run1")
    second = _run(tmp_path / "run2")

    def _stable(rec: dict) -> dict:
        meta = {
            k: v
            for k, v in rec["ingest_meta"].items()
            if k not in {"scraped_at", "run_id"}
        }
        return {"ingest_meta": meta, "payload": rec["payload"]}

    assert [_stable(r) for r in first] == [_stable(r) for r in second]


def test_scrape_run_row_finalized_as_success(
    vcr_cassette, tmp_path: Path, pg_cleanup: list[str]
):
    """When Postgres is available, the scrape_run row must finalize to 'success'."""
    raw_dir = tmp_path / "raw"
    runner = CliRunner()
    with vcr_cassette.use_cassette(CASSETTE), _scoped_settings_cwd(_repo_root()):
        result = _invoke_cli(runner, raw_dir)
    assert result.exit_code == 0

    line_count = len(_read_records(raw_dir))

    factory = sessionmaker(bind=create_engine(DB_URL, future=True), future=True)
    with factory() as sess:
        row = sess.execute(
            select(ScrapeRun)
            .where(ScrapeRun.source == SOURCE_KEY)
            .order_by(ScrapeRun.started_at.desc())
            .limit(1)
        ).scalar_one()
        pg_cleanup.append(row.run_id)
        assert row.status == "success", f"status={row.status} summary={row.error_summary}"
        assert row.records_count == line_count
        assert row.errors_count == 0
        assert row.finished_at is not None


def test_golden_payloads_match_cassette(vcr_cassette, tmp_path: Path):
    """Every golden record's payload must equal the cassette-replay output
    for the same ``external_id``.

    Mirrors the Shopify-quartet ``test_golden_payloads_match_cassette``:
    catches the bug where a golden sampled from a different scrape than the
    committed cassette silently drifts on timestamp-like fields.
    """
    golden_lines = _golden_lines()
    if not golden_lines:
        pytest.skip("no golden fixture")

    raw_dir = tmp_path / "raw"
    runner = CliRunner()
    with vcr_cassette.use_cassette(CASSETTE), _scoped_settings_cwd(_repo_root()):
        result = _invoke_cli(runner, raw_dir)
    assert result.exit_code == 0, f"CLI failed: {result.output}"

    cassette_payloads = {
        r.ingest_meta.external_id: r.payload for r in _read_records(raw_dir)
    }

    drifts: list[str] = []
    for line in golden_lines:
        golden = json.loads(line)
        ext_id = golden["ingest_meta"]["external_id"]
        if ext_id not in cassette_payloads:
            drifts.append(f"  external_id={ext_id}: in golden but not in cassette")
            continue
        if golden["payload"] != cassette_payloads[ext_id]:
            drifts.append(
                f"  external_id={ext_id}: golden payload differs from cassette payload"
            )

    assert not drifts, "golden ↔ cassette drift:\n" + "\n".join(drifts)


# ---------------------------------------------------------------------------
# Golden fixture sanity
# ---------------------------------------------------------------------------


def _golden_lines() -> list[str]:
    if not GOLDEN_FIXTURE.exists():
        return []
    return [
        line
        for line in GOLDEN_FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _golden_id(line: str) -> str:
    try:
        return f"idx={json.loads(line)['ingest_meta']['record_index']}"
    except Exception:
        return "malformed"


@pytest.mark.parametrize("line", _golden_lines(), ids=_golden_id)
def test_golden_fixture_lines_parse_as_raw_records(line: str):
    """Each golden JSONL line must validate as :class:`RawRecord`."""
    record = RawRecord.model_validate_json(line)
    assert record.ingest_meta.source == SOURCE_KEY
    assert record.ingest_meta.run_id  # synthetic ULID
    assert record.ingest_meta.external_id
    assert isinstance(record.payload, dict)
    assert "steepster_id" in record.payload
    assert record.payload["vendor_slug"] == VENDOR_SLUG
    assert "tasting_notes" in record.payload
