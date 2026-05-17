"""Unit tests for the Steepster HTML scraper (spec §6.2).

Two concerns covered:
- Author hashing: stability, NFC normalization, case-insensitivity, salt
  absence (i.e. reproducibility across runs).
- HTML parsing: company-index URL collection, pagination terminators,
  tasting-note extraction, missing-field tolerance, payload shape.

Integration-level end-to-end test (CLI + VCR cassette + golden JSONL) lives
in `tests/integration/test_steepster.py`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import httpx
import pytest
import structlog

from tea_scrapers.config import SteepsterConfig
from tea_scrapers.http.client import HttpClient, ScrapeError
from tea_scrapers.http.ratelimit import HostRateLimiter
from tea_scrapers.schemas.ingest import RawRecord
from tea_scrapers.sources.steepster import (
    SOURCE_KEY,
    SteepsterScraper,
    hash_author,
)
from tea_scrapers.storage.raw import JsonlWriter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_logs():
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        cache_logger_on_first_use=False,
    )
    yield cap
    structlog.reset_defaults()


@pytest.fixture
def config() -> SteepsterConfig:
    return SteepsterConfig(
        base_url="https://steepster.test",
        rate_limit_rps=100.0,  # fast for unit tests; rate-limit honored in integ
        timeout_seconds=30.0,
        vendor_slugs=["yunnan-sourcing"],
    )


def _http_client() -> HttpClient:
    return HttpClient(
        rate_limiter=HostRateLimiter(default_rps=100.0),
        sleep=lambda _s: None,
        client=httpx.Client(),
    )


# ---------------------------------------------------------------------------
# Fixture HTML
# ---------------------------------------------------------------------------

# Compact synthetic markup that matches the *load-bearing* structural
# patterns of real steepster pages observed on 2026-05-16:
# - Company-index pagination uses `<li><a href='?page=N'>Next</a></li>`
# - Tea-detail URLs match `^/teas/{vendor-slug}/{tea_id}-{tea_slug}/?$`
# - Tea metadata uses Schema.org microdata (`itemprop=name`,
#   `itemprop=reviewCount`, `itemprop=ratingValue`)
# - Tasting notes are `<div id='note_<id>' class='note'>` blocks with
#   nested microdata (author, rating, body, datePublished)


def _company_page_html(
    *,
    vendor_slug: str = "yunnan-sourcing",
    tea_ids: tuple[tuple[int, str], ...] = (),
    has_next: bool = False,
    next_page: int = 2,
) -> str:
    """Build a synthetic /companies/{slug} page."""
    tea_links = "".join(
        f"<a href='/teas/{vendor_slug}/{tid}-{slug}'>Tea {tid}</a>"
        for tid, slug in tea_ids
    )
    pagination = (
        f"<ul class='pagination'><li><a href='?page={next_page}'>Next</a></li></ul>"
        if has_next
        else ""
    )
    return dedent(
        f"""
        <!DOCTYPE html>
        <html><body>
          <div id='teas-list'>{tea_links}</div>
          {pagination}
        </body></html>
        """
    )


def _tea_detail_html(
    *,
    tea_id: str = "41785",
    tea_slug: str = "imperial-gold-needle",
    vendor_slug: str = "yunnan-sourcing",
    name: str = "Imperial Gold Needle Yunnan Black Tea",
    average_rating: int = 88,
    review_count: int = 34,
    tea_type: str = "Black Tea",
    flavors: str = "Chocolate, Malt, Honey",
    notes: tuple[dict, ...] = (),
    has_next: bool = False,
) -> str:
    notes_html = "".join(_note_html(n) for n in notes)
    pagination = (
        "<ul class='pagination'><li><a href='?page=2#tasting-notes'>Next</a></li></ul>"
        if has_next
        else ""
    )
    return dedent(
        f"""
        <!DOCTYPE html>
        <html><body id='teas_show'>
          <section id='tea-bottom'>
            <h1 itemprop='name'>{name}</h1>
            <div id='ratings-box' itemprop='aggregateRating'>
              <meta itemprop='reviewCount' content='{review_count}'>
              <div id='rating-average' itemprop='ratingValue'>{average_rating}</div>
            </div>
            <dl class='tea-description'>
              <dt>Tea type</dt><dd>{tea_type}</dd>
              <dt>Flavors</dt><dd>{flavors}</dd>
              <dt>Caffeine</dt><dd>High</dd>
              <dt>Certification</dt><dd class='empty'>Not available</dd>
            </dl>
            <div class='tea-prep'>
              <h5>Average preparation</h5>
              <div class='prep-details'>
                <span class='temp'>205 &deg;F / 96 &deg;C</span>
                <span class='steep-time'>2 min, 45 sec</span>
                <span class='tea-amount'>5 g</span>
              </div>
            </div>
            <div id='availability'>
              <h4 class='unavailable'>Currently unavailable</h4>
            </div>
          </section>
          <section id='tasting-notes'>
            {notes_html}
            {pagination}
          </section>
        </body></html>
        """
    )


def _note_html(note: dict) -> str:
    """Build one `<div id='note_*' class='note'>` block."""
    note_id = note["note_id"]
    author = note["author"]
    rating = note.get("rating", 80)
    body = note.get("body", "Tasty.")
    posted_at = note.get("posted_at", "2014-02-16T17:03:08Z")
    likes = note.get("likes", 0)
    comments = note.get("comments", 0)
    return dedent(
        f"""
        <div id='note_{note_id}' class='note'>
          <div itemprop='review' itemscope itemtype='http://schema.org/Review'>
            <div class='user'>
              <span itemprop='author' itemscope>
                <meta itemprop='name' content='{author}'>
              </span>
              <div class='rating' itemprop='reviewRating'>
                <span itemprop='ratingValue'>{rating}</span>
              </div>
            </div>
            <div class='content'>
              <div class='text'>
                <span itemprop='description'><p>{body}</p></span>
              </div>
              <div class='meta'>
                <div class='timestamp'>
                  <abbr itemprop='datePublished' class='timeago'
                        title='{posted_at}' content='{posted_at}'></abbr>
                </div>
                <div class='like-count'>
                  <a class='likes-count' data-pluralize-count='{likes}'>
                    {likes} likes
                  </a>
                </div>
                <div class='comment-count'>
                  <a class='show-inline-comments' data-pluralize-count='{comments}'>
                    {comments} comments
                  </a>
                </div>
              </div>
            </div>
          </div>
        </div>
        """
    )


# ---------------------------------------------------------------------------
# hash_author
# ---------------------------------------------------------------------------


class TestHashAuthor:
    def test_returns_sha256_prefix(self):
        h = hash_author("Sil")
        assert h.startswith("sha256:")
        # 64 hex chars after prefix.
        assert len(h) == len("sha256:") + 64

    def test_stable_across_calls(self):
        # Reproducibility is load-bearing for cross-run dedup
        # (spec §12 step-7 OQ #5: unsalted is intentional).
        assert hash_author("Sil") == hash_author("Sil")

    def test_normalizes_case(self):
        assert hash_author("Sil") == hash_author("SIL")
        assert hash_author("Silaena") == hash_author("silaena")

    def test_strips_whitespace(self):
        # A leading/trailing space in scraped HTML must not produce a
        # different hash from the canonical form.
        assert hash_author("  Sil  ") == hash_author("Sil")

    def test_nfc_normalization_collapses_diacritic_forms(self):
        # NFC: "café" (precomposed é) vs "café" (e + combining acute)
        composed = "café"
        decomposed = "café"
        assert hash_author(composed) == hash_author(decomposed)

    def test_different_authors_get_different_hashes(self):
        assert hash_author("Sil") != hash_author("Adagio")
        # Confirm the avalanche property holds for a 1-char diff too.
        assert hash_author("Sil") != hash_author("Silv")

    def test_unsalted_reproducibility_across_imports(self):
        # The point of spec §12 OQ #5 ("unsalted is intentional") is that
        # two independent invocations — say, this run and a re-run a month
        # later — produce byte-identical hashes for the same username.
        # If a salt were introduced (env-loaded etc.) this test would
        # break the moment the salt rotated.
        from tea_scrapers.sources.steepster import hash_author as h_again

        assert h_again("Sil") == hash_author("Sil")


# ---------------------------------------------------------------------------
# Vendor-page enumeration
# ---------------------------------------------------------------------------


class TestVendorPageEnumeration:
    def test_collect_tea_urls_from_single_page(
        self, httpx_mock, config: SteepsterConfig, tmp_path: Path
    ):
        base = config.base_url
        httpx_mock.add_response(
            url=f"{base}/companies/yunnan-sourcing",
            text=_company_page_html(
                tea_ids=((41785, "imperial-gold"), (74378, "laoshan-black")),
                has_next=False,
            ),
        )
        # First (and only) tea page — single-page case (no `Next` in
        # tasting-notes section).
        for tid, slug in ((41785, "imperial-gold"), (74378, "laoshan-black")):
            httpx_mock.add_response(
                url=f"{base}/teas/yunnan-sourcing/{tid}-{slug}",
                text=_tea_detail_html(
                    tea_id=str(tid),
                    tea_slug=slug,
                    notes=({"note_id": tid * 10, "author": f"User{tid}"},),
                ),
            )

        tracker = MagicMock()
        with _http_client() as http, JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000001", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000001",
            )
            scraper.run("incremental")

        assert writer.records_written == 2
        assert tracker.record_success.call_count == 2

    def test_pagination_terminates_when_no_next_link(
        self, httpx_mock, config: SteepsterConfig, tmp_path: Path
    ):
        """Page 1 has 'Next' → fetch page 2; page 2 has no 'Next' → stop."""
        base = config.base_url
        httpx_mock.add_response(
            url=f"{base}/companies/yunnan-sourcing",
            text=_company_page_html(
                tea_ids=((1, "a"),), has_next=True, next_page=2
            ),
        )
        httpx_mock.add_response(
            url=f"{base}/companies/yunnan-sourcing?page=2",
            text=_company_page_html(tea_ids=((2, "b"),), has_next=False),
        )
        for tid, slug in ((1, "a"), (2, "b")):
            httpx_mock.add_response(
                url=f"{base}/teas/yunnan-sourcing/{tid}-{slug}",
                text=_tea_detail_html(
                    tea_id=str(tid),
                    tea_slug=slug,
                    notes=({"note_id": tid * 10, "author": "Alice"},),
                ),
            )

        tracker = MagicMock()
        with _http_client() as http, JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000002", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000002",
            )
            scraper.run("incremental")

        # Two teas collected, two records written.
        assert writer.records_written == 2

    def test_empty_company_page_terminates_immediately(
        self, httpx_mock, config: SteepsterConfig, tmp_path: Path
    ):
        """A page with zero tea links must short-circuit (no infinite loop)."""
        base = config.base_url
        httpx_mock.add_response(
            url=f"{base}/companies/yunnan-sourcing",
            text=_company_page_html(tea_ids=(), has_next=True),
        )

        tracker = MagicMock()
        with _http_client() as http, JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000003", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000003",
            )
            scraper.run("incremental")

        assert writer.records_written == 0

    def test_duplicate_tea_links_deduplicated(
        self, httpx_mock, config: SteepsterConfig, tmp_path: Path
    ):
        """Steepster company pages emit each tea href 2–3× (image + title +
        meta); only one detail request per tea must fire."""
        base = config.base_url
        # Same tea three times.
        page_html = _company_page_html(
            tea_ids=((1, "a"), (1, "a"), (1, "a")),
            has_next=False,
        )
        httpx_mock.add_response(
            url=f"{base}/companies/yunnan-sourcing", text=page_html
        )
        httpx_mock.add_response(
            url=f"{base}/teas/yunnan-sourcing/1-a",
            text=_tea_detail_html(
                tea_id="1",
                tea_slug="a",
                notes=({"note_id": 999, "author": "Bob"},),
            ),
        )

        tracker = MagicMock()
        with _http_client() as http, JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000004", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000004",
            )
            scraper.run("incremental")

        assert writer.records_written == 1
        # Only two HTTP calls: 1 company page + 1 tea page.
        assert len(httpx_mock.get_requests()) == 2

    def test_cross_vendor_links_ignored(
        self, httpx_mock, config: SteepsterConfig, tmp_path: Path
    ):
        """The company page may link to teas under other vendor slugs (e.g.
        editorial cross-links). Only links matching the current slug count."""
        base = config.base_url
        # Insert a `/teas/other-vendor/9999-x` link in the markup.
        body = _company_page_html(tea_ids=((1, "a"),), has_next=False).replace(
            "<a href='/teas/yunnan-sourcing/1-a'>Tea 1</a>",
            "<a href='/teas/yunnan-sourcing/1-a'>Tea 1</a>"
            "<a href='/teas/other-vendor/9999-x'>OtherTea</a>",
        )
        httpx_mock.add_response(url=f"{base}/companies/yunnan-sourcing", text=body)
        httpx_mock.add_response(
            url=f"{base}/teas/yunnan-sourcing/1-a",
            text=_tea_detail_html(
                tea_id="1",
                tea_slug="a",
                notes=({"note_id": 10, "author": "Z"},),
            ),
        )

        tracker = MagicMock()
        with _http_client() as http, JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000005", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000005",
            )
            scraper.run("incremental")

        # Only the in-slug tea was fetched — no /teas/other-vendor/... call.
        urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert not any("other-vendor" in u for u in urls)
        assert writer.records_written == 1


# ---------------------------------------------------------------------------
# Tasting-note parsing
# ---------------------------------------------------------------------------


class TestNoteParsing:
    def test_extracts_all_fields_into_jsonl_payload(
        self, httpx_mock, config: SteepsterConfig, tmp_path: Path
    ):
        base = config.base_url
        httpx_mock.add_response(
            url=f"{base}/companies/yunnan-sourcing",
            text=_company_page_html(tea_ids=((42, "x"),), has_next=False),
        )
        httpx_mock.add_response(
            url=f"{base}/teas/yunnan-sourcing/42-x",
            text=_tea_detail_html(
                tea_id="42",
                tea_slug="x",
                notes=(
                    {
                        "note_id": 100,
                        "author": "Silaena",
                        "rating": 88,
                        "body": "Sweet, malty, honey.",
                        "posted_at": "2014-02-16T17:03:08Z",
                        "likes": 24,
                        "comments": 3,
                    },
                ),
            ),
        )

        tracker = MagicMock()
        with _http_client() as http, JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000010", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000010",
            )
            scraper.run("incremental")

        assert writer.path is not None
        line = writer.path.read_text(encoding="utf-8").splitlines()[0]
        record = RawRecord.model_validate_json(line)

        meta = record.ingest_meta
        assert meta.source == SOURCE_KEY
        assert meta.external_id == "42"
        assert meta.endpoint == f"{base}/teas/yunnan-sourcing/42-x"

        payload = record.payload
        assert payload["steepster_id"] == "42"
        assert payload["vendor_slug"] == "yunnan-sourcing"
        assert payload["name"] == "Imperial Gold Needle Yunnan Black Tea"
        assert payload["average_rating"] == 88
        assert payload["rating_count"] == 34
        assert payload["description_pairs"]["Tea type"] == "Black Tea"
        assert payload["description_pairs"]["Flavors"] == "Chocolate, Malt, Honey"
        # Capture-everything (§11): caffeine + empty-certification slot are kept too.
        assert payload["description_pairs"]["Caffeine"] == "High"
        assert payload["prep"]["steep-time"] == "2 min, 45 sec"
        assert "unavailable" in (payload["availability_text"] or "").lower()

        notes = payload["tasting_notes"]
        assert len(notes) == 1
        note = notes[0]
        assert note["steepster_note_id"] == "100"
        assert note["author_hash"] == hash_author("Silaena")
        assert note["author_hash"].startswith("sha256:")
        assert note["rating"] == 88
        assert "Sweet, malty, honey." in (note["body"] or "")
        assert note["posted_at"] == "2014-02-16T17:03:08Z"
        assert note["like_count"] == 24
        assert note["comment_count"] == 3
        # Author *name* is NOT in the payload — only the hash.
        assert "Silaena" not in line

    def test_note_pagination_followed(
        self, httpx_mock, config: SteepsterConfig, tmp_path: Path
    ):
        base = config.base_url
        httpx_mock.add_response(
            url=f"{base}/companies/yunnan-sourcing",
            text=_company_page_html(tea_ids=((42, "x"),), has_next=False),
        )
        # Page 1 has 'Next' → fetch ?page=2.
        httpx_mock.add_response(
            url=f"{base}/teas/yunnan-sourcing/42-x",
            text=_tea_detail_html(
                tea_id="42",
                tea_slug="x",
                notes=({"note_id": 1, "author": "A"},),
                has_next=True,
            ),
        )
        httpx_mock.add_response(
            url=f"{base}/teas/yunnan-sourcing/42-x?page=2",
            text=_tea_detail_html(
                tea_id="42",
                tea_slug="x",
                notes=({"note_id": 2, "author": "B"},),
                has_next=False,
            ),
        )

        tracker = MagicMock()
        with _http_client() as http, JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000020", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000020",
            )
            scraper.run("incremental")

        line = writer.path.read_text(encoding="utf-8").splitlines()[0]
        record = RawRecord.model_validate_json(line)
        notes = record.payload["tasting_notes"]
        assert [n["steepster_note_id"] for n in notes] == ["1", "2"]
        # Distinct authors → distinct hashes.
        assert notes[0]["author_hash"] != notes[1]["author_hash"]

    def test_note_with_missing_author_skipped_with_warning(
        self,
        httpx_mock,
        config: SteepsterConfig,
        tmp_path: Path,
        captured_logs,
    ):
        base = config.base_url
        # Markup the parser will hit but with no author at all.
        no_author = dedent(
            """
            <div id='note_999' class='note'>
              <div itemprop='review'>
                <div class='content'>
                  <span itemprop='description'><p>Orphan note.</p></span>
                </div>
              </div>
            </div>
            """
        )
        detail = _tea_detail_html(
            tea_id="42",
            tea_slug="x",
            notes=({"note_id": 100, "author": "Alice"},),
        ).replace("</section>", no_author + "</section>", 1)
        httpx_mock.add_response(
            url=f"{base}/companies/yunnan-sourcing",
            text=_company_page_html(tea_ids=((42, "x"),), has_next=False),
        )
        httpx_mock.add_response(
            url=f"{base}/teas/yunnan-sourcing/42-x", text=detail
        )

        tracker = MagicMock()
        with _http_client() as http, JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000030", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000030",
            )
            scraper.run("incremental")

        line = writer.path.read_text(encoding="utf-8").splitlines()[0]
        record = RawRecord.model_validate_json(line)
        # Only the well-formed note survived.
        note_ids = [n["steepster_note_id"] for n in record.payload["tasting_notes"]]
        assert note_ids == ["100"]
        # And we logged the skip.
        warnings = [
            e for e in captured_logs.entries
            if e["event"] == "steepster.note.missing_author"
        ]
        assert len(warnings) == 1
        assert warnings[0]["note_id"] == "999"


# ---------------------------------------------------------------------------
# Tracker integration / error paths
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_terminal_error_propagates_and_records_error(
        self, httpx_mock, config: SteepsterConfig, tmp_path: Path
    ):
        base = config.base_url
        # HttpClient retries 3 times by default → need 4 failing responses.
        for _ in range(4):
            httpx_mock.add_response(
                url=f"{base}/companies/yunnan-sourcing", status_code=500
            )

        tracker = MagicMock()
        with _http_client() as http, JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000040", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000040",
            )
            with pytest.raises(ScrapeError):
                scraper.run("incremental")

        assert tracker.record_error.call_count == 1
        summary = (
            tracker.record_error.call_args.kwargs.get("summary")
            or tracker.record_error.call_args.args[0]
        )
        assert "yunnan-sourcing" in summary

    def test_rate_limit_set_per_host_before_first_request(
        self, config: SteepsterConfig, tmp_path: Path
    ):
        fake_http = MagicMock(spec=HttpClient)
        # Empty company page → no detail fetches.
        fake_response = MagicMock()
        fake_response.text = _company_page_html(tea_ids=(), has_next=False)
        fake_http.get.return_value = fake_response

        tracker = MagicMock()
        with JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000050", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=fake_http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000050",
            )
            scraper.run("incremental")

        fake_http.set_host_rps.assert_called_once_with(
            "steepster.test", 100.0
        )
        # set_host_rps must precede the first get call.
        call_order = [c[0] for c in fake_http.method_calls]
        assert call_order.index("set_host_rps") < call_order.index("get")

    def test_no_vendor_slugs_short_circuits(
        self, config: SteepsterConfig, tmp_path: Path, captured_logs
    ):
        fake_http = MagicMock(spec=HttpClient)
        tracker = MagicMock()
        with JsonlWriter(
            run_id="01HXSTEEPSTERTEST00000060", base_dir=tmp_path
        ) as writer:
            scraper = SteepsterScraper(
                config=config,
                http_client=fake_http,
                writer=writer,
                tracker=tracker,
                run_id="01HXSTEEPSTERTEST00000060",
                vendor_slugs=[],  # explicit empty override
            )
            scraper.run("incremental")

        # No detail fetches.
        fake_http.get.assert_not_called()
        # Warning logged.
        warnings = [
            e for e in captured_logs.entries
            if e["event"] == "steepster.no_vendor_slugs"
        ]
        assert len(warnings) == 1
