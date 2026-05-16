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

A scrape run produces a new JSONL file with a unique `run_id` (ULID). Re-running is safe: it produces another file in the same partition. The loader uses `(source, external_id, scraped_at)` as the dedup key when going from JSONL → bronze.

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
    
  bitterleaf:
    display_name: "Bitterleaf Teas"
    base_url: "https://bitterleafteas.com"
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
- **Shopify storefront bot mitigation (placeholder User-Agent + IP reputation).** Discovered 2026-05-16 during step-4 live `--all` smoke test: after ~30 min of sustained scraping from one IP (cassette recordings + smoke), `yunnan_sourcing_com` returned 403 on page 8, and `white2tea` + `crimson_lotus` returned 403 on page 1. `yunnan_sourcing_us` succeeded. The configured default User-Agent is the `.env.example` placeholder `'tea-rec-engine/0.1 (https://github.com/gary/tea; contact: gary@...)'` with a fake repo URL and a fake contact email — almost certainly part of what the storefronts' edge (likely Cloudflare in front of Shopify) is mitigating against. Note that **cassette-replay tests are unaffected** (100/100 pytest green), and the CLI's per-vendor failure-isolation path behaved correctly (`scrape_run` rows finalized cleanly as `failed` with `terminal: ScrapeError ... returned 403`). Recommended V1.1 fix: set `USER_AGENT` env to a real contact + real repo URL; consider lowering `rate_limit_rps` for these vendors from 2 to 1; cron the scraper so traffic is paced over hours instead of bursts. Out of V1 scope to add a cloudscraper / playwright fallback per §11 ("Don't add browser automation prematurely").
