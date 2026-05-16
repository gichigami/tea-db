from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import structlog

from tea_scrapers.config import VendorConfig
from tea_scrapers.http.client import HttpClient, ScrapeError
from tea_scrapers.http.ratelimit import HostRateLimiter
from tea_scrapers.schemas.ingest import RawRecord
from tea_scrapers.sources.shopify import ShopifyScraper
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
def vendor() -> VendorConfig:
    return VendorConfig(
        source_key="yunnan_sourcing_us",
        display_name="Yunnan Sourcing USA",
        base_url="https://yunnansourcing.test",
        rate_limit_rps=2.0,
    )


def _http_client() -> HttpClient:
    """Build an HttpClient with no real sleeping and a real httpx mountable client."""
    return HttpClient(
        rate_limiter=HostRateLimiter(default_rps=100.0),
        sleep=lambda _s: None,
        client=httpx.Client(),
    )


def _product(pid: int, **overrides) -> dict:
    p = {
        "id": pid,
        "title": f"Tea {pid}",
        "handle": f"tea-{pid}",
        "body_html": "<p>Description</p>",
        "published_at": None,
        "vendor": "Test Vendor",
        "product_type": "Pu-erh",
        "tags": ["pu-erh"],
        "variants": [
            {
                "id": pid * 10,
                "title": "100g",
                "price": "32.50",
                "available": False,
                "sku": f"SKU-{pid}",
                "grams": 100,
            }
        ],
        "images": [],
    }
    p.update(overrides)
    return p


def _page(products: list[dict]) -> dict:
    return {"products": products}


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pagination_terminates_on_empty_products(
    httpx_mock, vendor: VendorConfig, tmp_path: Path
):
    base = vendor.base_url
    httpx_mock.add_response(
        url=f"{base}/products.json?limit=250&page=1",
        json=_page([_product(1), _product(2)]),
    )
    httpx_mock.add_response(
        url=f"{base}/products.json?limit=250&page=2",
        json=_page([_product(3)]),
    )
    httpx_mock.add_response(
        url=f"{base}/products.json?limit=250&page=3",
        json=_page([]),
    )

    tracker = MagicMock()
    with _http_client() as http, JsonlWriter(run_id="01HXTESTPAGINATE0000000000", base_dir=tmp_path) as writer:
        scraper = ShopifyScraper(
            vendor=vendor,
            http_client=http,
            writer=writer,
            tracker=tracker,
            run_id="01HXTESTPAGINATE0000000000",
        )
        scraper.run("incremental")

    assert writer.records_written == 3
    assert writer.path is not None
    lines = writer.path.read_text(encoding="utf-8").splitlines()
    parsed = [RawRecord.model_validate_json(line) for line in lines]
    assert [r.ingest_meta.record_index for r in parsed] == [0, 1, 2]
    assert [r.ingest_meta.external_id for r in parsed] == ["1", "2", "3"]
    assert tracker.record_success.call_count == 3


def test_max_pages_hard_cap_emits_warning(
    httpx_mock, vendor: VendorConfig, tmp_path: Path, captured_logs
):
    base = vendor.base_url
    # Only pages 1 and 2 should be requested; the third response is intentionally
    # not registered so pytest-httpx's strictness asserts page 3 was never fetched.
    for page in (1, 2):
        httpx_mock.add_response(
            url=f"{base}/products.json?limit=250&page={page}",
            json=_page([_product(page * 100)]),
        )

    tracker = MagicMock()
    with _http_client() as http, JsonlWriter(run_id="01HXTESTHARDCAP00000000000", base_dir=tmp_path) as writer:
        scraper = ShopifyScraper(
            vendor=vendor,
            http_client=http,
            writer=writer,
            tracker=tracker,
            run_id="01HXTESTHARDCAP00000000000",
            max_pages=2,
        )
        scraper.run("full")

    # Only the first two pages were consumed.
    requested_urls = [str(req.url) for req in httpx_mock.get_requests()]
    assert f"{base}/products.json?limit=250&page=3" not in requested_urls
    assert writer.records_written == 2

    hardcap_events = [e for e in captured_logs.entries if e["event"] == "scrape.page.hardcap"]
    assert len(hardcap_events) == 1
    assert hardcap_events[0]["page"] == 2


# ---------------------------------------------------------------------------
# Record contents
# ---------------------------------------------------------------------------


def test_ingest_meta_populated_correctly(
    httpx_mock, vendor: VendorConfig, tmp_path: Path
):
    base = vendor.base_url
    httpx_mock.add_response(
        url=f"{base}/products.json?limit=250&page=1",
        json=_page([_product(7384921093)]),
    )
    httpx_mock.add_response(
        url=f"{base}/products.json?limit=250&page=2",
        json=_page([]),
    )

    tracker = MagicMock()
    run_id = "01HXTESTMETA000000000000AA"
    with _http_client() as http, JsonlWriter(run_id=run_id, base_dir=tmp_path) as writer:
        scraper = ShopifyScraper(
            vendor=vendor,
            http_client=http,
            writer=writer,
            tracker=tracker,
            run_id=run_id,
        )
        scraper.run("incremental")

    assert writer.path is not None
    parsed = RawRecord.model_validate_json(writer.path.read_text(encoding="utf-8").splitlines()[0])
    meta = parsed.ingest_meta
    assert meta.source == "yunnan_sourcing_us"
    assert meta.run_id == run_id
    assert meta.external_id == "7384921093"
    assert meta.endpoint == f"{base}/products.json?limit=250&page=1"
    assert meta.scraped_at.tzinfo is not None
    assert meta.scraped_at.utcoffset() is not None
    assert meta.scraped_at.utcoffset().total_seconds() == 0


