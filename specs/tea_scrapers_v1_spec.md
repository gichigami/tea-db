# Tea Scrapers V1: Specification & Architecture

**Status:** Spec for agent implementation
**Audience:** Implementation agents (e.g. Claude Code) building V1 scrapers
**Parent doc:** `tea_rec_engine_design_v2.md`
**Last updated:** May 2026

This spec is the source of truth for V1 scraper implementation. When in doubt, prefer this doc's conventions over generic patterns. Deviate only with explicit justification.

---

## 1. Architecture Overview

Three-layer medallion pipeline. Scrapers write raw JSONL; a separate loader normalizes to Postgres; extraction runs over normalized records.

```
[HTTP sources]
   │
   ↓
[scraper]  ──→  data/raw/source={src}/date=YYYY-MM-DD/run={ulid}.jsonl
   │              (one record per line, self-describing wrapper)
   │
   ↓
[loader]   ──→  postgres.raw_product_snapshot (bronze, JSONB)
   │
   ↓
[normalize] ──→ postgres.product, vendor_product, product_snapshot (silver)
   │
   ↓
[extract]  ──→  postgres.product_profile (structured flavor/mouthfeel/qi)
                postgres.product_embedding (pgvector)
```

**Separation rationale.** Scrapers fail. Schemas evolve. LLM extraction prompts change. By keeping raw JSONL immutable and re-runnable, we can re-derive bronze/silver/profile layers from raw without re-hitting external sources. Standard medallion architecture; same shape as the IPC pipeline.

**No scraper writes directly to Postgres.** A scraper run produces JSONL files. A separate `load` command ingests JSONL → bronze. A separate `normalize` command builds silver. A separate `extract` command runs LLM extraction. Each is independently re-runnable.

---

## 2. Tooling

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Matches Gary's existing Lambda/FastAPI stack |
| HTTP | `httpx` (sync) | Modern, ergonomic, supports HTTP/2; sync is sufficient for V1 |
| HTML parsing | `selectolax` for speed, `beautifulsoup4` for ergonomics | Use selectolax for hot paths (Steepster crawl), bs4 elsewhere |
| Reddit | `praw` | Standard, well-maintained, handles auth |
| Schema validation | `pydantic` v2 | Industry standard; integrates with FastAPI if surfaces grow |
| CLI | `click` | Standard, composable, supports nested commands |
| Logging | `structlog` | Structured JSON logging out of the box |
| Config | `pydantic-settings` + YAML for vendor configs | Env vars for secrets, YAML for per-vendor parameters |
| YAML parsing | `pyyaml` >=6.0 | Required by the YAML side of the config layer (`config/vendors.yaml`, §4); pydantic-settings does not bundle a YAML loader |
| Database | Postgres 16 + `pgvector` extension | V1 future-proof per design decision. Note: the PyPI package and the Postgres extension are both called "pgvector," but the extension installs under the canonical name `vector` — `CREATE EXTENSION vector` (see §8) |
| pgvector SQLAlchemy bridge | `pgvector` >=0.3 (Python package) | Provides the `Vector` SQLAlchemy column type used by `product_embedding`; distinct from the Postgres extension of the same name |
| ORM | `sqlalchemy` 2.0 (async optional) | Standard, mature |
| Postgres driver | `psycopg[binary]` >=3.1 | SQLAlchemy does not bundle a driver; psycopg 3 is the modern successor to psycopg2 and the `[binary]` extra avoids a libpq build step on dev machines |
| Migrations | `alembic` | Standard companion to SQLAlchemy |
| Linting/format | `ruff` | Replaces black, isort, flake8 |
| Testing | `pytest` + `vcrpy` for HTTP fixtures | VCR records real responses for offline replay |
| Orchestration | Local `cron` calling CLI | Per V1 decision; Lambda comes later |
| ID generation | `python-ulid` | Time-sortable, URL-safe, better than uuid4 for run IDs |

**Tools agents should NOT add without justification:** Scrapy (overkill, opinionated), Selenium (Playwright is better if browser automation is needed; not anticipated for V1), Celery/Redis (overkill for cron-based V1), pandas (heavy for our use; use SQLAlchemy + dicts).

---

## 3. Project Structure

```
tea-scrapers/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore                       # data/raw/ included
├── alembic.ini
├── alembic/versions/
├── docker-compose.yml               # Postgres + pgvector for local dev
├── config/
│   └── vendors.yaml                 # Per-vendor Shopify config
├── src/tea_scrapers/
│   ├── __init__.py
│   ├── cli.py                       # Click entrypoint
│   ├── config.py                    # Pydantic Settings
│   ├── logging.py                   # structlog config
│   ├── http/
│   │   ├── client.py                # httpx wrapper with retry/backoff
│   │   └── ratelimit.py             # Per-host token bucket
│   ├── storage/
│   │   ├── raw.py                   # JSONL writer with partitioning
│   │   ├── models.py                # SQLAlchemy ORM models
│   │   └── session.py               # DB session factory
│   ├── sources/
│   │   ├── base.py                  # Scraper protocol/ABC
│   │   ├── shopify.py               # Generic Shopify scraper
│   │   ├── steepster.py
│   │   ├── teadb.py
│   │   └── reddit_source.py
│   ├── load/
│   │   └── bronze.py                # JSONL → raw_product_snapshot
│   ├── normalize/
│   │   ├── canonical.py             # Canonical product ID matching
│   │   └── products.py              # Bronze → silver
│   └── schemas/
│       ├── ingest.py                # JSONL record wrapper
│       ├── shopify.py               # Shopify product/variant schemas
│       ├── steepster.py
│       └── extracted.py             # LLM extraction output
├── tests/
│   ├── conftest.py
│   ├── fixtures/                    # VCR cassettes + golden JSONL
│   ├── unit/
│   └── integration/
└── data/
    └── raw/                         # JSONL output (gitignored)
        └── source={source}/
            └── date=YYYY-MM-DD/
                └── run={ulid}.jsonl
```

