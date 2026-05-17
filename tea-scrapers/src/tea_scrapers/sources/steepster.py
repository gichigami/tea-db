"""Steepster community-review scraper.

Spec reference: specs/tea_scrapers_v1_spec.md §6.2.

HTML scraping — no public API. The scraper:

1. For each configured vendor slug (`config/vendors.yaml::steepster.vendor_slugs`),
   paginates the company index ``/companies/{slug}?page=N`` collecting tea-detail
   URLs of the form ``/teas/{slug}/{tea_id}-{tea_slug}`` until a page has no
   tea links or no "Next" pagination link.
2. For each collected tea URL, fetches the detail page and paginates the
   inline tasting-note section (``?page=N#tasting-notes``) until a page has
   no ``id='note_*'`` divs.
3. Writes **one JSONL record per tea** with all tasting notes inlined.

Anti-pattern guardrails honored (§11):
- The scraper captures every field it sees in the HTML (rating, body, author
  hash, posted_at, note-id, like-count, comment-count, tea metadata).
- The payload is built once and written verbatim — no downstream filtering
  at scrape time.
- Author *names* are never written to JSONL: they're SHA-256 hashed at
  parse time (§6.2 "Hash author names rather than capturing them").
- 1 req per 10 sec honors steepster's robots.txt `Crawl-Delay: 10` (§6.2).
- HTTP timeout >= 60s accommodates the slow first-hit render times
  observed on tea-detail pages (§6.2).

Open items the scraper is *not* responsible for (filed in spec §12):
- Bronze schema choice (`raw_product_snapshot` vs new `raw_review_snapshot`)
  is data-engineer territory — this scraper produces JSONL of the §6.2
  shape; whichever bronze table the loader writes into is downstream.
- The Steepster→silver `product` join is gated on V.4.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import structlog
from selectolax.parser import HTMLParser, Node

from tea_scrapers.config import SteepsterConfig
from tea_scrapers.http.client import HttpClient, ScrapeError
from tea_scrapers.schemas.ingest import IngestMeta, RawRecord
from tea_scrapers.storage.raw import JsonlWriter
from tea_scrapers.storage.run_tracker import RunTracker

SOURCE_KEY = "steepster"
_AUTHOR_HASH_PREFIX = "sha256:"
# `/teas/{vendor-slug}/{tea_id}-{tea-slug}` — vendor-slug and tea-slug allow
# hyphenated lowercase ASCII; tea_id is numeric. Confirmed against
# https://steepster.com/companies/yunnan-sourcing as of 2026-05-16.
_TEA_URL_RE = re.compile(
    r"^/teas/(?P<vendor_slug>[a-z0-9-]+)/(?P<tea_id>\d+)-(?P<tea_slug>[a-z0-9-]+)/?$"
)
_NOTE_ID_RE = re.compile(r"^note_(\d+)$")
_PAGE_QUERY_RE = re.compile(r"[?&]page=(\d+)\b")


def hash_author(name: str) -> str:
    """Stable, unsalted SHA-256 of a Steepster author name.

    Input is NFC-normalized and lowercased before hashing so case-only and
    diacritic-form-only variants collapse to the same hash. Unsalted is
    intentional (spec §12 step-7 OQ #5): cross-run reproducibility supports
    "returning-reviewer" dedup in the downstream silver layer — a salt
    would make every run's hashes incomparable to the previous run's.

    The output is prefixed ``sha256:`` per §6.2's example record, which
    lets future schema migrations distinguish unsalted SHA-256 from any
    future salted scheme without touching every historical row.
    """
    normalized = unicodedata.normalize("NFC", name).strip().lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{_AUTHOR_HASH_PREFIX}{digest}"


class SteepsterScraper:
    """Crawl steepster.com vendor + tea + tasting-note pages.

    See module docstring for the strategy. Driven by :class:`SteepsterConfig`
    loaded from `config/vendors.yaml`. Honors a per-host rate limit of
    ``cfg.rate_limit_rps`` (default 0.1 — one request per 10 seconds).
    """

    # Hard cap on company-index pages walked per vendor slug. At 100 teas/page
    # this is 25,000 teas/vendor — well past any V1 allowlisted vendor's
    # actual catalog (Yunnan Sourcing is ~3,700 teas across 367 pages).
    MAX_COMPANY_PAGES = 500
    # Hard cap on tasting-note pages walked per tea. Notes paginate at 10 per
    # page; the largest tea (~9,000 reviews) would top out around 900 pages,
    # but in practice we hit pagination terminators long before this.
    MAX_NOTE_PAGES = 1000

    def __init__(
        self,
        config: SteepsterConfig,
        http_client: HttpClient,
        writer: JsonlWriter,
        tracker: RunTracker,
        run_id: str,
        *,
        vendor_slugs: list[str] | None = None,
        max_teas_per_vendor: int | None = None,
        log: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._cfg = config
        self._http = http_client
        self._writer = writer
        self._tracker = tracker
        self._run_id = run_id
        # CLI may pass an explicit slug list to scope a run to one vendor.
        self._vendor_slugs = vendor_slugs if vendor_slugs is not None else list(config.vendor_slugs)
        # Optional cap used by integration tests with trimmed cassettes; in
        # production this is None (walk every collected tea URL).
        self._max_teas_per_vendor = max_teas_per_vendor
        self._log = log or structlog.get_logger(__name__)
        self._record_index = 0

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, mode: str = "incremental") -> None:
        """Walk each configured vendor slug and emit one JSONL record per tea.

        `mode` is informational only — incremental dedup happens in the
        bronze loader via ``payload_hash`` (spec §4 / §8), not at scrape time.
        """
        host = urlparse(self._cfg.base_url).netloc
        self._http.set_host_rps(host, self._cfg.rate_limit_rps)

        if not self._vendor_slugs:
            self._log.warning("steepster.no_vendor_slugs")
            return

        for slug in self._vendor_slugs:
            try:
                self._run_vendor(slug)
            except ScrapeError as exc:
                # Per spec §4: a per-record/per-tea failure does NOT abort the
                # run, but a terminal HTTP failure (5xx storm, 401/403) does.
                # `ScrapeError` is the terminal class — re-raise so the
                # tracker finalizes as 'failed' and the CLI exits 2.
                self._tracker.record_error(summary=f"vendor={slug}: {exc}")
                raise

    # ------------------------------------------------------------------
    # Vendor crawl (company-index pagination)
    # ------------------------------------------------------------------

    def _run_vendor(self, vendor_slug: str) -> None:
        tea_urls = self._collect_tea_urls(vendor_slug)
        self._log.info(
            "steepster.vendor.urls_collected",
            vendor_slug=vendor_slug,
            count=len(tea_urls),
        )

        if self._max_teas_per_vendor is not None:
            tea_urls = tea_urls[: self._max_teas_per_vendor]

        for tea_url in tea_urls:
            try:
                self._scrape_one_tea(tea_url, vendor_slug)
            except ScrapeError:
                # Terminal — propagate so the vendor loop aborts as well.
                raise
            except ValueError as exc:
                # Per-tea parse failure: log + continue (spec §4 "single
                # failed record does NOT abort a run"). Counted as an error
                # in the tracker so it surfaces in the scrape_run summary.
                self._tracker.record_error(summary=f"tea_parse: {tea_url}: {exc}")
                self._log.warning(
                    "steepster.tea.parse_error",
                    url=tea_url,
                    vendor_slug=vendor_slug,
                    error=str(exc),
                )

    def _collect_tea_urls(self, vendor_slug: str) -> list[str]:
        """Paginate ``/companies/{slug}?page=N`` and collect tea-detail URLs.

        Terminator: a page with no matching `/teas/{slug}/{tea_id}-{slug}`
        hrefs OR no "Next" pagination link. Both signals are checked because
        steepster's last numbered page typically still has a "Next" link
        that 404s on click — so the absence of teas is the load-bearing
        signal.

        URLs are returned de-duplicated in first-seen order; the company
        page emits each tea href 2–3 times (image + title + meta).
        """
        seen: dict[str, None] = {}  # ordered set via dict
        page = 1
        while page <= self.MAX_COMPANY_PAGES:
            url = self._company_page_url(vendor_slug, page)
            response = self._http.get(url)
            html = response.text
            tree = HTMLParser(html)

            page_urls = list(self._extract_tea_urls(tree, vendor_slug))
            for u in page_urls:
                seen.setdefault(u, None)

            has_next = self._has_next_pagination_link(tree)

            self._log.info(
                "steepster.company.page",
                vendor_slug=vendor_slug,
                page=page,
                tea_count=len(page_urls),
                has_next=has_next,
                url=url,
            )

            if not page_urls:
                # Empty page = past the end. Steepster's index sometimes
                # serves an empty list rather than 404 on overshoot.
                return list(seen.keys())
            if not has_next:
                return list(seen.keys())
            page += 1

        self._log.warning(
            "steepster.company.hardcap",
            vendor_slug=vendor_slug,
            page=self.MAX_COMPANY_PAGES,
        )
        return list(seen.keys())

    def _company_page_url(self, vendor_slug: str, page: int) -> str:
        if page == 1:
            return f"{self._cfg.base_url}/companies/{vendor_slug}"
        return f"{self._cfg.base_url}/companies/{vendor_slug}?page={page}"

    def _extract_tea_urls(self, tree: HTMLParser, vendor_slug: str) -> list[str]:
        """Find all `/teas/{vendor_slug}/{tea_id}-{tea_slug}` hrefs on a company page."""
        urls: list[str] = []
        seen_local: set[str] = set()
        for anchor in tree.css("a[href]"):
            href = anchor.attributes.get("href") or ""
            # Normalize to absolute path for URL-shape match; we only walk
            # links that belong to *this* vendor slug per spec §6.2 (the
            # company page sometimes links cross-vendor in editorial copy).
            path = urlparse(href).path or href
            m = _TEA_URL_RE.match(path)
            if m is None or m.group("vendor_slug") != vendor_slug:
                continue
            absolute = urljoin(self._cfg.base_url, path)
            if absolute in seen_local:
                continue
            seen_local.add(absolute)
            urls.append(absolute)
        return urls

    @staticmethod
    def _has_next_pagination_link(tree: HTMLParser) -> bool:
        """Detect a 'Next' link in the pagination block.

        Steepster's pagination block renders ``<li><a href='?page=N'>Next</a></li>``
        on every page except the last (verified 2026-05-16 against
        ``/companies/yunnan-sourcing``). We require both the literal "Next"
        text and an ``?page=`` href so unrelated anchors labeled "Next" in
        the chrome can't fool the terminator.
        """
        for anchor in tree.css("a"):
            text = (anchor.text() or "").strip()
            if text != "Next":
                continue
            href = anchor.attributes.get("href") or ""
            if _PAGE_QUERY_RE.search(href):
                return True
        return False

    # ------------------------------------------------------------------
    # Tea detail (one JSONL record per tea, notes inlined)
    # ------------------------------------------------------------------

    def _scrape_one_tea(self, tea_url: str, vendor_slug: str) -> None:
        first_response = self._http.get(tea_url)
        first_html = first_response.text
        first_tree = HTMLParser(first_html)

        metadata = self._parse_tea_metadata(first_tree, tea_url, vendor_slug)

        all_notes: list[dict[str, Any]] = []
        all_notes.extend(self._parse_tasting_notes(first_tree))

        # Walk subsequent pages until a page has no `id='note_*'` divs or no
        # "Next" pagination link. Tracking the most recent tree separately
        # from `first_tree` keeps the terminator check cleanly scoped.
        last_tree: HTMLParser = first_tree
        page = 2
        while page <= self.MAX_NOTE_PAGES:
            if not self._has_next_notes_page(last_tree):
                break
            page_url = self._tea_page_url(tea_url, page)
            response = self._http.get(page_url)
            last_tree = HTMLParser(response.text)
            page_notes = self._parse_tasting_notes(last_tree)
            self._log.info(
                "steepster.tea.notes_page",
                url=page_url,
                page=page,
                note_count=len(page_notes),
            )
            if not page_notes:
                break
            all_notes.extend(page_notes)
            page += 1
        else:
            self._log.warning(
                "steepster.tea.notes_hardcap",
                url=tea_url,
                page=self.MAX_NOTE_PAGES,
            )

        payload: dict[str, Any] = dict(metadata)
        payload["tasting_notes"] = all_notes

        record = RawRecord(
            ingest_meta=IngestMeta(
                source=SOURCE_KEY,
                scraped_at=datetime.now(timezone.utc),
                run_id=self._run_id,
                endpoint=tea_url,
                record_index=self._record_index,
                external_id=metadata["steepster_id"],
            ),
            payload=payload,
        )
        self._writer.write(record)
        self._tracker.record_success(1)
        self._record_index += 1

        self._log.info(
            "steepster.tea.scraped",
            url=tea_url,
            steepster_id=metadata["steepster_id"],
            note_count=len(all_notes),
        )

    @staticmethod
    def _tea_page_url(tea_url: str, page: int) -> str:
        sep = "&" if "?" in tea_url else "?"
        return f"{tea_url}{sep}page={page}"

    @staticmethod
    def _has_next_notes_page(tree: HTMLParser) -> bool:
        # Same logic as `_has_next_pagination_link` (the company-index and
        # tea-detail pages share the pagination partial). Kept as a separate
        # method to make the two call sites readable at a glance.
        for anchor in tree.css("a"):
            text = (anchor.text() or "").strip()
            if text != "Next":
                continue
            href = anchor.attributes.get("href") or ""
            if _PAGE_QUERY_RE.search(href):
                return True
        return False

    # ------------------------------------------------------------------
    # Parsers (HTML → dicts) — exposed for unit testing.
    # ------------------------------------------------------------------

    def _parse_tea_metadata(
        self, tree: HTMLParser, tea_url: str, vendor_slug: str
    ) -> dict[str, Any]:
        """Extract tea-level metadata from the detail page.

        Capture everything visible in the rendered HTML (spec §11): name,
        type, ingredients, flavor descriptors, sold-in, caffeine, average
        rating, rating count, prep details, availability. Downstream
        normalization decides which fields matter.
        """
        # tea_id from URL is the load-bearing identifier — fall back from
        # body markup if the URL parse loses precision.
        m = _TEA_URL_RE.match(urlparse(tea_url).path or "")
        if m is None:
            raise ValueError(f"unparseable tea URL: {tea_url}")
        steepster_id = m.group("tea_id")

        name = _first_text(tree, "h1[itemprop='name']")

        rating_average = _first_text(tree, "#rating-average")
        review_count = _first_attr(tree, "meta[itemprop='reviewCount']", "content")

        description_block = _first_text(tree, ".description") or _first_text(
            tree, "#tea-description"
        )

        # `<dl class='tea-description'>` carries the structured key/value
        # pairs (tea type, ingredients, flavors, sold-in, caffeine,
        # certification). Walk it pair-wise.
        description_pairs = _parse_description_dl(tree)

        prep = _parse_prep_block(tree)
        availability = _first_text(tree, "#availability h4")

        return {
            "steepster_id": steepster_id,
            "url": tea_url,
            "vendor_slug": vendor_slug,
            "name": name,
            "average_rating": _try_int(rating_average),
            "rating_count": _try_int(review_count),
            "description": description_block,
            "description_pairs": description_pairs,
            "prep": prep,
            "availability_text": availability,
        }

    def _parse_tasting_notes(self, tree: HTMLParser) -> list[dict[str, Any]]:
        """Extract every `<div id='note_*'>` block on a page.

        Returns a list of dicts with at minimum the §6.2 schema fields
        plus everything else visible in the markup (note_id, like_count,
        comment_count) so downstream consumers don't have to re-scrape.
        """
        notes: list[dict[str, Any]] = []
        for node in tree.css("div.note"):
            note_id = node.attributes.get("id") or ""
            id_match = _NOTE_ID_RE.match(note_id)
            if id_match is None:
                # A `div.note` with a non-conformant id is a markup change
                # we want to see in logs but not abort on.
                self._log.warning(
                    "steepster.note.unparseable_id",
                    raw_id=note_id,
                )
                continue
            steepster_note_id = id_match.group(1)

            author_name = _first_attr(
                node, "meta[itemprop='name']", "content"
            ) or _first_text(node, "[itemprop='author'] [itemprop='name']")

            if not author_name:
                # No author → can't hash. Skip-with-warning rather than
                # writing a record with empty author_hash that could
                # collide with other empty-author records downstream.
                self._log.warning(
                    "steepster.note.missing_author",
                    note_id=steepster_note_id,
                )
                continue

            rating_text = _first_text(node, "[itemprop='ratingValue']")
            body_text = _first_text(node, "[itemprop='description']")
            posted_at_iso = _first_attr(
                node, "abbr[itemprop='datePublished']", "content"
            )
            like_count = _try_int(_first_attr(node, ".likes-count", "data-pluralize-count"))
            comment_count = _try_int(
                _first_attr(node, ".show-inline-comments", "data-pluralize-count")
            )

            notes.append(
                {
                    "steepster_note_id": steepster_note_id,
                    "author_hash": hash_author(author_name),
                    "rating": _try_int(rating_text),
                    "body": body_text,
                    "posted_at": posted_at_iso,
                    "like_count": like_count,
                    "comment_count": comment_count,
                }
            )
        return notes


# ---------------------------------------------------------------------------
# Small HTML helpers — kept module-private; tests use them via the public
# scraper API.
# ---------------------------------------------------------------------------


def _first_text(node: HTMLParser | Node, selector: str) -> str | None:
    match = node.css_first(selector)
    if match is None:
        return None
    text = match.text(strip=True)
    return text or None


def _first_attr(node: HTMLParser | Node, selector: str, attr: str) -> str | None:
    match = node.css_first(selector)
    if match is None:
        return None
    value = match.attributes.get(attr)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _try_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_description_dl(tree: HTMLParser) -> dict[str, str]:
    """Walk `<dl class='tea-description'>` into a dt→dd mapping.

    Preserves the displayed casing from the page so downstream normalize
    can decide what to canonicalize (spec §11 anti-pattern: "Don't filter
    at scrape time").
    """
    dl = tree.css_first("dl.tea-description")
    if dl is None:
        return {}
    pairs: dict[str, str] = {}
    current_key: str | None = None
    for child in dl.iter():
        tag = (child.tag or "").lower()
        if tag == "dt":
            key = child.text(strip=True)
            current_key = key or None
        elif tag == "dd" and current_key:
            value = child.text(strip=True)
            if value and current_key not in pairs:
                pairs[current_key] = value
    return pairs


def _parse_prep_block(tree: HTMLParser) -> dict[str, str]:
    """Capture the optional `.tea-prep .prep-details` span values."""
    prep = tree.css_first(".tea-prep .prep-details")
    if prep is None:
        return {}
    out: dict[str, str] = {}
    for span in prep.css("span"):
        cls = (span.attributes.get("class") or "").strip()
        if not cls:
            continue
        text = span.text(strip=True)
        if text:
            out[cls] = text
    return out


__all__ = ["SteepsterScraper", "SOURCE_KEY", "hash_author"]