def test_payload_passed_through_unmodified(
    httpx_mock, vendor: VendorConfig, tmp_path: Path
):
    base = vendor.base_url
    payload = _product(
        42,
        body_html="<h1>Rich</h1><p>With <em>HTML</em></p>",
        published_at=None,
    )
    # Force a variant with available=false to make sure we don't filter it out.
    payload["variants"][0]["available"] = False
    httpx_mock.add_response(
        url=f"{base}/products.json?limit=250&page=1",
        json=_page([payload]),
    )
    httpx_mock.add_response(
        url=f"{base}/products.json?limit=250&page=2",
        json=_page([]),
    )

    tracker = MagicMock()
    with _http_client() as http, JsonlWriter(run_id="01HXTESTPAYLOAD0000000000A", base_dir=tmp_path) as writer:
        scraper = ShopifyScraper(
            vendor=vendor,
            http_client=http,
            writer=writer,
            tracker=tracker,
            run_id="01HXTESTPAYLOAD0000000000A",
        )
        scraper.run("incremental")

    assert writer.path is not None
    line = writer.path.read_text(encoding="utf-8").splitlines()[0]
    parsed = RawRecord.model_validate_json(line)
    assert parsed.payload == payload
    # body_html survives.
    assert parsed.payload["body_html"] == "<h1>Rich</h1><p>With <em>HTML</em></p>"
    # published_at=null survives.
    assert parsed.payload["published_at"] is None
    # available=false survives.
    assert parsed.payload["variants"][0]["available"] is False


# ---------------------------------------------------------------------------
# Tracker integration
# ---------------------------------------------------------------------------


def test_scrape_error_propagates_and_records_error(
    httpx_mock, vendor: VendorConfig, tmp_path: Path
):
    base = vendor.base_url
    # HttpClient retries 3 times by default → need 4 failing responses.
    for _ in range(4):
        httpx_mock.add_response(
            url=f"{base}/products.json?limit=250&page=1",
            status_code=500,
        )

    tracker = MagicMock()
    with _http_client() as http, JsonlWriter(run_id="01HXTESTSCRAPEERR0000000A", base_dir=tmp_path) as writer:
        scraper = ShopifyScraper(
            vendor=vendor,
            http_client=http,
            writer=writer,
            tracker=tracker,
            run_id="01HXTESTSCRAPEERR0000000A",
        )
        with pytest.raises(ScrapeError):
            scraper.run("incremental")

    assert tracker.record_error.call_count == 1
    summary = tracker.record_error.call_args.kwargs.get("summary") or tracker.record_error.call_args.args[0]
    assert "500" in summary


# ---------------------------------------------------------------------------
# Rate limit + page log
# ---------------------------------------------------------------------------


def test_rate_limit_configured_per_host_before_first_request(
    vendor: VendorConfig, tmp_path: Path
):
    # Use a MagicMock HttpClient so we can introspect the call order without
    # touching the network — httpx_mock isn't needed here.
    fake_http = MagicMock(spec=HttpClient)
    # get_json must return the empty page so the scraper terminates after one call.
    fake_http.get_json.return_value = _page([])

    tracker = MagicMock()
    with JsonlWriter(run_id="01HXTESTRPS00000000000000A", base_dir=tmp_path) as writer:
        scraper = ShopifyScraper(
            vendor=vendor,
            http_client=fake_http,
            writer=writer,
            tracker=tracker,
            run_id="01HXTESTRPS00000000000000A",
        )
        scraper.run("incremental")

    fake_http.set_host_rps.assert_called_once_with("yunnansourcing.test", 2.0)
    # set_host_rps must precede the first get_json call.
    call_order = [c[0] for c in fake_http.method_calls]
    assert call_order.index("set_host_rps") < call_order.index("get_json")


def test_scrape_page_event_emitted_with_url_and_count(
    httpx_mock, vendor: VendorConfig, tmp_path: Path, captured_logs
):
    base = vendor.base_url
    httpx_mock.add_response(
        url=f"{base}/products.json?limit=250&page=1",
        json=_page([_product(1), _product(2)]),
    )
    httpx_mock.add_response(
        url=f"{base}/products.json?limit=250&page=2",
        json=_page([]),
    )

    tracker = MagicMock()
    with _http_client() as http, JsonlWriter(run_id="01HXTESTPAGELOG00000000000", base_dir=tmp_path) as writer:
        scraper = ShopifyScraper(
            vendor=vendor,
            http_client=http,
            writer=writer,
            tracker=tracker,
            run_id="01HXTESTPAGELOG00000000000",
        )
        scraper.run("incremental")

    page_events = [e for e in captured_logs.entries if e["event"] == "scrape.page"]
    # One per non-empty page + one for the empty terminator.
    assert len(page_events) == 2
    counts = {e["page"]: e["count"] for e in page_events}
    assert counts == {1: 2, 2: 0}
    urls = {e["url"] for e in page_events}
    assert urls == {
        f"{base}/products.json?limit=250&page=1",
        f"{base}/products.json?limit=250&page=2",
    }