---

## 4. Conventions

### Configuration

Secrets from env vars (`.env` for local, gitignored). Per-vendor params from `config/vendors.yaml`. Two distinct sources of config; don't mix them.

```python
# src/tea_scrapers/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "postgresql://localhost/tea"
    raw_data_dir: Path = Path("data/raw")
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "tea-rec-engine/0.1 (contact: gary@...)"
    user_agent: str = "tea-rec-engine/0.1 (https://github.com/gary/tea; contact: gary@...)"
    log_level: str = "INFO"
    
    model_config = {"env_file": ".env"}
```

### HTTP Client

All HTTP goes through one shared client. Agents do not instantiate `httpx.Client()` directly in source modules.

```python
# src/tea_scrapers/http/client.py
class HttpClient:
    """
    Wrapper around httpx with:
    - Per-host rate limiting (token bucket)
    - Exponential backoff retry (3 retries, base 1s)
    - User-Agent injection
    - Request/response logging
    - Raises ScrapeError on terminal failures
    """
    def get(self, url: str, **kwargs) -> httpx.Response: ...
    def get_json(self, url: str, **kwargs) -> dict: ...
```

The set of HTTP statuses that trigger retry is exposed as `HttpClient.RETRYABLE_STATUSES` (class attribute). V1 default: `{408, 425, 429}` plus all 5xx. Vendor configs may override per-host when a source's semantics differ (e.g., a vendor that returns 451 for transient rate-limit errors).

Rate limits default to 2 req/sec per host. Override via vendor config when needed (Steepster: 1 req/sec).

### Logging

Structured logs only. Every scraper emits at least these events:

- `scrape.run.start` with `run_id`, `source`, `mode`
- `scrape.request` with `url`, `status`, `duration_ms`
- `scrape.record` with `external_id`, `payload_bytes` (debug level)
- `scrape.run.end` with `records_count`, `errors_count`, `duration_s`

`scrape.request` carries `(url, status, duration_ms)` on every HTTP attempt. On transport failure (DNS, connect, read error) the event still fires with `status=None` and an additional `error=<exception class name>` key. Log consumers filtering on `status` must treat `None` as "transport-layer failure", distinct from 5xx.

```python
import structlog
log = structlog.get_logger()
log.info("scrape.run.start", run_id=run_id, source=source, mode=mode)
```

### Error Handling

- HTTP retries handled in `HttpClient`. Don't reimplement.
- A single failed record does NOT abort a run. Log the error, continue, surface count in `scrape.run.end`.
- A 5xx storm or auth failure DOES abort. Use `ScrapeError` for terminal failures.
- Never `except Exception: pass`. Either handle the specific exception or re-raise.

### Idempotency

A scrape run produces a new JSONL file with a unique `run_id` (ULID). Re-running is safe: it produces another file in the same partition. The loader uses `(source, external_id, payload_hash)` as the dedup key when going from JSONL → bronze. The hash is SHA-256 of the canonical JSON encoding of `payload` (`sort_keys=True`, compact separators, UTF-8, `ensure_ascii=False`) — always computed from the JSONL payload, never recomputed from a JSONB round-trip.

---

## 5. Raw Storage Layer

### File path convention

```
data/raw/source={source}/date=YYYY-MM-DD/run={ulid}.jsonl
```

Hive-style partitioning. Date is UTC scrape date. ULID is sortable.

### Record format

Each line is one record with a self-describing wrapper:

```json
{
  "ingest_meta": {
    "source": "yunnan_sourcing_us",
    "scraped_at": "2026-05-16T14:30:00Z",
    "run_id": "01HXY4Z9...",
    "endpoint": "https://yunnansourcing.us/products.json?page=3",
    "record_index": 47,
    "external_id": "7384921093"
  },
  "payload": { /* raw upstream object, unmodified */ }
}
```

**The `payload` is never massaged.** It's the exact upstream object. All parsing happens downstream. This is what makes re-parsing cheap when extraction logic evolves.

### Schema

```python
# src/tea_scrapers/schemas/ingest.py
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
```

---

## 6. Per-Source Specifications

### 6.1 Shopify Quartet (Tier 1)

Four vendors share one generic scraper, configured via `config/vendors.yaml`.

#### Endpoint

```
GET https://{base_url}/products.json?limit=250&page={N}
```

- No authentication required
- Returns up to 250 products per page
- Paginate by incrementing `page` until the `products` array is empty
- Hard cap: 250 pages × 250 = 62,500 products (no vendor approaches this)

#### Response shape

