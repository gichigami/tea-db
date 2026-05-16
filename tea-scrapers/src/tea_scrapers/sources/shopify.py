"""Generic Shopify `/products.json` scraper.

Spec reference: specs/tea_scrapers_v1_spec.md §6.1.

One scraper, many vendors — vendor-specific knobs come from `config/vendors.yaml`
(see :class:`tea_scrapers.config.VendorConfig`). The payload is captured
verbatim per §11 ("Don't mutate the payload"): no `body_html` stripping,
no `available` filtering, no dropping of unpublished products.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

import structlog

from tea_scrapers.config import VendorConfig
from tea_scrapers.http.client import HttpClient, ScrapeError
from tea_scrapers.schemas.ingest import IngestMeta, RawRecord
from tea_scrapers.storage.raw import JsonlWriter
from tea_scrapers.storage.run_tracker import RunTracker


class ShopifyScraper:
    """Paginates `{base_url}/products.json?limit=250&page=N` until the array is empty."""

    PAGE_LIMIT = 250

    def __init__(
        self,
        vendor: VendorConfig,
        http_client: HttpClient,
        writer: JsonlWriter,
        tracker: RunTracker,
        run_id: str,
        *,
        max_pages: int = 250,
        log: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._vendor = vendor
        self._http = http_client
        self._writer = writer
        self._tracker = tracker
        self._run_id = run_id
        self._max_pages = max_pages
        self._log = log or structlog.get_logger(__name__)

    def run(self, mode: str = "incremental") -> None:
        """Walk pages until empty, writing one JSONL record per product.

        `mode` is currently informational — incremental dedup happens in the
        bronze loader via `payload_hash` (spec §8), not at scrape time.
        """
        host = urlparse(self._vendor.base_url).netloc
        self._http.set_host_rps(host, self._vendor.rate_limit_rps)

        record_index = 0
        page = 1
        while page <= self._max_pages:
            url = self._page_url(page)
            try:
                data = self._http.get_json(url)
            except ScrapeError as exc:
                self._tracker.record_error(summary=str(exc))
                raise

            products = data.get("products") if isinstance(data, dict) else None
            if not products:
                self._log.info(
                    "scrape.page",
                    page=page,
                    count=0,
                    url=url,
                )
                return

            for product in products:
                record = RawRecord(
                    ingest_meta=IngestMeta(
                        source=self._vendor.source_key,
                        scraped_at=datetime.now(timezone.utc),
                        run_id=self._run_id,
                        endpoint=url,
                        record_index=record_index,
                        external_id=str(product["id"]),
                    ),
                    payload=product,
                )
                self._writer.write(record)
                self._tracker.record_success(1)
                record_index += 1

            self._log.info(
                "scrape.page",
                page=page,
                count=len(products),
                url=url,
            )
            page += 1

        self._log.warning(
            "scrape.page.hardcap",
            page=self._max_pages,
            source=self._vendor.source_key,
        )

    def _page_url(self, page: int) -> str:
        return f"{self._vendor.base_url}/products.json?limit={self.PAGE_LIMIT}&page={page}"


__all__ = ["ShopifyScraper"]
