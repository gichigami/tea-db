"""End-to-end VCR-replayed scrape of ``crimsonlotustea.com``.

Spec reference: ``specs/tea_scrapers_v1_spec.md`` §9.

The cassette in ``fixtures/cassettes/crimson_lotus_products.yaml`` was
recorded once against the live storefront; CI replays it offline. To
re-record (annually or when Shopify's payload shape shifts), run::

    VCR_RECORD_MODE=once pytest \\
        tests/integration/test_shopify_crimson_lotus.py::test_cli_full_run_against_cassette

and verify ``git diff`` shows no leaked cookies / UA email before committing.
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

CASSETTE = "crimson_lotus_products.yaml"
SOURCE_KEY = "crimson_lotus"
GOLDEN_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "golden" / f"{SOURCE_KEY}.jsonl"
)
DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/tea",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_jsonl_path(raw_dir: Path) -> Path:
    """Locate the single JSONL file produced under a Hive-partitioned tree."""
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
    """Invoke ``tea-scrape ingest shopify --vendor ...`` with raw output rerouted.

    Always points ``DATABASE_URL`` at the test Postgres — the CLI opens a
    ``RunTracker`` which writes a ``scrape_run`` row, so we can't dodge the
    DB entirely even for cassette-only tests. (Without a reachable DB the
    CLI would exit 2 before any HTTP work happens.)
    """
    env = {"RAW_DATA_DIR": str(raw_dir), "DATABASE_URL": DB_URL}
    return runner.invoke(
        cli,
        ["ingest", "shopify", "--vendor", SOURCE_KEY, "--mode", "full"],
        env=env,
        catch_exceptions=False,
    )


@contextmanager
def _scoped_settings_cwd(target_dir: Path) -> Iterator[None]:
    """Run the CLI body with cwd at the repo root so ``config/vendors.yaml`` resolves.

    ``load_shopify_vendors()`` reads ``Path("config/vendors.yaml")`` relative
    to the process cwd. The integration test's tmp_path doesn't contain that
    file, so we temporarily chdir to the repo root for the CLI invocation.
    """
    repo_root = target_dir
    prev = os.getcwd()
    os.chdir(repo_root)
    try:
        yield
    finally:
        os.chdir(prev)


def _repo_root() -> Path:
    # tests/integration/test_X.py → up 3 → tea-scrapers/
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Postgres skip plumbing (mirrors tests/unit/test_run_tracker.py)
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
    """Yield a list mutated by the test; deletes those ``scrape_run`` rows on teardown."""
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
    assert len(records) > 0, "cassette replayed but no records were written"

    # Every record round-trips through RawRecord.model_validate_json — this
    # is the gate that protects downstream loaders from schema drift.
    for r in records:
        assert r.ingest_meta.source == SOURCE_KEY
        assert r.ingest_meta.external_id  # non-empty
        assert r.ingest_meta.endpoint.startswith("https://crimsonlotustea.com/")
        assert isinstance(r.payload, dict)
        assert "id" in r.payload


def test_payload_byte_equivalence_for_sample_product(vcr_cassette, tmp_path: Path):
    """Anti-pattern check: scraper must not mutate the upstream payload in transit."""
    raw_dir = tmp_path / "raw"
    runner = CliRunner()
    with vcr_cassette.use_cassette(CASSETTE) as cassette, _scoped_settings_cwd(_repo_root()):
        result = _invoke_cli(runner, raw_dir)

        assert result.exit_code == 0

        # Reconstruct the upstream pages from the cassette so we can compare
        # the JSONL output to the *server-returned* product objects, not to
        # whatever pytest-httpx synthesized.
        upstream_products_by_id: dict[str, dict] = {}
        for interaction in cassette.requests:
            response = cassette.responses_of(interaction)[0]
            body = response["body"]["string"]
            if isinstance(body, bytes):
                body = body.decode("utf-8")
            data = json.loads(body)
            for product in data.get("products", []):
                upstream_products_by_id[str(product["id"])] = product

    records = _read_records(raw_dir)
    # Pick a product with rich body_html *and* multiple variants — that's
    # the worst case for payload mutation (HTML stripping, variant filtering).
    candidates = [
        r
        for r in records
        if r.payload.get("body_html")
        and "<" in r.payload["body_html"]
        and len(r.payload.get("variants", [])) >= 2
    ]
    assert candidates, "cassette has no product with HTML body + >=2 variants"

    sample = candidates[0]
    expected = upstream_products_by_id[sample.ingest_meta.external_id]
    # Dict equality (not string) — JSON key ordering should not matter.
    assert sample.payload == expected, (
        f"payload mutated for external_id={sample.ingest_meta.external_id}"
    )


def test_replay_is_deterministic_across_runs(vcr_cassette, tmp_path: Path):
    """Two back-to-back replays produce byte-identical JSONL.

    Implicit if ``record_mode='none'`` and the cassette is the only source of
    truth, but worth pinning explicitly: a non-determinism creep (e.g. ULID
    leaking into the payload, system clock in the JSON) would break
    reproducibility for golden-fixture comparison.
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

    # ingest_meta carries scraped_at (wall clock) and run_id (ULID) — drop
    # those to compare the *content* the scraper extracts, which is what
    # downstream loaders dedup on via payload_hash.
    def _stable(rec: dict) -> dict:
        meta = {k: v for k, v in rec["ingest_meta"].items() if k not in {"scraped_at", "run_id"}}
        return {"ingest_meta": meta, "payload": rec["payload"]}

    assert [_stable(r) for r in first] == [_stable(r) for r in second]


def test_cassette_contains_pagination_terminator(vcr_cassette, tmp_path: Path):
    """The cassette must include the empty terminator page so we exercise the loop end."""
    raw_dir = tmp_path / "raw"
    runner = CliRunner()
    with vcr_cassette.use_cassette(CASSETTE) as cassette, _scoped_settings_cwd(_repo_root()):
        result = _invoke_cli(runner, raw_dir)
        assert result.exit_code == 0
        empty_pages = 0
        total_pages = 0
        for interaction in cassette.requests:
            total_pages += 1
            response = cassette.responses_of(interaction)[0]
            body = response["body"]["string"]
            if isinstance(body, bytes):
                body = body.decode("utf-8")
            data = json.loads(body)
            if not data.get("products"):
                empty_pages += 1

    assert total_pages >= 2, (
        f"cassette has only {total_pages} request(s); need >=2 pages to exercise pagination"
    )
    assert empty_pages == 1, (
        f"cassette must contain exactly one empty-terminator page, found {empty_pages}"
    )


def test_scrape_run_row_finalized_as_success(
    vcr_cassette, tmp_path: Path, pg_cleanup: list[str]
):
    """When Postgres is available, the scrape_run row must be finalized to 'success'."""
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
    """Compact pytest node id for a golden line — keep the test report readable."""
    try:
        return f"idx={json.loads(line)['ingest_meta']['record_index']}"
    except Exception:
        return "malformed"


@pytest.mark.parametrize("line", _golden_lines(), ids=_golden_id)
def test_golden_fixture_lines_parse_as_raw_records(line: str):
    """Each golden JSONL line must validate as :class:`RawRecord`.

    Protects the fixture from rotting independently of the cassette — if a
    spec change tightens ``IngestMeta`` and we forget to regenerate the
    golden file, this test catches it before the silver normalizer trips.
    """
    record = RawRecord.model_validate_json(line)
    assert record.ingest_meta.source == SOURCE_KEY
    assert record.ingest_meta.run_id  # deterministic synthetic ULID
    assert record.ingest_meta.external_id
    assert isinstance(record.payload, dict)
    assert "id" in record.payload