```json
{
  "products": [
    {
      "id": 7384921093,
      "title": "2025 Yunnan Sourcing \"Yi Bang Village\" Ripe Pu-erh Tea Cake",
      "handle": "2025-yunnan-sourcing-yi-bang-village-ripe-pu-erh-tea-cake",
      "body_html": "<p>A distinctive single-village ripe pu-erh...</p>",
      "published_at": "2026-03-15T08:00:00Z",
      "created_at": "2026-03-10T12:30:00Z",
      "updated_at": "2026-05-15T16:22:00Z",
      "vendor": "Yunnan Sourcing",
      "product_type": "Ripe Pu-erh",
      "tags": ["pu-erh", "ripe", "yi-bang", "2025", "single-village"],
      "variants": [
        {
          "id": 41928374,
          "title": "100g cake",
          "price": "32.50",
          "available": true,
          "sku": "YS-2025-YIB-100",
          "grams": 100
        }
      ],
      "images": [...]
    }
  ]
}
```

#### Vendor config

```yaml
# config/vendors.yaml
shopify_vendors:
  yunnan_sourcing_us:
    display_name: "Yunnan Sourcing USA"
    base_url: "https://yunnansourcing.us"
    rate_limit_rps: 2
    
  yunnan_sourcing_com:
    display_name: "Yunnan Sourcing China"
    base_url: "https://yunnansourcing.com"
    rate_limit_rps: 2
    
  white2tea:
    display_name: "white2tea"
    base_url: "https://white2tea.com"
    rate_limit_rps: 2
    
  crimson_lotus:
    display_name: "Crimson Lotus Tea"
    base_url: "https://crimsonlotustea.com"
    rate_limit_rps: 2
```

#### Output

One JSONL record per product. The `payload` is the full product object from Shopify (including all variants and images). `external_id` is the product's `id` as a string.

#### Gotchas

- The `available` flag lives on variants, not products. A product is "in stock" if any variant has `available: true`.
- Some vendors use the same product across `.us` and `.com` with different IDs. Canonical matching is downstream (in `normalize/canonical.py`), not the scraper's job.
- `body_html` contains the description. Some vendors include rich HTML with images and tables; some plain paragraphs. Strip HTML downstream in normalize, not in scrape.
- `published_at: null` means the product is not currently visible to public. Capture it anyway, mark with a flag.

#### Implementation skeleton

```python
# src/tea_scrapers/sources/shopify.py
class ShopifyScraper(SourceScraper):
    def __init__(self, vendor_key: str, config: VendorConfig, http: HttpClient, writer: JsonlWriter):
        self.vendor_key = vendor_key
        self.config = config
        self.http = http
        self.writer = writer
    
    def run(self, mode: Literal["full", "incremental"]) -> RunSummary:
        run_id = str(ULID())
        scraped_at = datetime.now(UTC)
        page = 1
        total = 0
        
        while True:
            url = f"{self.config.base_url}/products.json?limit=250&page={page}"
            data = self.http.get_json(url)
            products = data.get("products", [])
            
            if not products:
                break
            
            for idx, product in enumerate(products):
                record = RawRecord(
                    ingest_meta=IngestMeta(
                        source=self.vendor_key,
                        scraped_at=scraped_at,
                        run_id=run_id,
                        endpoint=url,
                        record_index=total + idx,
                        external_id=str(product["id"]),
                    ),
                    payload=product,
                )
                self.writer.write(record)
            
            total += len(products)
            page += 1
        
        return RunSummary(run_id=run_id, records=total, errors=0)
```

### 6.2 Steepster (Tier 2)

HTML scraping. No public API. Used for joining tasting notes to catalog products at the producer + style level.

#### URLs

- Vendor index: `https://steepster.com/companies/{slug}/teas` (paginated)
- Tea detail: `https://steepster.com/teas/{vendor-slug}/{tea-id}-{tea-slug}`
- Tasting notes embedded in tea detail page (paginated section)

#### Vendor slugs to crawl

Initial list (matches Tier 1 Shopify quartet plus high-history vendors):
- `yunnan-sourcing`
- `white2tea`
- `crimson-lotus-tea`
- `bitterleaf-teas`
- `menghai-tea-factory`
- `xiaguan-tea-factory`
- `dayi` (alternate name for Menghai)

#### Strategy

1. For each vendor slug, paginate the vendor index, collect tea URLs
2. For each tea URL, fetch detail page, extract:
   - Tea name
   - Stated metadata (year, style, etc.)
   - All tasting notes on the page (paginated; follow pagination)
3. Write one JSONL record per tea, with all notes inlined

#### Anti-bot considerations

- Steepster may serve Cloudflare. Start with plain `httpx`. If 403/503 responses appear, switch to `cloudscraper` (drop-in replacement). Do not jump to Playwright without evidence it's needed.
- Rate limit at 1 req/sec, not 2.
- Always send realistic Accept-Language, Accept-Encoding headers.

#### Output

One JSONL record per tea:
```json
{
  "ingest_meta": {...},
  "payload": {
    "steepster_id": "12345",
    "url": "https://steepster.com/teas/yunnan-sourcing/12345-2013-yunnan-sourcing-yi-dian-hong",
    "name": "2013 Yunnan Sourcing 'Yi Dian Hong' Ripe Pu-erh Cake",
    "vendor_slug": "yunnan-sourcing",
    "average_rating": 79,
    "rating_count": 12,
    "description": "...",
    "tasting_notes": [
      {
        "author_hash": "sha256:...",
        "rating": 82,
        "body": "Sweet, woody, hints of cocoa...",
        "posted_at": "2014-03-15"
      }
    ]
  }
}
```

Hash author names rather than capturing them, for downstream privacy hygiene.

### 6.3 TeaDB.org (Tier 2)

WordPress site. Check `/wp-json/wp/v2/posts` first. If enabled (likely), use the JSON API. Otherwise fall back to sitemap.xml + HTML.

#### Endpoint (preferred)

```
GET https://teadb.org/wp-json/wp/v2/posts?per_page=100&page={N}
```

- Returns post objects with content, title, tags, categories, links, dates
- Paginate via `page` param
- Some posts may have featured images and excerpts

#### Output

One JSONL record per post:
```json
{
  "ingest_meta": {...},
  "payload": {
    "wp_id": 4521,
    "url": "https://teadb.org/...",
    "title": "...",
    "content_html": "...",
    "tags": [...],
    "categories": [...],
    "published_at": "...",
    "author": "..."
  }
}
```

### 6.4 Reddit (Tier 2)

PRAW. Subreddits: `r/puer`, `r/tea`. Pull submissions plus top-level comments.

#### Configuration

Requires:
- `reddit_client_id`
- `reddit_client_secret`
- `reddit_user_agent` (must identify project per Reddit's API rules)

#### Strategy

Date-windowed crawl (Reddit caps listings at ~1000 per query):
1. For each subreddit, walk backwards from now in 7-day windows
2. For each window, search `subreddit.search("*", time_filter="all", sort="new")` with date constraints
3. For each submission, fetch up to top 50 comments, hash author names

#### Rate limit

PRAW respects Reddit's API limits automatically (60 req/min). Don't override.

#### Output

One JSONL record per submission, with comments nested.

---

## 7. CLI Interface

The CLI is the only interface agents implement for orchestration. Cron calls the CLI; no daemons, no Lambda wrappers (yet).

```bash
# Initial full scrape of a single Shopify source
tea-scrape ingest shopify --vendor yunnan_sourcing_us --mode full

# Daily incremental (re-scrapes everything, dedup downstream)
tea-scrape ingest shopify --vendor yunnan_sourcing_us --mode incremental

# All Shopify vendors
tea-scrape ingest shopify --all

# Steepster, TeaDB, Reddit
tea-scrape ingest steepster --vendor yunnan-sourcing
tea-scrape ingest teadb
tea-scrape ingest reddit --subreddit puer --since 7d

# Load JSONL → bronze
tea-scrape load --since 2026-05-16

# Run canonical ID matching and silver normalization
tea-scrape normalize --since 2026-05-16

# Status / health
tea-scrape status
tea-scrape status --source yunnan_sourcing_us
```

Exit codes: 0 success, 1 partial failure (some records errored), 2 terminal failure. Cron should alert on non-zero.

---

## 8. Postgres Schema

DDL the loader expects. Migration sits in `alembic/versions/001_initial.py`.

```sql
-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;  -- pgvector installs under the canonical name `vector`
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- for fuzzy name matching

-- Bronze: raw immutable snapshots
CREATE TABLE raw_product_snapshot (
    snapshot_id    BIGSERIAL PRIMARY KEY,
    source         TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    scraped_at     TIMESTAMPTZ NOT NULL,
    run_id         TEXT NOT NULL,
    payload        JSONB NOT NULL,
    payload_hash   TEXT NOT NULL,  -- sha256 of canonical payload, for change detection
    UNIQUE (source, external_id, payload_hash)
);
CREATE INDEX idx_raw_source_time ON raw_product_snapshot (source, scraped_at DESC);
CREATE INDEX idx_raw_external ON raw_product_snapshot (source, external_id);

-- Silver: canonical entities
CREATE TABLE producer (
    producer_id    BIGSERIAL PRIMARY KEY,
    canonical_name TEXT UNIQUE NOT NULL,
    aliases        TEXT[],
    parent_brand   TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_producer_name_trgm ON producer USING gin (canonical_name gin_trgm_ops);

CREATE TABLE region (
    region_id      BIGSERIAL PRIMARY KEY,
    country        TEXT NOT NULL,
    province       TEXT,
    county         TEXT,
    mountain       TEXT,
    village        TEXT,
    UNIQUE (country, province, county, mountain, village)
);

CREATE TABLE vendor (
    vendor_id      BIGSERIAL PRIMARY KEY,
    source_key     TEXT UNIQUE NOT NULL,  -- e.g. "yunnan_sourcing_us"
    display_name   TEXT NOT NULL,
    base_url       TEXT
);

CREATE TABLE product (
    product_id     BIGSERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    producer_id    BIGINT REFERENCES producer(producer_id),
    region_id      BIGINT REFERENCES region(region_id),
    tea_type       TEXT,  -- white/green/oolong/black/dark/etc
    tea_style      TEXT,  -- sheng/shou/yancha/sencha/etc
    harvest_year   INT,
    cultivar       TEXT,
    format         TEXT,  -- cake/brick/tuocha/loose/etc
    weight_grams   INT,
    data_quality_tier CHAR(1),  -- A/B/C/D per design doc
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_product_producer_year ON product (producer_id, harvest_year);
CREATE INDEX idx_product_name_trgm ON product USING gin (canonical_name gin_trgm_ops);

CREATE TABLE vendor_product (
    vendor_product_id  BIGSERIAL PRIMARY KEY,
    vendor_id          BIGINT NOT NULL REFERENCES vendor(vendor_id),
    product_id         BIGINT NOT NULL REFERENCES product(product_id),
    vendor_external_id TEXT NOT NULL,
    vendor_url         TEXT,
    UNIQUE (vendor_id, vendor_external_id)
);
-- For Shopify sources, ``vendor_external_id`` is the composite
-- ``"{shopify_product_id}:{shopify_variant_id}"`` so each weight variant
-- maps to a distinct silver ``product`` row + ``vendor_product`` row.
-- See ``VendorProduct`` docstring in ``storage/models.py`` for the full
-- rationale (and §12 OQ for the non-Shopify scheme, to be documented when
-- Steepster / TeaDB / Reddit land in step 7+).

-- Silver fact: daily inventory/price snapshots
CREATE TABLE product_snapshot (
    snapshot_id        BIGSERIAL PRIMARY KEY,
    vendor_product_id  BIGINT NOT NULL REFERENCES vendor_product(vendor_product_id),
    scraped_at         TIMESTAMPTZ NOT NULL,
    available          BOOLEAN NOT NULL,
    price_cents        INT,
    currency           CHAR(3),
    description_hash   TEXT,  -- detect description changes
    UNIQUE (vendor_product_id, scraped_at)
);
CREATE INDEX idx_snapshot_time ON product_snapshot (scraped_at DESC);
CREATE INDEX idx_snapshot_available ON product_snapshot (vendor_product_id, scraped_at DESC) WHERE available = TRUE;

-- Extraction outputs (V1.5)
CREATE TABLE product_profile (
    product_id         BIGINT PRIMARY KEY REFERENCES product(product_id),
    flavor_tags        JSONB,  -- list of {l3_id, intensity, confidence}
    mouthfeel          JSONB,
    hou_yun            INT,
    hui_gan            INT,
    sheng_jin          INT,
    cha_qi             JSONB,
    aging_state        TEXT,
    quote_evidence     JSONB,
    extracted_at       TIMESTAMPTZ DEFAULT NOW(),
    extraction_version TEXT
);

CREATE TABLE product_embedding (
    product_id   BIGINT PRIMARY KEY REFERENCES product(product_id),
    embedding    VECTOR(1536),  -- text-embedding-3-large dim
    model        TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_embedding_hnsw ON product_embedding USING hnsw (embedding vector_cosine_ops);

-- Run tracking
CREATE TABLE scrape_run (
    run_id         TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    mode           TEXT NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ,
    status         TEXT NOT NULL,  -- running/success/partial/failed
    records_count  INT,
    errors_count   INT,
    error_summary  TEXT
);
```

### Canonical product ID matching

A separate concern handled in `normalize/canonical.py`. Given a raw vendor product, match it to an existing `product` row or create a new one. Order of checks:

1. Exact match on `(producer_id, harvest_year, normalized_name, weight_grams)` → reuse
2. Trigram similarity above threshold (0.85) on `canonical_name` filtered by producer → candidate
3. LLM tiebreaker for ambiguous candidates → reuse or create
4. Otherwise create new `product` row

Trigram index supports the fuzzy match step efficiently. Build this on day 1.

---

## 9. Testing Strategy

### Unit tests

- Pydantic schemas with valid/invalid inputs
- Pagination logic with mocked responses
- Canonical ID matching with synthetic name variations
- JSONL writer with partitioning correctness

### Integration tests

VCR cassettes record real HTTP responses once, then replay offline:

```python
# tests/integration/test_shopify_scraper.py
@vcr.use_cassette("fixtures/yunnan_sourcing_us_page_1.yaml")
def test_shopify_scrapes_yunnan_sourcing(http_client, jsonl_writer):
    scraper = ShopifyScraper("yunnan_sourcing_us", config, http_client, jsonl_writer)
    summary = scraper.run(mode="full")
    assert summary.records > 1000
    assert summary.errors == 0
```

Re-record cassettes annually or when endpoint shapes change. Don't re-record on every test run.

### Golden JSONL

Maintain `tests/fixtures/golden/{source}.jsonl` with ~10 representative records per source. Use these for downstream loader/normalizer testing without re-running scrapers.

#### Authoring procedure

To create or regenerate a golden file, **derive it from the committed cassette**, never from an independent live scrape. A golden sampled from a different scrape than the committed cassette captures will silently drift on timestamp-like fields (`updated_at`, etc.) — the bronze loader's `payload_hash` dedup treats the drifted record as new. The `test_golden_payloads_match_cassette` test (added 2026-05-16) catches this class of bug.

Canonical procedure:

1. Record the cassette first: `VCR_RECORD_MODE=once pytest tests/integration/test_shopify_<vendor>.py::test_cli_full_run_against_cassette`.
2. Trim the cassette if it exceeds ~5 MB (see the `yunnan_sourcing_com` precedent — hand-trimmed to pages 1+2 + a synthesized empty terminator).
3. Generate the golden by **replaying the committed cassette** (not by re-scraping live):
   - Parse the cassette YAML's first interaction (`?page=1`), `response.body.string`, and `json.loads(...)["products"]`.
   - Take the first 10 products.
   - Wrap each in a `RawRecord`-shaped dict with synthetic `ingest_meta`:
     - `source`: vendor `source_key`
     - `scraped_at`: deterministic placeholder (`"2026-05-16T00:00:00Z"`)
     - `run_id`: deterministic placeholder (`"01HXY4Z9GOLDEN_FIXTURE_<SUFFIX>"`, e.g. `_YS_US`, `_W2T`, `_CLT`, `_YS_COM`)
     - `endpoint`: the cassette page-1 URI
     - `record_index`: sequential 0..9
     - `external_id`: `str(product["id"])`
   - `payload`: the product dict verbatim (no mutation, per §11).
4. Verify with: `pytest tests/integration/test_shopify_<vendor>.py::test_golden_payloads_match_cassette`. Failure means the golden drifts from the cassette — fix before commit.

The procedure is intentionally byte-faithful: a re-recorded cassette + re-derived golden are the only way to refresh fixtures, and they move in lockstep.

---

## 10. Implementation Sequencing

Build in this order. Each step is independently validatable before moving on.

1. **Scaffolding**: pyproject, project structure, Postgres docker-compose, alembic init, CLI shell
2. **Shared infra**: HttpClient with rate limit + retry, JsonlWriter with partitioning, structlog config, run tracking
3. **Shopify scraper**: generic implementation, vendor config loader, end-to-end test against `yunnansourcing.us`
4. **Run remaining Shopify vendors**: white2tea, Crimson Lotus, Yunnan Sourcing .com (Bitterleaf removed in step 3 — see §12 first bullet)
5. **Bronze loader**: JSONL → `raw_product_snapshot` with dedup via `payload_hash`
6. **Canonical ID matcher** + **silver normalizer**: Shopify products → product/vendor_product/product_snapshot rows
7. **Steepster scraper**: separate module, slower rate limit, HTML parsing
8. **TeaDB scraper**: WordPress JSON API preferred
9. **Reddit scraper**: PRAW with date-windowed crawl
10. **Cron integration**: shell script + crontab entry committed to repo as `scripts/crontab.example`

Steps 1-6 are the V1 Tier 1 backbone. After step 6, the structured silver layer is queryable and the recommender can start being built against it in parallel with Tier 2 scrapers.

---

## 11. Anti-Patterns

Things agents should NOT do:

- **Don't write to Postgres from a scraper.** Scrapers write JSONL. Loaders read JSONL. Don't merge these.
- **Don't filter at scrape time.** Capture every record the source returns, even if it looks irrelevant. Filtering is downstream.
- **Don't mutate the `payload`.** Whatever the upstream returns, write it through verbatim. Parsing and cleaning happens in normalize.
- **Don't hardcode vendor URLs or API keys.** URLs in `config/vendors.yaml`, keys in env vars.
- **Don't add per-source retry logic.** Use `HttpClient`. If it's insufficient, fix it there, not in source modules.
- **Don't catch broad exceptions.** `except Exception: pass` is a bug. Catch the specific class or let it propagate.
- **Don't add browser automation prematurely.** Try `httpx` first. Try `cloudscraper` second. Only reach for `playwright` with evidence it's needed.
- **Don't introduce async without justification.** Sync is sufficient for V1 throughput. Async raises debugging cost.
- **Don't skip the bronze layer.** Even if a scrape feels simple, raw records always go through `raw_product_snapshot` before being normalized. This is what makes re-derivation cheap.
- **Don't add new dependencies casually.** The tooling table in §2 is the approved set. New deps require justification in the PR description.

---

## 12. Open Items For Implementer

- ~~Verify whether Crimson Lotus Tea and Bitterleaf Teas are actually on Shopify (`/products.json` returns valid response). If not, escalate before building generic scraper.~~ **Resolved 2026-05-16**: Crimson Lotus is Shopify (kept in `config/vendors.yaml`); Bitterleaf Teas runs WooCommerce (removed from `shopify_vendors`). A WooCommerce scraper for Bitterleaf is out of V1 scope; revisit post-V1 if community demand justifies it.
- Verify Steepster doesn't require auth for tea/note pages. Spot-check 5 pages with no cookies.
- Verify TeaDB has WordPress JSON API enabled (`/wp-json/wp/v2/posts` returns 200 with posts).
- Determine initial Reddit date window (suggest last 2 years for V1; full historical can come later).
- Decide canonical name normalization rules (case, punctuation, year position). Suggest pulling from a small ruleset in `normalize/canonical.py` with comments.
- **Stale `scrape_run` row sweep.** `RunTracker` uses two SQLAlchemy sessions (one to insert the 'running' row, one to finalize) so a long scrape doesn't hold an open transaction. If the finalize session can't reach Postgres, the row stays 'running' indefinitely. V1 accepts this; a cron-level sweep (`UPDATE scrape_run SET status='failed', error_summary='stale: no finalize' WHERE status='running' AND started_at < now() - interval '6 hours'`) should be added in V2 ops tooling.
- **Cassette repo-size trajectory.** Surfaced 2026-05-16 in step-4 code review. The four Shopify vendors land ~15 MB of cassette YAML in `tests/fixtures/cassettes/`. Steepster (per-tea pages, ~10K+ pages probable), TeaDB (WP posts), and Reddit (thread bodies) at the same fidelity will scale this past 100 MB before step 9. That's a real repo-clone tax and PR-review friction. Three options to decide before step 7 (Steepster scraper) lands:
  1. **Git LFS for `tests/fixtures/cassettes/`** — minimal code change; adds an LFS-pull step to CI and contributor setup.
  2. **Gzipped cassettes** (`*.yaml.gz`) — vcrpy supports compression natively; ~10× shrink for JSON-heavy bodies; cassettes are no longer human-grep-able for token-leak audits without decompression.
  3. **Per-source sampling policy** — keep a small "shape" cassette (3 pages worth) committed for replay tests; gitignore a larger sampled cassette that CI fetches from object storage on demand.
  Decision owner: tech-lead, before step 7. Default if undecided: option 2 (gzipped), since it's the smallest behavior change and the leak-audit grep can run on a decompressed temp file.
- **Shopify storefront bot mitigation (placeholder User-Agent + IP reputation).** Discovered 2026-05-16 during step-4 live `--all` smoke test: after ~30 min of sustained scraping from one IP (cassette recordings + smoke), `yunnan_sourcing_com` returned 403 on page 8, and `white2tea` + `crimson_lotus` returned 403 on page 1. `yunnan_sourcing_us` succeeded. The configured default User-Agent is the `.env.example` placeholder `'tea-rec-engine/0.1 (https://github.com/gary/tea; contact: gary@...)'` with a fake repo URL and a fake contact email — almost certainly part of what the storefronts' edge (likely Cloudflare in front of Shopify) is mitigating against. Note that **cassette-replay tests are unaffected** (100/100 pytest green), and the CLI's per-vendor failure-isolation path behaved correctly (`scrape_run` rows finalized cleanly as `failed` with `terminal: ScrapeError ... returned 403`). Recommended V1.1 fix: set `USER_AGENT` env to a real contact + real repo URL; consider lowering `rate_limit_rps` for these vendors from 2 to 1; cron the scraper so traffic is paced over hours instead of bursts. Out of V1 scope to add a cloudscraper / playwright fallback per §11 ("Don't add browser automation prematurely"). Operational details for V1.1 ops follow-up:
  - **`HttpClient` already treats 403 as terminal** (`src/tea_scrapers/http/client.py:116`) — the retry budget is correctly not burned on soft blocks, so a 403 immediately finalizes the `scrape_run` row as `failed`. No retry-loop hardening needed.
  - **Cool-down guidance.** A 403 from this class of mitigation typically clears in 5–60 minutes from the same IP. Cron the scraper at hourly cadence with per-vendor staggering rather than running `--all` back-to-back. Do not retry within the same process when a 403 hits; the next cron tick is the correct retry boundary.
  - **Detection & alerting.** Add a structlog counter / metric on `scrape.request` events with `status=403`. When the cumulative rate across any rolling 1-hour window exceeds (say) 5% of total requests for a vendor, page ops — that's the signal that mitigation has escalated beyond transient cool-down. `RunTracker` already finalizes per-vendor rows correctly, but there's no cross-run trend signal today.
  - **robots.txt position.** Not honored today. Shopify storefronts' `robots.txt` typically allows `/products.json` for low-rate crawlers but disallows aggressive paths (`/cart`, `/checkout`). Decide a V1.1 stance: opt-in honoring via `HttpClient`, or document explicitly that the project's scrapers are polite-but-not-robots-aware. Either way, the position should be in the spec, not implicit.
- **Bronze loader follow-ups.** Surfaced 2026-05-16 in step-5 code review (code-reviewer on the data-engineer diff). All five are non-blocking — the loader ships green at 124/124 — but each is a real cleanup with a clear owner:
  1. **Rename `LoadStats._vendor_bucket` → `LoadStats.bucket_for`** (`src/tea_scrapers/load/bronze.py`). Single-underscore "private" helper is called from outside the dataclass (`BronzeLoader.run`, `_iter_records`, `_flush_batch`). Cohesion is fine; just drop the false private-leak signal so future linters don't trip.
  2. **Add `LoadStats.insert_errors` counter**, distinct from `parse_errors`. Today the `IntegrityError` defensive catch in `_flush_batch` buckets failed-batch insert-validation errors (NOT NULL, FK, schema drift) into `parse_errors`, conflating them with per-line JSON / pydantic failures. Stat counters lose precision exactly this way; split before dashboards key off the merged name.
  3. **`tests/conftest.py::reset_session_caches()` helper.** `test_cli_terminal_failure_exit_2` reaches into `session._session_factory.cache_clear()` + `session._settings.cache_clear()` inline (with SLF001 noqa) to defeat module-level `@lru_cache` so a per-invocation `DATABASE_URL` env override actually takes effect. Works today (one consumer), but silently breaks if anyone adds another `lru_cache`d factory. Centralize.
  4. **Strengthen `test_since_filter_skips_older_partitions`.** Currently stages identical fixtures in both date partitions and asserts post-filter row count; with identical fixtures, dedup would produce the same row count even if the date filter no-op'd, so the test passes for the wrong reason. Stage a distinct fixture (different `external_id`) in the older partition and assert it's *not* in bronze.
  5. **Broader §4 (Idempotency) rewrite.** The line-193 sync in this PR fixed the stale dedup-key sentence, but §4's surrounding prose about "another file in the same partition is safe" is now better justified by hash-based dedup rather than `scraped_at`. Consolidate idempotency + dedup-key rationale into one place rather than scattered across §4 / §8. Decision owner: tech-lead.
- **Silver normalizer follow-ups.** Filed 2026-05-16 alongside step-6 (canonical ID matcher + silver normalizer landing). All non-blocking; each pinned for the right owner at the right time.
  1. **Non-Shopify `vendor_external_id` schemes.** Shopify uses the composite `"{shopify_product_id}:{shopify_variant_id}"` (documented in `storage/models.py::VendorProduct` docstring and §8 of this spec). Steepster / TeaDB / Reddit need their own analogous schemes when steps 7–9 land. Owner: scraper-engineer at that time. Until then, the silver normalizer assumes Shopify shape — adding a non-Shopify source requires `shopify_mapper.py` to become source-aware (or get peers).
  2. **`variant.grams` tare-in-packaging quirk.** Codified in `shopify_mapper.py::_parse_weight_from_option` and verified against the YS-US golden record (`"100 Grams"` option → `variant.grams=125`). Future engineers may be tempted to "simplify" by trusting `variant.grams` directly; the comment in the mapper exists to prevent that. Cross-link: the unit test `test_normalize_shopify_mapper.py::test_option1_overrides_variant_grams` asserts the divergence stays.
  3. **Currency=USD V1 hardcode.** All three current vendors quote USD; the silver fact column `product_snapshot.currency` is filled with the literal `"USD"`. Breaks the moment a non-US Shopify vendor lands (e.g. a Taiwanese / UK source). Resolution: lift to `VendorConfig.currency` and have the mapper read it. Owner: scraper-engineer at first non-USD vendor.
  4. **Cultivar + region extraction for non-YS vendors.** YS structured tags (`Producer_*`, `Region_*`, `Cultivar_*`) drive most of `shopify_mapper.py`'s field extraction today. white2tea and Crimson Lotus emit free-form tags and short product titles; the mapper falls back to `payload.vendor` + start-of-title regex but does not extract region or cultivar at all. Deferred to ml-engineer V1.5 (LLM-driven extraction over `body_html`).
  5. **LLM tiebreaker stub at matcher step 3.** `CanonicalMatcher.match_or_create_product` currently logs `silver_match_ambiguity` and falls through to over-create when 2+ trigram candidates cluster within 0.10 similarity. The spec §8 calls for an LLM tiebreaker here; V1 stub is conservative (over-create is recoverable, false-merge is not). Cross-link: ROADMAP step 7+ where ml-engineer picks this up.
  6. **Tier-sweep perf.** V1's `normalize/tier.py::assign_tiers` runs one CTE-based UPDATE per normalize run, scoped to the touched product IDs. Fine at thousands of products. At V2 scale (curated catalog tier D + multi-vendor cross-product, ~100K+ products), the CTE will start to dominate; the obvious optimization is a denormalized `product.last_available_at` column maintained on snapshot insert. Decision owner: data-engineer at first slow-run signal.
  7. **`--since` semantic divergence.** The bronze loader filters on partition date in the JSONL path (`source=.../date=YYYY-MM-DD/...`); the silver normalizer filters on the in-row `raw_product_snapshot.scraped_at::date`. Both accept `--since YYYY-MM-DD` but they mean slightly different things. Acceptable in V1 (the two values are equal in practice — `JsonlWriter` derives the partition from `scraped_at`), but document so operators don't get confused when they diverge under, say, a re-load of older partitions.
  8. **`product.canonical_name` not UNIQUE is intentional.** The matcher key is the 4-tuple `(producer_id, harvest_year, normalized_name, weight_grams)`; canonical names can legitimately repeat across e.g. different weight variants. A comment in `canonical.py` flags this; future readers should not "fix" this by adding a unique constraint.
  9. **Variant-id mutation on Shopify republish.** If a vendor deletes and re-creates a Shopify variant in their admin, the new variant_id is a different integer, and the silver normalizer will create a new `vendor_product` row (because `vendor_external_id` is composite of product_id + variant_id). The historical snapshot chain orphans onto the old row. Rare but possible; V1 accepts the duplication.
  10. **Producer / region creation idempotency under multi-writer.** V1 cron is single-writer; the cache-on-write semantics in `CanonicalMatcher` (in-process dict cache + SELECT-first) would race under concurrent normalize runs. Flag if/when multi-writer becomes a real configuration.
- **Silver normalizer polish items.** Filed 2026-05-16 in step-6 code review. Non-blocking hygiene; bundle into a single follow-up PR rather than landing in step 6.
  1. **`canonical.py` `_trgm_initialized_sessions` late-init.** Defers `set` allocation to first method call via `hasattr` guard. Move to `__init__` next to the other dict caches.
  2. **`canonical.py:25` docstring drift on aliases.** Claims "raw-form spellings are appended"; `_maybe_append_alias` actually stores the normalized form. Either reword the docstring or document why raw is intentionally dropped.
  3. **`tier.py` no-snapshots branch.** Products with zero snapshots get tier `'C'` via the ELSE arm of the CASE — correct per the V1 brief (treat as never-available), but worth a one-line comment so a future reader doesn't misread it as a bug. Revisit if curated tier-D ingestion ever produces snapshot-less products.
  4. **`silver.py` cross-batch cache survival.** `_producer_cache` / `_vendor_cache` / `_touched_product_ids` survive across batch transactions. If a batch `IntegrityError`s and rolls back, cached IDs created in that batch become stale and can FK-violate the next batch. Single-writer V1 keeps this rare; cross-link to OQ #10.
  5. **`test_normalize_tier.py::test_long_discontinued_lands_in_tier_c` boundary brittleness.** Uses `30*30` days (~29.6 months) vs. the 24-month tier-B cutoff. Bump to `weeks=110` or `days=30*40` for a less brittle margin.
  6. **`test_normalize_pipeline.py` row-count idiom.** Uses `len(.all())` to count rows at lines `:444+ (×6 sites)`. Should be `select(func.count()).select_from(...)` — same answer, less materialization.
  7. **AMBIGUOUS_GAP fall-through has no dedicated test.** `test_product_two_high_similarity_candidates_reuse_via_top_ceiling` exercises the `AMBIGUOUS_TOP_CEILING` reuse path (top_sim ≥ 0.95); no test currently exercises the `AMBIGUOUS_GAP` over-create path (top_sim in [0.85, 0.95) with second candidate within 0.10). Designing inputs that land in that band requires fiddly pg_trgm-similarity-aware text construction; defer to a polish PR.
