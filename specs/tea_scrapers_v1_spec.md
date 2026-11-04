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
| Hanzi → pinyin (matching keys only) | `pypinyin` >=0.50,<1.0 | Canonical matcher only (§12 "Hybrid canonical matcher" sub-q #9 / H.2b): toneless pinyin + space/punctuation-insensitive skeleton keys for exact / trigram matching; stored names never rewritten. MIT, pure Python, zero transitive deps, 0.84 MB wheel / 3.9 MB installed (mostly the phrase dictionary that resolves polyphones), imported lazily. Alternatives: hand-rolled char table (no polyphone handling — 长 / 行 / 重 are common in tea names), `jieba`+table (bigger, segmentation we do not need), ICU `Han-Latin` transliterator via PyICU (C extension, platform wheels, tone-stripping still ours) |
| Name embeddings (OPTIONAL `[embed]` extra) | `torch` (CPU wheels) + `transformers` (≥4.51) + `huggingface-hub`, model BGE-M3; Qwen3-Embedding-0.6B adapter for the H.3 Option-B probe (not recommended for V1, see §12) | Hybrid canonical matcher only (§12 "Hybrid canonical matcher", H.3). Not in the base install; CI / pytest use committed fixture vectors. Justification and the FlagEmbedding / sentence-transformers comparison are in the §12 entry |

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

Rate limits default to 2 req/sec per host. Override via vendor config when needed (Steepster: 1 request per 10 sec per robots.txt `Crawl-Delay: 10` — see §6.2).

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

- Vendor index: `https://steepster.com/companies/{slug}` (paginated). The `/teas` suffix variant 404s — verified 2026-05-16 against `yunnan-sourcing`, `rishi-tea`, `tazo`, `mighty-leaf-tea` via `sitemap1.xml`.
- Tea detail: `https://steepster.com/teas/{vendor-slug}/{tea-id}-{tea-slug}` (pattern confirmed by sitemap, 2026-05-16)
- Tasting notes embedded in tea detail page (paginated section)
- Sitemap: `https://steepster.com/sitemap1.xml` (~9.3 MB, ~41,165 `/teas/` URLs + ~8,820 `/companies/` URLs in one document); three additional `sitemap{2,3,4}.xml` indexes referenced from `/robots.txt`.

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

**V1 enumeration: company-index pagination.** For each vendor slug in the V1 allowlist, walk `/companies/{slug}?page=N` and collect tea-detail URLs from each page. This is the implemented strategy as of step 7 (see step-7 follow-up #4 in §12 for rationale; the short version is that downloading the 9.3 MB `sitemap1.xml` per run is wasteful relative to a 7-vendor allowlist whose largest member produces ~3,700 detail fetches either way).

**Sitemap-based enumeration MAY be preferred** if the allowlist is removed or expands past ~20 vendors. Steepster publishes `https://steepster.com/sitemap1.xml` (~9.3 MB, lists ~41,165 tea URLs and ~8,820 company URLs in a single XML document) and three additional `sitemap{2,3,4}.xml` indexes referenced from `/robots.txt`. At that scale, sitemap-filter-then-fetch is cheaper than walking every vendor's company index.

1. For each vendor slug, paginate the vendor index, collect tea URLs
2. For each tea URL, fetch detail page, extract:
   - Tea name
   - Stated metadata (year, style, etc.)
   - All tasting notes on the page (paginated; follow pagination)
3. Write one JSONL record per tea, with all notes inlined

#### Anti-bot considerations

- Steepster may serve Cloudflare. Start with plain `httpx`. If 403/503 responses appear, switch to `cloudscraper` (drop-in replacement). Do not jump to Playwright without evidence it's needed.
- **Rate limit: 10 seconds between requests** per robots.txt `Crawl-Delay: 10` (`https://steepster.com/robots.txt`). Steepster overrides the global 2 req/sec default documented in §4 — configure via `VendorConfig.rate_limit_rps = 0.1` (or equivalent crawl-delay knob) for this source.
- **HTTP timeout ≥ 60 seconds.** Tea-detail page render times observed at 15–25 seconds on first hit (origin is slow + occasionally returns 504s before warming the surrogate cache); `sitemap1.xml` itself took ~8.7 sec to render (probe, 2026-05-16). Configure `HttpClient.timeout` for this source independently of the global default via the per-host override mechanism (§4). The shared client's 3-retry budget will absorb the transient 504s once timeout headroom is adequate.
- Always send realistic Accept-Language, Accept-Encoding headers.

#### Output

One JSONL record per tea. The shipped (step-7) record captures every visible field per §11; the schema below is canonical:

```json
{
  "ingest_meta": {
    "source": "steepster",
    "external_id": "12345",
    "endpoint": "https://steepster.com/teas/yunnan-sourcing/12345-...",
    "...": "..."
  },
  "payload": {
    "steepster_id": "12345",
    "url": "https://steepster.com/teas/yunnan-sourcing/12345-2013-yunnan-sourcing-yi-dian-hong",
    "name": "2013 Yunnan Sourcing 'Yi Dian Hong' Ripe Pu-erh Cake",
    "vendor_slug": "yunnan-sourcing",
    "average_rating": 79,
    "rating_count": 12,
    "description": "...",
    "description_pairs": {
      "Tea type": "Black Tea",
      "Ingredients": "...",
      "Flavors": "Cocoa, Malt, Honey, ...",
      "Sold in": "Bulk, Loose Leaf",
      "Caffeine": "High"
    },
    "prep": {
      "temp": "205 °F / 96 °C",
      "steep-time": "2 min, 45 sec",
      "tea-amount": "5 g",
      "water-volume": "9 oz / 272 ml"
    },
    "availability_text": "Currently unavailable",
    "tasting_notes": [
      {
        "steepster_note_id": "225516",
        "author_hash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "rating": 82,
        "body": "Sweet, woody, hints of cocoa...",
        "posted_at": "2014-03-15T12:00:00Z",
        "like_count": 24,
        "comment_count": 0
      }
    ]
  }
}
```

**Author hashing.** Hash author names rather than capturing them, for downstream privacy hygiene. Algorithm pinned 2026-05-16 (closes §12 step-7 OQ #5):

- **Unsalted SHA-256** of the NFC-normalized, lowercased, whitespace-stripped username, prefixed with the literal `sha256:`. Unsalted is intentional — cross-run reproducibility supports the "returning-reviewer" dedup the downstream silver layer needs, and a salt would make every run's hashes incomparable to the previous run's.
- Implementation: `tea_scrapers.sources.steepster.hash_author`. The `sha256:` prefix exists so a future schema migration to a salted scheme can be distinguished from V1 hashes without touching every historical row.

**Pagination terminators.** Resolved 2026-05-16 (closes §12 step-7 OQ #6):

- Company-index walk: terminate when a page has zero `/teas/{slug}/...` hrefs OR when no `<a>` element labeled `Next` with an `?page=` href is present. Both signals checked because the last numbered company page typically still has a "Next" link in the chrome.
- Tea-detail notes walk: terminate when a page has zero `<div class='note'>` blocks OR when no "Next + `?page=`" link is present (same partial as the company index).

**Config location.** Steepster lives in the same `config/vendors.yaml` as the Shopify vendors under a sibling top-level `steepster:` key rather than in a separate `config/sources.yaml` (decision pinned in the step-7 PR). The Steepster entry is **not a vendor** — it's one community source whose payload references many producers via `vendor_slug`. Loader is `tea_scrapers.config.load_steepster_config`. Schema:

```yaml
steepster:
  base_url: "https://steepster.com"
  rate_limit_rps: 0.1        # honors robots.txt Crawl-Delay: 10
  timeout_seconds: 60        # tea-detail render times observed at 15–25s
  vendor_slugs:              # V1 allowlist
    - "yunnan-sourcing"
    - "white2tea"
    - "crimson-lotus-tea"
    - "bitterleaf-teas"
    - "menghai-tea-factory"
    - "xiaguan-tea-factory"
    - "dayi"
```

**`STEEPSTER_RATE_LIMIT_RPS` env override.** The loader honors this env var to let VCR-replay integration tests bypass the 10-second crawl delay (the rate limiter has no idea VCR is intercepting and would sleep against a wall clock anyway). Operators can also use it to tune live rate temporarily without editing the committed YAML. The committed YAML default (0.1 rps) is the polite-citizen contract — env override should be set to the higher value (e.g. `100`) only in test/dev contexts.

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

A separate concern handled in `normalize/canonical.py`. Given a raw vendor product, match it to an existing `product` row or create a new one.

**V1 baseline (trigram, shipped in step 6).** Order of checks:

1. Exact match on `(producer_id, harvest_year, normalized_name, weight_grams)` → reuse
2. Trigram similarity above threshold (0.85) on `canonical_name` filtered by producer (and, as implemented, by `harvest_year` + `weight_grams` via `IS NOT DISTINCT FROM`) → candidate
2.5. Attribute guard (H.2, `normalize/attributes.py`): a trigram candidate whose name-parsed recipe code, item number (`No.15` / `#03` / `Part 2` / `Pt.1` / `Grade 3` / model codes like `BP-12`, `V93`) or sheng/shou conflicts with the query is vetoed before the ambiguity check; when both `harvest_year`s are NULL the guard compares the vintage year parsed anywhere in the name (matching-time only; the stored column keeps the mapper's start-of-title semantics); all vetoed → create (`veto_created`). Steps 1–2 compare on a matching key that strips evidenced inventory suffixes (`" :: Seattle Inventory"`); the stored `canonical_name` is still the raw vendor title. See §12 hybrid sub-question #7.
3. LLM tiebreaker for ambiguous candidates → reuse or create (V1 ships a conservative stub that over-creates; see §12 silver normalizer follow-up #5)
4. Otherwise create new `product` row

The producer matcher follows the same shape (NFC norm → alias → exact → trigram → create) but has no ambiguity-gap check today. Trigram index supports the fuzzy match step efficiently; it was built on day 1 (`idx_product_name_trgm` in 001, `idx_producer_name_trgm` in 002) and stays.

**Approved evolution: hybrid matcher (owner-resolved 2026-10-31; see §12 "Hybrid canonical matcher").** Both the product and producer matchers move to: exact → attribute guard (parsed recipe code / sheng-shou / year / weight; any mismatch vetoes the pair) → candidate generation from trigram **and** embedding kNN → hybrid score with thresholds calibrated on a hand-labeled pair set → LLM tiebreaker for the ambiguous band → create. Name embeddings are local BGE-M3, stored as `halfvec(1024)` in a new `name_embedding` table (migration 003), separate from the description-level `product_embedding` table above. The hybrid path ships behind a matcher-strategy config flag (`trigram | hybrid`, default `trigram`) in shadow mode; the default flips only if hybrid beats the trigram baseline on the labeled set. Trigram is the baseline and a permanent candidate generator, not a deprecated path. Until the flip, the V1 baseline above is the normative behavior.

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

Cassettes for sources added in step 7 and later MUST be recorded with a `.yaml.gz` extension. The custom `GzipFilesystemPersister` registered on the `vcr_cassette` fixture (`tests/_vcr_gzip.py`) handles transparent gzip read/write; vcrpy itself does not auto-detect the suffix. The four Shopify cassettes from steps 3–4 are grandfathered uncompressed and MUST NOT be re-recorded without regenerating their golden JSONL fixtures in the same commit (lockstep procedure below).

### Golden JSONL

Maintain `tests/fixtures/golden/{source}.jsonl` with ~10 representative records per source. Use these for downstream loader/normalizer testing without re-running scrapers.

#### Authoring procedure

To create or regenerate a golden file, **derive it from the committed cassette**, never from an independent live scrape. A golden sampled from a different scrape than the committed cassette captures will silently drift on timestamp-like fields (`updated_at`, etc.) — the bronze loader's `payload_hash` dedup treats the drifted record as new. The `test_golden_payloads_match_cassette` test (added 2026-05-16) catches this class of bug.

Canonical procedure:

1. Record the cassette first: `VCR_RECORD_MODE=once pytest tests/integration/test_shopify_<vendor>.py::test_cli_full_run_against_cassette`. For sources added in step 7 and later, set `CASSETTE = "<vendor>_products.yaml.gz"` in the test module before recording — the rest of the invocation is identical; the gzip persister handles compression transparently on save.
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
5. **Leak audit (gzipped cassettes).** Before committing a `.yaml.gz` cassette: `zcat tests/fixtures/cassettes/<name>.yaml.gz | grep -iE 'set-cookie|authorization'`. The `before_record_response` scrubber in `tests/conftest.py` already strips these; the grep is belt-and-suspenders for the case where a new source emits a header the scrubber doesn't yet know about. The grep must produce no matches.

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
- **Don't commit uncompressed cassettes for sources added step 7+.** The `GzipFilesystemPersister` exists precisely so per-tea-page scrapers (Steepster, TeaDB, Reddit) don't push `tests/fixtures/cassettes/` past 100 MB. New cassettes land as `.yaml.gz`.
- **Don't re-record the Shopify-quartet cassettes without regenerating their goldens in the same commit.** The byte-faithful pairing in §9 ("Authoring procedure") is what keeps `test_golden_payloads_match_cassette` honest; a cassette refresh without a golden refresh silently breaks the dedup invariants the bronze loader relies on.

---

## 12. Open Items For Implementer

- ~~Verify whether Crimson Lotus Tea and Bitterleaf Teas are actually on Shopify (`/products.json` returns valid response). If not, escalate before building generic scraper.~~ **Resolved 2026-05-16**: Crimson Lotus is Shopify (kept in `config/vendors.yaml`); Bitterleaf Teas runs WooCommerce (removed from `shopify_vendors`). A WooCommerce scraper for Bitterleaf is out of V1 scope; revisit post-V1 if community demand justifies it.
- ~~Verify Steepster doesn't require auth for tea/note pages. Spot-check 5 pages with no cookies.~~ **Resolved 2026-05-16**: probe confirmed no auth wall on root, post pages, sitemap, or `/teas/...` detail; no `/users/sign_in` redirects on the relevant paths. Probe also surfaced the §6.2 corrections (vendor index URL pattern, `Crawl-Delay: 10`, sitemap-preferred enumeration, HTTP timeout ≥60s) landed in the same PR.
- Verify TeaDB has WordPress JSON API enabled (`/wp-json/wp/v2/posts` returns 200 with posts).
- Determine initial Reddit date window (suggest last 2 years for V1; full historical can come later).
- Decide canonical name normalization rules (case, punctuation, year position). Suggest pulling from a small ruleset in `normalize/canonical.py` with comments.
- **Stale `scrape_run` row sweep.** `RunTracker` uses two SQLAlchemy sessions (one to insert the 'running' row, one to finalize) so a long scrape doesn't hold an open transaction. If the finalize session can't reach Postgres, the row stays 'running' indefinitely. V1 accepts this; a cron-level sweep (`UPDATE scrape_run SET status='failed', error_summary='stale: no finalize' WHERE status='running' AND started_at < now() - interval '6 hours'`) should be added in V2 ops tooling.
- ~~**Cassette repo-size trajectory.** Surfaced 2026-05-16 in step-4 code review. The four Shopify vendors land ~15 MB of cassette YAML in `tests/fixtures/cassettes/`. Steepster (per-tea pages, ~10K+ pages probable), TeaDB (WP posts), and Reddit (thread bodies) at the same fidelity will scale this past 100 MB before step 9. That's a real repo-clone tax and PR-review friction. Three options to decide before step 7 (Steepster scraper) lands:~~
  1. ~~**Git LFS for `tests/fixtures/cassettes/`** — minimal code change; adds an LFS-pull step to CI and contributor setup.~~
  2. ~~**Gzipped cassettes** (`*.yaml.gz`) — vcrpy supports compression natively; ~10× shrink for JSON-heavy bodies; cassettes are no longer human-grep-able for token-leak audits without decompression.~~
  3. ~~**Per-source sampling policy** — keep a small "shape" cassette (3 pages worth) committed for replay tests; gitignore a larger sampled cassette that CI fetches from object storage on demand.~~
  ~~Decision owner: tech-lead, before step 7. Default if undecided: option 2 (gzipped), since it's the smallest behavior change and the leak-audit grep can run on a decompressed temp file.~~
  **Resolved 2026-05-16**: Option 2 (gzipped cassettes) selected by tech-lead. vcrpy 8.1.1 does NOT auto-detect `.yaml.gz` from the filename suffix (`FilesystemPersister.load_cassette` opens cassettes via plain text-mode `path.open()`), so the "no code change" framing in option 2's original wording was incorrect. The fix is a 25-LOC `GzipFilesystemPersister` (`tests/_vcr_gzip.py`) registered on the `vcr_cassette` fixture in `tests/conftest.py` — it subclasses `FilesystemPersister` and wraps file I/O in `gzip.open(...)` when the cassette path ends in `.yaml.gz`; uncompressed paths fall through to the parent unchanged. Measured 6.7–7.2× compression on the Shopify cassettes (4.36 MB → 664 KB on `yunnan_sourcing_us`, 2.79 MB → 385 KB on `crimson_lotus`). The Shopify-quartet cassettes from steps 3–4 are grandfathered uncompressed (re-recording them without regenerating goldens would break the `test_golden_payloads_match_cassette` invariant — see §11 anti-pattern). Step-7+ cassettes record as `.yaml.gz`; the leak-audit grep runs against `zcat <name>.yaml.gz` per §9 procedure. `.gitattributes` declares `*.yaml.gz binary` to suppress diff + line-ending normalization. A round-trip unit test (`tests/unit/test_vcr_gzip.py`) asserts gzip-magic-bytes on save and byte-identical text content between `.yaml.gz` and `.yaml` paths.
- **[ADDRESSED V1.1] Shopify storefront bot mitigation (placeholder User-Agent + IP reputation).** Discovered 2026-05-16 during step-4 live `--all` smoke test: after ~30 min of sustained scraping from one IP (cassette recordings + smoke), `yunnan_sourcing_com` returned 403 on page 8, and `white2tea` + `crimson_lotus` returned 403 on page 1. `yunnan_sourcing_us` succeeded. The configured default User-Agent is the `.env.example` placeholder `'tea-rec-engine/0.1 (https://github.com/gary/tea; contact: gary@...)'` with a fake repo URL and a fake contact email — almost certainly part of what the storefronts' edge (likely Cloudflare in front of Shopify) is mitigating against. Note that **cassette-replay tests are unaffected** (100/100 pytest green), and the CLI's per-vendor failure-isolation path behaved correctly (`scrape_run` rows finalized cleanly as `failed` with `terminal: ScrapeError ... returned 403`). Recommended V1.1 fix: set `USER_AGENT` env to a real contact + real repo URL; consider lowering `rate_limit_rps` for these vendors from 2 to 1; cron the scraper so traffic is paced over hours instead of bursts. Out of V1 scope to add a cloudscraper / playwright fallback per §11 ("Don't add browser automation prematurely"). Operational details for V1.1 ops follow-up:
  - **`HttpClient` already treats 403 as terminal** (`src/tea_scrapers/http/client.py:116`) — the retry budget is correctly not burned on soft blocks, so a 403 immediately finalizes the `scrape_run` row as `failed`. No retry-loop hardening needed.
  - **Cool-down guidance.** A 403 from this class of mitigation typically clears in 5–60 minutes from the same IP. Cron the scraper at hourly cadence with per-vendor staggering rather than running `--all` back-to-back. Do not retry within the same process when a 403 hits; the next cron tick is the correct retry boundary.
  - **Detection & alerting.** Add a structlog counter / metric on `scrape.request` events with `status=403`. When the cumulative rate across any rolling 1-hour window exceeds (say) 5% of total requests for a vendor, page ops — that's the signal that mitigation has escalated beyond transient cool-down. `RunTracker` already finalizes per-vendor rows correctly, but there's no cross-run trend signal today.
  - **robots.txt position.** Not honored today. Shopify storefronts' `robots.txt` typically allows `/products.json` for low-rate crawlers but disallows aggressive paths (`/cart`, `/checkout`). Decide a V1.1 stance: opt-in honoring via `HttpClient`, or document explicitly that the project's scrapers are polite-but-not-robots-aware. Either way, the position should be in the spec, not implicit. Note (2026-05-16): Steepster's `/robots.txt` contains a substantial individual `Disallow: /<username>` block (hundreds of specific user paths, likely opt-outs). For V1 this is non-binding — §6.2 traverses `/teas/...` detail pages, not `/<username>/posts/...` user-post URLs. Revisit if/when V1.1 considers walking user-post URLs for richer tasting-note context. The Shopify and Steepster robots positions should be settled together as a single robots-honoring policy decision, not litigated per-source.
  - **Resolution landed 2026-05-16 (V1.1 ops fix).** Tech-lead audit this session confirmed the diagnosis: page-1 failures on `white2tea` / `crimson_lotus` rule out a rate-limit cause; placeholder UA + IP reputation is the load-bearing signal. What shipped: (1) `Settings.user_agent` and `Settings.reddit_user_agent` now field-validate against the well-known placeholder substrings (`github.com/gary/tea`, `contact: gary@`) — the project refuses to start when either UA still matches the class default; operator must override via `.env` (template updated at `tea-scrapers/.env.example`, real UA points at `https://github.com/gichigami/tea-db` + `gjohnson@pioneer-aero.com`). (2) `HttpClient` now emits a `WARNING`-level `scrape.request` log event with `terminal_block=True` and `status=<401|403>` before raising `ScrapeError` on auth / edge-mitigation responses — provides the structured signal for the cross-run trend aggregation flagged above. (3) `tea-scrapers/scripts/crontab.example` documents the recommended hourly per-vendor stagger (15-minute offsets) plus daily bronze + silver pipeline at 02:00 UTC; this is a template for ROADMAP step 10 to wire up, not a wired cron. (4) `rate_limit_rps: 2` per vendor is **unchanged** per the tech-lead audit — page-1 failures aren't a rate effect, so lowering to 1 would slow well-behaved vendors needlessly. **Still open:** (a) cross-run 403 aggregation / alerting (the structlog field is emitted but no aggregator consumes it yet — V1.2 dashboards); (b) the unified robots.txt policy decision called for in the previous bullet. Live re-scrape verification of the 3 previously-blocked vendors is an operator task post-merge: allow ≥1 hour cool-down from the last 403, then run `tea-scrape ingest shopify --vendor <key>` per vendor. **Verification result (2026-05-16, ~16h post-merge cooldown):** `white2tea` ✅ 1052 records / 3.4s / 0 terminal blocks; `crimson_lotus` ✅ 410 records / 2.5s / 0 terminal blocks; `yunnan_sourcing_com` ❌ 1000 records persisted then 403 on page 5 (`/products.json?limit=250&page=5`) with `terminal_block=True` at **30ms** response time. The 30ms response time is diagnostic: it's a precomputed CF rule firing as the request arrives, not a per-request decision (those take 50–200ms minimum), and pages 1–4 all passed clean in 1.0–1.4s each. Conclusion: V1.1 UA fix is **necessary but not sufficient** — `yunnan_sourcing_com` has additional stateful-within-CF bot mitigation that fires after a small burst of `/products.json` requests from one IP. Tracked as a V1.2 follow-up at step-7 follow-up #9 below (it's mitigation strategy, not a step-7 polish item, but landed in the same live-verification session). The other two vendors confirm the V1.1 fix removes the placeholder-UA detection path.
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
  5. **LLM tiebreaker stub at matcher step 3.** `CanonicalMatcher.match_or_create_product` currently logs `silver_match_ambiguity` and falls through to over-create when 2+ trigram candidates cluster within 0.10 similarity. The spec §8 calls for an LLM tiebreaker here; V1 stub is conservative (over-create is recoverable, false-merge is not). Cross-link: ROADMAP step 7+ where ml-engineer picks this up. **Update 2026-10-31:** now scoped under the §12 "Hybrid canonical matcher" entry (sub-question #4); the tiebreaker lands as step 5 of that plan.
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
  7. **AMBIGUOUS_GAP fall-through has no dedicated test.** `test_product_two_high_similarity_candidates_reuse_via_top_ceiling` exercises the `AMBIGUOUS_TOP_CEILING` reuse path (top_sim ≥ 0.95); no test currently exercises the `AMBIGUOUS_GAP` over-create path (top_sim in [0.85, 0.95) with second candidate within 0.10). Designing inputs that land in that band requires fiddly pg_trgm-similarity-aware text construction; defer to a polish PR. **Filled 2026-10-31 (H.2, data-engineer):** `test_product_ambiguous_gap_falls_through_to_over_create` (self-validating: asserts the seed similarities land in the band before calling the matcher). The top-ceiling test was also de-flaked — its hex-`uuid` suffix could share letter trigrams with the single-letter `A`/`B`/`C` words and push one seed under 0.85 (it was in fact ~0.86, not ≥0.95 as its docstring claimed); both now use digit-only tokens.
- **Steepster corpus scope expansion** (filed 2026-05-16, owner: tech-lead, target: V1.1). Sitemap reveals 41,165 tea URLs total; V1 fetches only those matching the §6.2 vendor allowlist (~7 vendors). At robots-compliant `Crawl-Delay: 10` a full corpus crawl is ~5 days wall-clock and produces ~30K tea pages whose producer/style coverage falls outside V1's catalog. Open question: when (and against what selection criterion) should V1.1 expand the Steepster crawl beyond the allowlist? Candidate criteria: (a) join-overlap with `producer` table once V.4 lands, (b) review-count thresholds on Steepster, (c) editorial pick of legendary/discontinued producers (design §5). Defer until V.4 join evidence is in hand.
- **Steepster producer-slug ↔ producer-identity mismatch** (filed 2026-05-16, owner: scraper-engineer + data-engineer, target: first Steepster silver-load PR). Steepster's `/companies/{slug}` URLs use vendor- or reseller-style slugs (e.g. `/companies/yunnan-sourcing` is the retailer, not an upstream producer). For producers like Menghai Tea Factory or Xiaguan that ship through multiple retailers, coverage under any single Steepster slug may be thin or fragmented across slugs (e.g. `/companies/menghai-tea-factory` vs `/companies/dayi` vs producer mentions inside Yunnan Sourcing-listed teas). Open question: should the §6.2 vendor allowlist be expanded for these producers based on first-crawl evidence, or should silver-layer producer normalization (alias table from step 6) handle the merge? Decide during the first allowlisted crawl when concrete numbers are available. This is a producer-identity question, not a scope-policy change.
- **Step-7 Steepster kickoff risks.** Filed 2026-05-16 by tech-lead, pending decisions before or during step-7 implementation. Each is independent; resolve in the appropriate PR.
  1. **Non-Shopify `vendor_external_id` scheme for Steepster** (owner: data-engineer + scraper-engineer). Silver normalizer's composite `vendor_external_id = "{product_id}:{variant_id}"` is Shopify-specific. What is Steepster's equivalent? Candidate: the numeric `tea_id` parsed from `/teas/{vendor-slug}/{tea-id}-{tea-slug}`. **Step-7 update (2026-05-16, scraper-engineer):** the scraper writes `external_id = <tea_id>` in `ingest_meta`, consistent with the candidate. Silver loader is data-engineer's call when step-7 silver-load gets specced; cross-link to silver normalizer follow-up #1.
  2. **Bronze record schema: reuse `raw_product_snapshot` with `source='steepster'`, or introduce `raw_review_snapshot`?** (owner: data-engineer). Steepster's natural unit is "tea + inlined review list" (not "product snapshot"). Reusing `raw_product_snapshot` conflates two semantic shapes under one bronze table; splitting introduces a second bronze loader path. **Step-7 status (2026-05-16):** still open. The Steepster JSONL output is shape-compatible with the existing bronze loader (one record per `external_id` per `source`, with `payload` as a self-describing dict), so the loader writes Steepster rows into `raw_product_snapshot` with `source='steepster'` today. That's a load-time choice, reversible if the data-engineer decides to split. Cross-link to OQ on producer-slug mismatch above.
  3. **Steepster→`product` join strategy** (owner: data-engineer + ml-engineer). Per design §5 / ROADMAP V.4, the join from Steepster review data into the catalog is at producer + style level, not 1:1 product match. Pin that step 7 does NOT attempt `vendor_product` fan-out from Steepster — bronze captures the records, and the join lands later as part of V.4 work. **Step-7 status (2026-05-16):** honored. Scraper writes JSONL only; no silver fan-out attempted.
  4. **Tier system applicability to reviews** (owner: tech-lead). A/B/C/D is a product-availability concept (design §3); reviews have no availability. Either reviews bypass `tier` entirely (recommended default; review records are not tier-eligible) or "tier" gains a second meaning (overload, not recommended). Decide before Steepster silver-load. **Step-7 status (2026-05-16):** still open. Scraper-side neutral — the JSONL has no tier field.
  5. **Author hashing specifics** (owner: scraper-engineer). ~~§6.2 says "hash author names" — pin: SHA-256, unsalted, NFC-normalized lowercased input. Unsalted is intentional so the hash is reproducible across runs to support returning-reviewer dedup downstream.~~ **Resolved 2026-05-16 (scraper-engineer):** SHA-256, unsalted, NFC + lowercase + strip, prefixed `sha256:`. Implementation in `tea_scrapers.sources.steepster.hash_author`; unit tests in `tests/unit/test_steepster_scraper.py::TestHashAuthor` pin stability + NFC + case-insensitivity. The `sha256:` prefix is mandatory so a future schema migration to a salted scheme can be distinguished without touching historical rows. §6.2 above now documents the algorithm as part of the schema.
  6. **HTML pagination terminator semantics** (owner: scraper-engineer). ~~Shopify gave a deterministic "empty array = end of pages." Steepster HTML pagination is uncertain (last-page link missing? "next" button disabled? 404 on overshoot? notes inline-on-one-page vs paginated?). The Rishi Tea probe page (510 KB) showed no `?page=` markers in the body, suggesting at least some pages inline all notes — but the `--max-time 19s` curl cap may have curtailed the response, so verify in step-7 code. Decide the terminator condition in the step-7 PR with at least one positive (paginated) and one negative (single-page) cassette.~~ **Resolved 2026-05-16 (scraper-engineer):** Verified against real `crimson-lotus-tea` company page + `crimson-lotus-tea/{tea-id}-...` detail pages. Terminator pinned in §6.2 above. Both the company-index walk and the tea-detail notes walk check the same two signals: zero in-scope items found OR no "Next + `?page=`" link. Unit tests `tests/unit/test_steepster_scraper.py::TestVendorPageEnumeration::test_pagination_terminates_when_no_next_link` and `test_note_pagination_followed` cover the positive (paginated) and negative (single-page) cases respectively. The recorded VCR cassette `tests/fixtures/cassettes/steepster_crimson_lotus_tea.yaml.gz` exercises both terminators on real markup.

- **Step-7 follow-ups (filed 2026-05-16, owner per item; non-blocking).** Filed during the step-7 Steepster scraper landing PR.
  1. **`STEEPSTER_RATE_LIMIT_RPS` env override consumes a Settings-shaped env var without going through `Settings`.** The override is read directly via `os.environ.get` in `load_steepster_config`. It works, but a future refactor that consolidates env-driven config into `Settings` will want to lift this knob over so the precedence order is consistent across all env-vars. Non-urgent; only one consumer today (integration tests).
  2. **`max_teas_per_vendor` is scraper-side only, not in `SteepsterConfig`.** The CLI plumbs `--max-teas` to `SteepsterScraper(..., max_teas_per_vendor=N)`; the YAML has no `max_teas_per_vendor` knob. If V1.1 ops wants a per-vendor cap (e.g. "scrape 500 teas per vendor per nightly run; the full backwalk is the weekend cron"), lift the cap into `SteepsterConfig`.
  3. **Author-name leak audit is structural, not substring.** `test_no_plaintext_author_field_in_notes` asserts the per-note schema has `author_hash` and no plaintext-author field under a fixed forbidden-key set. It does NOT scan the JSONL for verbatim username substrings, because reviewers legitimately mention each other by name in note body text (and stripping that would violate §11 "capture every record verbatim"). If a future refactor stops emitting `author_hash` in favor of a different key (e.g. `reviewer_hash`), this test's forbidden-key list would silently miss the regression. Cross-link: when an LLM extraction layer for tasting notes lands (V.3), revisit whether body text needs PII redaction — that's a separate concern from the scraper-side hash.
  4. **Company-index sitemap fallback not implemented.** §6.2 calls sitemap-based enumeration "preferred" with company-index pagination as a fallback. The V1 implementation does the opposite: it walks the company index directly. Rationale: a single-vendor crawl with a 7-slug allowlist is bounded (the largest vendor, Yunnan Sourcing, is ~3,700 teas across 367 pages = ~3,700 detail fetches either way), and walking the company index avoids the 9.3MB `sitemap1.xml` download for every run. Revisit if V1.1 expands the allowlist past ~20 vendors or removes it entirely.
  5. **`crimson-lotus-tea` cassette is 8.3 MB compressed (48 MB uncompressed).** Larger than any Shopify cassette (the biggest is ~5 MB). It's still well below the 100 MB threshold flagged in the original §12 cassette-size trajectory item, and gzip is doing 5.8× compression, but Steepster (and TeaDB, Reddit ahead) will continue this growth curve. If the cumulative `tests/fixtures/cassettes/` directory crosses ~50 MB on disk, revisit Option 3 from the original cassette-size resolution (per-source sampling policy with cassettes in object storage).
  6. **`max_teas_per_vendor` slice happens after the full company-index walk.** `SteepsterScraper._collect_tea_urls` paginates the entire company index for a vendor before the caller slices to `max_teas_per_vendor` in `scrape_vendor` (`sources/steepster.py:164-165`). The crimson-lotus-tea cassette therefore walked all 29 company-index pages even though only 10 detail fetches followed — and the cassette size scales with the company catalog, not with `--max-teas`. A future re-record against a larger vendor (e.g. yunnan-sourcing at 367 company pages) would produce a ~12× larger cassette before any trimming. Fix: terminate `_collect_tea_urls` early once `len(seen) >= max_teas_per_vendor`. Cheap; defer to a polish PR. Cross-link to follow-up #5 above (cassette size trajectory). **Empirically validated 2026-05-16 (live re-verification):** a `--max-teas 3` run against crimson-lotus-tea took ~29 min wall-clock, of which ~10 min was the 29-page company walk to find 3 teas; the early-terminate fix would have cut that to ~30s. Worth implementing before any larger `--max-teas` operator-debug runs.
  7. **`.env.example` ships `DATABASE_URL` commented out.** First-time operators copy `.env.example → .env` and immediately hit `fe_sendauth: no password supplied` because the `Settings` default (`postgresql+psycopg://localhost/tea`) has no credentials and the docker-compose container expects `postgres:postgres`. The example currently shows `# DATABASE_URL=postgresql+psycopg://localhost/tea` (commented out, no creds). Fix: ship the example with `DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/tea` uncommented to match `docker-compose.yml`, and add a comment noting that production deployments should override. Owner: data-engineer or scraper-engineer at next-touch. Filed 2026-05-16 during live re-verification of step 7.
  8. **HttpClient retry backoff may be tuned too patient for Steepster's 504 pattern.** Live re-verification observed 9 × 504 + 1 × 502 across 48 HTTP requests against Steepster. Each retry incurred ~2 min of backoff before the successful re-attempt — but Steepster's 504s clear in seconds when the origin surrogate cache warms, so a 5–10s backoff would have absorbed them at ~1/12 the wall-clock cost. The bulk seed (estimated ~12–15 days continuous against the V1 allowlist per the live-run extrapolation) would benefit substantially from a per-source backoff override — Steepster needs short, eager retries against its render-flaky origin; Shopify's `/products.json` rarely 5xxs at all. Investigation: audit `http/client.py` backoff schedule; consider a `VendorConfig.retry_backoff_max_s` override exposed through `vendors.yaml`. Owner: scraper-engineer before the Steepster bulk seed. Filed 2026-05-16 during live re-verification of step 7.
  9. **`yunnan_sourcing_com` post-V1.1 bot mitigation: 4-page CF pagination quota.** Filed 2026-05-16 during live re-verification of the V1.1 fix (see V1.1 resolution paragraph above for the verification table). Pages 1–4 of `/products.json?limit=250&page=N` pass clean (1.0–1.4s each); page 5 returns 403 in **30 ms** with `terminal_block=True`. The 30ms timing is diagnostic of a stateful-within-CF block (precomputed-rule match, not request-content inspection). Burst-then-block pattern; the other 3 Shopify vendors (`yunnan_sourcing_us`, `white2tea`, `crimson_lotus`) are not affected — same IP, same UA, same `httpx` client, just different CF deployments. Coverage impact is real: `yunnan_sourcing_com` is the **biggest shoppable surface** in the V1 allowlist (estimated ~3,700+ SKUs vs ~1,052 white2tea / ~410 crimson_lotus / `yunnan_sourcing_us` is in the same brand but smaller US-stock subset). Owner: scraper-engineer (with possible escalation to ml-engineer if Playwright fallback ultimately needed). Target: V1.2. Investigation strategy below — pick by cost/probability:
     1. **Reproduce-and-characterize first** (~30 min, before any code change). After ≥1h cooldown: (a) re-run with `limit=50` to test whether the threshold is page-count-based or product-count-based, (b) re-run with `rate_limit_rps: 0.5` to test whether spacing helps, (c) re-run from a different IP (cellular tether, VPN exit, or `curl` from a cloud box) to test whether the block is IP-attached vs. fingerprint-attached, (d) `curl -I` page 5 directly to see what CF headers come back on the 403 (`cf-ray`, `cf-mitigated`, `server-timing`). This evidence narrows options 2–5 below from "guess" to "informed pick."
     2. **`?since_id=` cursor pagination** (~1–2h code). Shopify supports cursor-based pagination via `?since_id=<last_product_id>` instead of `?page=N`. Anecdotally friendlier to some CF rules because the URL pattern looks more like a "deep link" than an "enumeration sweep." Cheap to implement in `sources/shopify.py` as an alternative iterator behind a per-vendor flag. Worth trying after characterizing (1).
     3. **Per-vendor `request_burst_cap` + resumable partial crawl** (~3–5h code). Cap each run at N pages (e.g. 4), persist the resume cursor (`last_product_id` from final page) in `scrape_run.metadata`, schedule multiple runs across a 24h cron window. The streaming `JsonlWriter` already preserves partial progress; the bronze loader's `payload_hash` dedup handles re-runs idempotently. Pairs naturally with (2).
     4. **`cloudscraper` library fallback** (~1 day code; new dep). Drop-in for `httpx` that solves CF JS challenges. §6.2 already documents this as the Steepster fallback path — would extend to `yunnan_sourcing_com` if (2) + (3) don't suffice. Adds dep weight; defer until necessary.
     5. **Reach out to Yunnan Sourcing for API access / scraping permission** (days–weeks, cross-cutting). Best long-term answer — formal cooperation removes the entire CF-mitigation problem. Pursue in parallel with technical work; doesn't block (1)–(4).
     **Original recommended sequence (pre-Phase-1):** (1) → (2) if cursor pagination plausible from headers → (3) if (2) insufficient → (4) only if (2)+(3) fail. (5) runs in parallel regardless. Do NOT skip to (4) — `§11` anti-pattern explicitly forbids jumping to browser automation without evidence it's needed.

     **Phase 1 execution + revised diagnosis (2026-05-16 evening, same session):** Sub-option 1d (`curl -I` HEAD probes from this IP) returned 200/200 on pages 1 and 5 ~7 min after the original 403, with no `cf-mitigated` / `retry-after` / challenge headers. That apparent "block cleared in minutes" outcome led to a too-confident "burst-rate sliding-window limit" conclusion and a Phase 2 retry at `rate_limit_rps: 0.5` (vendors.yaml YS .com override). **The retry failed again — 403 on page 1 this time at 103 ms.** A follow-up curl GET from the same IP within the same minute window then returned 200. Same IP, same UA, different result by HTTP client → the block is **not rate-based, not IP-based, not URL-path-based; it is request-signature-based (JA3 TLS fingerprint + HTTP version differential)**. Two response-time signals corroborate: 30 ms (initial 403, cached fingerprint hash) vs 103 ms (later 403, fresh fingerprint inspection + blocklist lookup) — both characteristic of CF active-mitigation modes against bot signatures. curl uses HTTP/2 + OpenSSL system-default TLS handshake (matches browser JA3s); `httpx` defaults to HTTP/1.1 + Python `ssl` module defaults (well-flagged Python-client JA3). The burst-rate event from the initial scrape almost certainly **promoted the (IP × Python-httpx fingerprint) tuple onto an active blocklist with a multi-minute sticky window**; during that window, CF aggressively blocks the fingerprint while letting curl-like signatures pass.

     **Revised mitigation options (post-Phase-1):**
     - **2a. `httpx(http2=True)` + `h2` dep** (~30 min code; new dep `h2`). Cheapest possible test. Brings httpx's HTTP version into alignment with curl. Does NOT fix the JA3 TLS fingerprint — may still 403 if the TLS hash alone is on the blocklist — but eliminates one of two confounders. Try first because the cost is trivial.
     - **2b. `curl-cffi` library** (~1–2 day code; new dep `curl-cffi`). Drop-in for `httpx` for most use cases; wraps libcurl and impersonates Chrome's TLS fingerprint (`impersonate="chrome120"` etc.). This is the standard scraping-community solution to JA3-based CF mitigation. High probability of fixing this specific issue at one library swap. Strongly preferred over playwright / cloudscraper for this problem class — curl-cffi is a single dep, no headless browser, no JS engine.
     - **2c. Per-source HTTP-client switch** (~1–2 day code, on top of 2b). Don't migrate the entire `HttpClient` to curl-cffi; expose a `VendorConfig.http_client = "httpx" | "curl_cffi"` knob so YS .com gets curl-cffi while the other 3 Shopify vendors + Steepster keep the lighter-weight httpx path. This isolates the fingerprint hack to where it's needed and avoids paying the curl-cffi cost (slightly heavier, native libcurl dep) for vendors that don't need it.
     - **3. Cursor pagination / partial-crawl options from the original plan are deprecated** — the evidence now says the block isn't pagination-pattern-based, so changing URL shape won't help.
     - **4. cloudscraper is still deprecated** — it solves JS challenges, not JA3 fingerprints; wrong tool.
     - **5. Vendor outreach** unchanged — still worth pursuing in parallel.

     **Revised recommended sequence:** (2a) for ~30-min cheap test → (2b) + (2c) if 2a fails (high probability). Skip (3) and (4) entirely unless 2a+2b+2c all fail (very unlikely). (5) parallel always.

     **Vendors.yaml state (2026-05-16 end of session):** `yunnan_sourcing_com.rate_limit_rps` reverted to `2` from the failed `0.5` experiment, with an inline `# KNOWN ISSUE` comment pointing back to this follow-up. The rate setting is correct as-is for V1; the fix lives in the HTTP-client layer, not the rate-limit layer.

- **Hybrid canonical matcher (trigram → trigram + embedding + attribute guard)** (filed 2026-10-31 by tech-lead, owner of decision: project owner, **status: RESOLVED — Option B approved**; sub-questions below remain OPEN). Supersedes the "trigram only" reading of §8 "Canonical product ID matching"; §8 now describes trigram as the V1 baseline and this as the approved evolution.

  **Decision.** Move both `match_or_create_product` and `match_or_create_producer` to a hybrid matcher. Target flow:
  1. Exact match (unchanged from V1).
  2. **Attribute guard** — parse recipe code (e.g. Dayi `7262` / `7572`), sheng vs shou, harvest year, and weight from both sides; any parsed-and-mismatched attribute vetoes the pair regardless of downstream score. Treatment of attributes present on one side only is open (sub-question #7).
  3. **Candidate generation** — union of trigram candidates (existing `%` + `set_limit` path) and embedding kNN over `name_embedding`.
  4. **Hybrid score** with an auto-match threshold and a review band, both calibrated on a hand-labeled pair set.
  5. **LLM tiebreaker** for the ambiguous band only.
  6. Otherwise create.

  Embedding model: **local BGE-M3** (MIT license; dense + sparse heads; strong Chinese coverage), stored as `halfvec(1024)` in a **new `name_embedding` table** keyed by entity with `model`, `revision`, and `text_hash` columns so re-embedding is cache-addressable and model upgrades are detectable. This is deliberately separate from the description-level `product_embedding` table. Rollout is shadow-mode behind a matcher-strategy config flag (`trigram | hybrid`, default `trigram`); flip the default only if hybrid beats the trigram baseline P/R on the labeled set. **Trigram is not removed.**

  **Rationale.**
  - Trigram fails on cross-script and romanization variants (大红袍 / Da Hong Pao; Laobanzhang / Lao Ban Zhang), which matter most for the producer matcher and for the historical → current pivot (design §5, §6 #7) — a false non-match there splits a legendary tea from its current listing.
  - Dense embeddings collapse exactly the tokens that distinguish tea products: recipe codes (`7262` vs `7572`), years, and weights embed near-identically. Hence the attribute guard is **mandatory**, not an optimization; the hybrid path must not ship without it.
  - Merges are irreversible downstream (snapshot chains, reviews, and quote-evidence attach to the merged `product_id`); over-creates are recoverable. Precision-first: target ≥ ~0.98 precision on auto-match, everything below goes to the review band / tiebreaker / create. Same conservatism as the V1 over-create stub (silver follow-up #5).
  - The product trigram step already blocks on producer + year + weight (`IS NOT DISTINCT FROM`), so the guard generalizes an existing invariant rather than inventing one. The producer matcher has no ambiguity-gap check today; the hybrid path adds one.

  **Phased steps** (tracked in `ROADMAP.md` under "Hybrid canonical matcher"):
  0. tech-lead — this OQ + §8 amendment.
  1. qa-engineer — hand-labeled producer + product pair set (agent-proposed labels, owner-reviewed) + trigram baseline precision/recall.
  2. data-engineer — attribute-guard parser + fix for `":: Seattle Inventory"`-style suffix near-duplicates.
  3. ml-engineer — embedder module: `Embedder` protocol, cache keyed by `(model, revision, text_hash)`, deterministic fake embedder for tests; new-dependency justification per §11 (`sentence-transformers` + `torch` vs `FlagEmbedding`).
  4. data-engineer — migration `003` (`name_embedding`) + matcher-strategy config flag.
  5. ml-engineer + qa-engineer — threshold calibration (auto-match ≥ ~0.98 precision + review band).
  6. data-engineer — shadow-mode run on the bulk seed; log trigram-vs-hybrid disagreements for owner review.
  7. qa-engineer + code-reviewer — tests (fixture vectors, no network, no model download in CI) + review.

  **Still-open sub-questions** (not resolved by the Option B decision; resolve in the step noted):
  1. **Exact scan vs HNSW for name kNN** (owner: data-engineer, step 4). At V1 scale (low thousands of products, hundreds of producers) an exact `ORDER BY embedding <=> :q` scan, especially pre-filtered by producer, may beat HNSW on both recall and simplicity. Decide on measured latency; don't build the index by reflex.
  2. **Hybrid scoring formula** (owner: ml-engineer, step 5). Weighted linear blend of trigram similarity + cosine vs reciprocal rank fusion (RRF). RRF is threshold-hostile (rank-based scores don't calibrate to a precision target); a weighted blend is easier to calibrate but needs per-signal normalization. Pick on the labeled set.
  3. **Does BGE-M3 sparse eventually replace trigram?** (owner: ml-engineer + tech-lead, post-step-6). Not in this decision — trigram stays as baseline and candidate generator. Revisit only with shadow-mode evidence.
  4. **LLM tiebreaker scope** (owner: ml-engineer, step 5). Cross-link silver normalizer follow-up #5: the existing stub over-creates. Decide whether the tiebreaker sees only the review band, whether it may override an attribute-guard veto (recommended: never), and which provider (ties to the design §10 / ROADMAP "LLM provider for extraction" OQ).
  5. **Two embedding stacks** (owner: tech-lead + ml-engineer, before V.6). Design §10 "Embedding model: API vs local" leans API (`text-embedding-3-large`) for **description** embeddings; this decision puts **name** embeddings on local BGE-M3. Two stacks is acceptable if justified (names are short, multilingual, high-volume re-embeds; descriptions feed the recommender), but the design §10 OQ must be answered knowing BGE-M3 is already in the dependency set.
  6. **`product_embedding` dimension/comment mismatch** (owner: data-engineer + ml-engineer, before V.6). `alembic/versions/001_initial.py:254-268` and `storage/models.py:233-245` (and the §8 DDL above) declare `VECTOR(1536)` with the comment "text-embedding-3-large dim", but `text-embedding-3-large` defaults to 3072 dims (1536 is `text-embedding-3-small`'s default, or 3-large truncated via the `dimensions` parameter). Also note pgvector's HNSW limit is 2000 dims for `vector` (4000 for `halfvec`), so 3072 un-truncated cannot be HNSW-indexed as `vector`. Correct the comment or the dimension when V.6 picks the description model; not touched by migration 003.
  7. **Attribute guard: one-sided attributes** (owner: data-engineer, step 2; calibrated in step 5). The V1 trigram step blocks with `IS NOT DISTINCT FROM`, so a NULL `harvest_year` / `weight_grams` on one side never matches a populated one. The guard must decide whether "parsed on one side, absent on the other" vetoes (V1-equivalent, precision-first) or passes to scoring (recall-friendlier for vendors whose titles omit year/weight — white2tea, Crimson Lotus per silver follow-up #4). Default to veto until the labeled set shows the recall cost. **Provisional decision (2026-10-31, data-engineer, H.2; pending H.1 labeled-set evidence — NOT resolved):** `ONE_SIDED_POLICY` in `normalize/attributes.py` sets **all four** attributes (`harvest_year`, `weight_grams`, `recipe_code`, `style`) to `VETO`, i.e. V1-`IS NOT DISTINCT FROM`-equivalent; flipping one to `PASS` is a one-line change and is covered by a test. Evidence so far (cassettes only, not labeled): over all 2,462 Shopify cassette titles, same-vendor + same-year pairs with suffix-stripped trigram ≥ 0.85 number 193 — 87 are identical after stripping `:: Seattle Inventory`, 101 pass the guard, **5 are real sheng/shou mismatches** that the pre-H.2 trigram step would merge (e.g. YS "Bang Wai Mountain" / "Impression" / "Lao Man'e Village" Raw vs Ripe, "Cha Gao" Raw vs Ripe resin), and **0 are one-sided** for recipe code or style — so one-sided veto costs no measured recall within-vendor today. The recall risk is cross-vendor (the hybrid kNN path, where producer blocking is weaker); H.5 should re-measure on the labeled set. Guard as shipped in the trigram path: recipe code + sheng/shou parsed from the **name** (YS `product_type` is not consulted); year + weight compared from the mapper fields / product columns (already SQL-blocked in trigram; the guard re-checks them so the hybrid path can reuse it). `MIXED` (both markers, e.g. combo packs, YS "Ripe Raw" titles) is a distinct style value. All-candidates-vetoed → `ProductDecision.VETO_CREATED`; per-candidate vetoes are logged (`silver_match_guard_veto`) and counted (`NormalizeStats.guard_vetoes` / `guard_vetoes_by_reason`, per-source `guard_vetoes`). Vetoes do **not** affect the normalize exit code (a veto is a confident non-match, unlike `ambiguous_created`). **Review follow-ups landed 2026-10-31:** `#7542` / `No.7542` now parse as codes; elevations (`1800m`, `2400 meters`, `ft`, `masl`) and ages (`1000 Year Old`, `yrs`) are not codes; `Fu Lu Shou` / `Shou Xing` / `Wan Shou` / `Sheng Xiao` / 生肖 / 生日 are not style markers; the shared suffix regex uses an explicit ASCII-whitespace + NBSP class (Postgres `\s` under `C.UTF-8` does not match NBSP; Python's does); `suffix_exact` is emitted only when a suffix is present on either side (whitespace-only differences are `exact`). **H.1-driven follow-up (2026-11-01, data-engineer):** the H.1 labeled set showed 10 `hard_negative_year` false merges where the year is not at title start (CL `"Spring 2017 X"` vs `"Spring 2018 X"`, `"Elemental Puerh - 2019 …"`, YS `"Sampler for 2023 …"`): the mapper leaves `harvest_year` NULL on both, so the SQL block and the guard both passed. `GuardAttributes.from_name` now falls back to the name-parsed vintage year (first standalone 4-digit token in [1900, 2099] that is not a recipe code / quantity) **only when the mapper value is NULL**; the mapper and the stored column are unchanged (its start-of-title anchor is a deliberate step-6 rule, not a bug). New fifth attribute `item_number` (`ONE_SIDED_POLICY` `VETO`; 0 labeled positives carry one on one side only) catches 10 more `hard_negative_recipe_code` merges (`Pt.1`/`Pt.2`, `Part 1`/`Part 2` samplers, teaware `#03`/`#04`, `Grade 2`/`3`, `BP-12`/`BP-13`, `ZH03`/`ZH95`, `KA39` vs none). H.1 harness `--product-policy h2` @0.85: P 0.650 → 0.867, R 0.336 unchanged (FP 28 → 8). Remaining 8 FPs are not guard-shaped: bare-number teapot models (`"Jiao Ni 66"`/`71`) and name variants (`"Autumn X"` vs `"X"` ×5 — a one-sided harvest-season attribute would catch these but risks cross-vendor recall; `Gold`/`Red Label`; `Impression`/`Lincang Impression`) — left for the H.5 scorer.
     **H.2b one-sided year / weight experiment (2026-11-04, data-engineer; default UNCHANGED = veto).** Harness variants on top of the shipped (a2)+(b) matcher (`matcher_variants.py`; pairwise, so relaxed-weight recall is an *upper bound* — in corpus a weightless record meets one row per weight variant with identical names and the AMBIGUOUS step over-creates):

     | one-sided policy | product P / R @0.85 | R@P≥.98 (t) | Δ TP (where) | new FP |
     |---|---|---|---|---|
     | veto (default) | 0.889 / 0.413 | 0.406 (0.92) | — | — |
     | year: block on *effective* year (mapper, else name-parsed), one-sided still vetoes | 0.889 / 0.413 | 0.406 | 0 | 0 |
     | year = pass | 0.895 / 0.439 | 0.432 | +4 cross-vendor | 0 |
     | weight = pass | 0.911 / 0.529 | 0.516 | +12 `:: Seattle Inventory` twins, +5 cross-vendor, +1 dup | 0 |
     | weight = pass only when the weightless side is Steepster | 0.896 / 0.445 | 0.432 | +5 Steepster↔CL | 0 |
     | weight = pass, **exact name only** | 0.910 / 0.523 | 0.516 | +12 twins, +1 dup | 0 |
     | year + weight = pass | 0.915 / 0.555 | 0.542 | +18 | 0 |
     | year + weight = pass, exact only / strict 0.95 | 0.914 / 0.548 | 0.542 | +17 | 0 |

     No variant adds a false positive on this set, but no variant reaches P ≥ 0.98 at 0.85 either (the pre-existing name-variant FPs set that), and the biggest, cleanest gain — the 12 Seattle twins whose variant carries no parseable weight — rests on **low-confidence** labels. Implemented as `canonical.ONE_SIDED_BLOCKING` (`harvest_year` / `weight_grams`: `"veto"` | `"exact_only"`; per-matcher override `CanonicalMatcher(one_sided_blocking=…)`): `exact_only` reuses a one-sided row only on a (skeleton-)exact name that is the **unique** such row in the relaxed block and passes the guard (`one_sided_exact` decision), never via trigram, never across a both-known mismatch. **Default stays `veto` for both.** Recommendation for the owner: after confirming the 12 suffix-twin labels, flip `weight_grams` to `exact_only` (one line; tests cover both settings); leave `harvest_year` at `veto` (+4 pairs is noise-level). Score-based relaxations (`pass`, `pass` + strict) are harness-only and not recommended.
     **Is relaxing needed for V1's historical → current pivot? No.** Of the 46 Steepster↔Crimson Lotus positives, even year + weight = pass recovers only ~5–9: the rest are blocked by the one-sided *style* veto (Steepster titles omit "Sheng / Raw") and by low trigram (`"2016 Moon Princess"` vs `"2016 "Moon Princess" Sheng / Raw Puerh from Crimson Lotus Tea"` = 0.34). Steepster names are short containments of the Shopify title, which neither one-sided relaxation nor trigram addresses — and step-7 OQ #3 already scopes the Steepster join to **producer + style** (V.4), not 1:1 `product` matching. Relaxing the product gate for Steepster would add merge risk for a join V1 does not make; if V.4 later wants tea-level links, a token-containment scorer inside the producer + year block is the cheaper lever.
  8. **`product.canonical_name` is order-dependent under suffix matching** (filed 2026-10-31 by data-engineer from H.2 code review; owner: data-engineer + tech-lead; **decide before H.3 and before the bulk seed**). The suffix-stripped matching key joins a `":: Seattle Inventory"` re-listing to its base product, but `canonical_name` is whatever title created the row. If the Seattle listing is normalized first, the suffix leaks into `canonical_name` — visible in the UI tea card and, more importantly, in the text H.3 embeds into `name_embedding` (the suffix would dilute name vectors and break `text_hash` stability across re-normalizes). Options: (a) store `strip_inventory_suffix(title)` as `canonical_name` on create (raw title still in bronze + could move to a `vendor_product.vendor_title` column for quote-evidence); (b) keep raw `canonical_name` and embed / display a derived `matching_key`; (c) on suffix-exact reuse, rewrite a suffixed `canonical_name` to the base title. Not fixed in H.2 by design (the brief preserved the raw title where it lives today).
     **H.3 position (2026-11-01, ml-engineer):** the embedder never embeds `canonical_name`. `tea_scrapers.embed.text.product_embedding_text` = `matching_key(name)` (suffix-stripped, NFC, lower-cased, whitespace-collapsed); producers embed the producer-matcher normalization. So "exact match" ⇔ "same `text_hash`", and the vector does not depend on normalize order whichever of (a)/(b)/(c) H.4 picks for display. Lower-casing was checked against the model: cosine moves ≤ 0.04 on the cross-script probes (e.g. `Jin Jia Qi`/`jin jia qi` vs `金家棋`: 0.369 / 0.368).
  9. **Cross-script names need transliteration, not embeddings** (filed 2026-11-01 by ml-engineer from H.3 preliminary eval; owner: ml-engineer + data-engineer, decide in step 5). BGE-M3 dense does **not** bridge romanized ↔ hanzi short proper nouns: `Da Hong Pao`/`大红袍` 0.345, `Jin Jia Qi`/`金家棋` 0.369, `Da Yi`/`大益` 0.382, `Xiaguan Tea Factory`/`下关茶厂` 0.54 — the same band as unrelated producer pairs (easy negatives 0.37–0.65), while hard negatives (`Menghai Tea Factory` / `Menghai Tian Di Ren Tea Factory`) score 0.84. BGE-M3 sparse is 0.00 on every producer cross-script match pair (no shared tokens). The rationale bullet above ("trigram fails on cross-script… 大红袍 / Da Hong Pao") still holds, but embeddings are not the fix for it. Candidates: (a) hanzi → toneless pinyin transliteration (e.g. `pypinyin`, MIT, pure-Python; would be a new dep, §11) applied to the matching key before trigram / embedding, so `大红袍` → `da hong pao`; (b) curated producer alias table (owner-reviewed) for the ~dozen big factories; (c) route cross-script candidates to the LLM tiebreaker (#4). Recommendation: (a)+(b) for producers, measured on the labeled set before H.5 commits thresholds.
     **H.2b result (2026-11-04, data-engineer; owner chose "Option A — cheap fixes first").** (a) and (b) implemented as independent toggles and measured on the H.1 harness (`python -m tea_scrapers.eval.matcher_variants --aliases config/producer_aliases.yaml`; labels still agent-proposed — small deltas are noise). "R@P≥.98" = max recall over t ∈ {0.00…1.00} with P ≥ 0.98 (same definition as the H.3 table).

     | variant (all on the live H.2 gate) | product P / R @0.85 | product R@P≥.98 | producer P / R @0.85 | producer R@P≥.98 |
     |---|---|---|---|---|
     | baseline | 0.867 / 0.335 | 0.310 | 1.000 / 0.059 | 0.157 |
     | (a1) trigram on pinyin key | 0.871 / 0.348 | 0.323 | 1.000 / 0.098 | 0.196 |
     | (a2) + skeleton exact | 0.889 / 0.413 | 0.406 | 1.000 / 0.157 | 0.255 |
     | (a3) + collapsed-form trigram | 0.889 / 0.413 | 0.406 | 1.000 / 0.157 | 0.255 |
     | (b) curated aliases only | 0.867 / 0.335 | 0.310 | 1.000 / 0.745 | 0.745 |
     | (a2)+(b) **shipped default** | 0.889 / 0.413 | 0.406 | 1.000 / 0.765 | 0.765 |

     Per category (TP/pos @0.85, baseline → shipped): product `cross_script` 9/44 → 21/44 (romanization/spacing 100%-ish; hanzi titles mostly still miss — `生普洱`→"sheng pu er" vs "Raw Pu-erh" is *translation*, not transliteration; 3 `translation_not_transliteration` pairs are out of reach by design); producer `producer_alias` 3/37 → 28/37, `cross_script` 0/14 → 11/14. **Zero new false positives in any category**; the remaining product FPs (7 `hard_negative_name_variant`, 1 recipe) are the pre-existing H.2 ones. Collapsed (space-removed) trigram adds nothing — pg_trgm splits on non-alphanumerics, so the collapsed form only matters for *exact*, where the skeleton already covers it — **not shipped**. Exact on "both spaced and collapsed forms" reduces to skeleton equality (spaced-equal ⇒ skeleton-equal), so only the skeleton is compared.
     What shipped (`normalize/translit.py`, `normalize/canonical.py`, `TRANSLITERATE_MATCHING_KEYS = True`, per-matcher `CanonicalMatcher(transliterate=False)` override): producer step 1 looks up `aliases && {norm, pinyin_key}`; a hanzi producer is created with its pinyin key as an extra alias (`金家棋` → also `jin jia qi`); new step 2.5 matches the skeleton against every stored alias / canonical name in SQL (`regexp_replace(…, '[^a-z0-9]+', '', 'g')`, pattern shared with Python) and reuses **only on a unique owner** (`producer.skeleton_ambiguous` logged otherwise); trigram queries a hanzi name by its pinyin key. Product: new step 1.5 `skeleton_exact` scans the producer / year / weight block in Python (pinyin cannot run in SQL; skeletons cached per `product_id`) and, unlike step 1, **runs the attribute guard** on the hit; trigram queries a hanzi title by its pinyin key. Raw titles / `canonical_name` untouched. Curated aliases (`normalize/producer_aliases.py`): seeded into the **existing `producer.aliases` column** at `tea-scrape normalize` start (`--producer-aliases`, default `config/producer_aliases.yaml`, skipped with a log line if absent; a malformed file exits 2 before the run starts), idempotent union, never renames a canonical, **never merges** a pre-existing split (seeds the canonical-named row, skips keys another row holds, reports `producer_alias_seed.conflict` on the run). The curated file (33 producers, ontology-curator) loads with 0 conflicts and 0 ambiguous skeletons; seeding the dev DB twice: 4 inserted / 29 unioned, then 33 unchanged.
     **Left for the owner (label vs. curation disagreements, not matcher bugs):** the 12 remaining producer misses are almost all the curator's deliberate *bare-toponym* rule (`Xiaguan`, `Liming`, `Nan Qiao`, `Mengku`, `Lao Man'e`, `下关`, `老曼峨` are never aliases because they appear in titles as places) — the H.1 labels call them matches; plus `CNNP` ↔ `Zhong Cha` / `中茶` / the full CNNP name, `Fei Tai` ↔ `FT`, `Yunnan Sourcing Tea Shop` ↔ `Yunnan Sourcing USA` (low). One side has to move. **Known limits:** pypinyin polyphone choice on rare proper nouns (recall miss); pinyin is lossy (`西安` / `先` → `xian`; guarded by unique-owner / attribute guard); the skeleton drops accented Latin letters rather than folding them (keeps SQL and Python byte-identical).
     **Mapper issues flagged by ontology-curator (noted, not fixed; owner: data-engineer, before the bulk seed):** (i) the producer fallback creates a single catch-all `"Other"` producer (with `"Other Factories"` as a separate bucket) — placeholder buckets then block on `producer_id` as if they were one entity; (ii) for resold teas the shop name ends up as the producer (curator's examples: Changtai at Crimson Lotus, "Menghai" at white2tea) — this splits the same pressing across producers and the product gate then vetoes it. Both are attribution bugs upstream of the matcher; aliases cannot fix them.

- **H.3 embedder module — status + new-dependency justification** (filed 2026-11-01 by ml-engineer; ROADMAP H.3; **status: implemented, preliminary eval only — thresholds NOT final, H.5 calibrates after owner label review**).

  **What landed.** `tea_scrapers/embed/`: `Embedder` protocol (`model_name`, `revision` = pinned 40-hex HF commit SHA, `dim`, `supports_sparse`, `embed(texts, with_sparse=)` → L2-normalized dense + optional sparse token-id→weight); `ModelKey(model, revision, dim)`; `text_hash` = hex SHA-256 of NFC + whitespace-collapsed text (`TEXT_NORMALIZATION_VERSION = "nfc-ws-v1"`). `EmbeddingStore` interface (`get_many(key, text_hashes)` / `put_many(key, entries)`) with in-memory and gzip-JSONL file stores (float16 dense = `halfvec` precision) — H.4's `name_embedding` table implements the same two methods; `CachedEmbedder` is the read-through wrapper and, with no inner model, a cache-only embedder that raises `EmbeddingCacheMiss`. `FakeEmbedder` (SHA-256-seeded, no model) for plumbing tests; `fixture_embedder()` over the committed vectors for harness tests. `BgeM3Embedder` lazily imports torch and raises `EmbedderUnavailable` with the install hint when the extra is missing. Eval: `tea_scrapers.eval.embedding_eval` (dense / sparse / weighted-hybrid `PairScorer`s for the H.1 harness, ROC-AUC, recall at P ≥ 0.98 / 0.90, per-category tables). Fixture builder: `scripts/build_name_embedding_fixtures.py` (`--check` needs no model; `--parity` checks the model card numbers).

  **Model pin.** `BAAI/bge-m3` @ `5617a9f61b028005a4858fdac845db406aefb181` (HF `lastModified` 2024-07-03), MIT. Only the 7 inference files are fetched (`config.json`, tokenizer ×4, `pytorch_model.bin`, `sparse_linear.pt`; ~2.2 GB) — the repo's ONNX copy (another ~2.2 GB) and ColBERT head are skipped. `use_safetensors=False` is explicit (repo has no safetensors; transformers would otherwise fire a background Hub conversion request). Dense = 1024-d CLS, L2-normalized, matching H.4's `halfvec(1024)`.

  **Dependency choice: neither FlagEmbedding nor sentence-transformers — `transformers` + `torch` directly**, as an optional `[embed]` extra (`torch>=2.6`, `transformers>=4.44,<5`, `huggingface-hub>=0.23,<1.0`; tested with torch 2.15.0+cpu, transformers 4.57.6, huggingface-hub 0.36.2). Both candidate libraries are thin layers *over* transformers + torch, so neither saves the heavy part:
  - *sentence-transformers*: dense head only for BGE-M3 (its `SparseEncoder` targets SPLADE-style MLM heads, not M3's `sparse_linear.pt`), and adds scikit-learn + scipy + Pillow. We would lose the sparse signal the owner asked to keep available.
  - *FlagEmbedding*: has dense + sparse (+ ColBERT) via `BGEM3FlagModel`, but its install pulls `datasets`, `accelerate`, `peft`, `sentence-transformers`, `ir-datasets`, `sentencepiece`, `protobuf` for fine-tuning / eval code we never run, and it is a research repo with a coarse release cadence.
  - *Direct*: M3 inference is ~30 lines (dense = normalized `[CLS]`; sparse = `relu(sparse_linear(h))` per token, max-pooled per token id, `<s>` `</s>` `<pad>` `<unk>` dropped) and reproduces `BGEM3FlagModel` exactly — `--parity` matches the model card: dense `[[0.6259, 0.3475], [0.3499, 0.6782]]` vs card `[[0.6265, 0.3477], [0.3499, 0.678]]` (card is fp16), lexical 0.19554 vs 0.19555, disjoint 0.0. Cost: we own those 30 lines (pinned by the parity check). Installed size with CPU wheels: 1.2 GB (vs ~5 GB with CUDA wheels).
  - `numpy` is **not** required by the base package (stdlib `struct` float16 + pure-Python cosine; ~1k pairs × 1024 d runs in < 1 s). `pgvector` 0.5 does not pull numpy either.

  **Fixture vectors.** `tests/fixtures/matching/name_embeddings_bge_m3.jsonl.gz` — 672 unique texts (all H.1 product names as matching keys, producer-pair names, producer hints), float16 dense + 4-dp sparse, **1.44 MB** gzip (entropy-bound: float16 is ~1.38 MB raw). Byte-deterministic (gzip `mtime=0`, sorted). `test_fixture_covers_every_pair_text` fails when labels or `matching_key` change → rerun the builder (incremental; only new texts embed).

  **Preliminary eval (2026-11-01, all 517 agent-proposed pairs; `python -m tea_scrapers.eval.embedding_eval`).** Hybrid = 0.5·dense + 0.5·trigram; "P ≥ 0.98 R" = max recall over t ∈ {0.00…0.99} with precision ≥ 0.98. Best-F1 is uninformative here (it degenerates to t ≈ 0 because the gate already did the work and positives are ~45%), so AUC over the pairs the score actually decides is shown instead.

  | view | scorer | AUC (score-decided) | P ≥ 0.98: t / R | P ≥ 0.90: t / R |
  |---|---|---|---|---|
  | product / h2 (live gate, 131 score-decided) | trigram | 0.521 | 0.92 / 0.310 | 0.88 / 0.323 |
  | | dense | 0.532 | 0.99 / 0.290 | 0.97 / 0.323 |
  | | sparse | 0.473 | 0.96 / 0.303 | 0.93 / 0.323 |
  | | hybrid dense⊕trigram | 0.526 | 0.95 / 0.316 | 0.92 / 0.329 |
  | | hybrid dense⊕sparse⊕trigram | 0.503 | 0.94 / 0.323 | 0.92 / 0.329 |
  | producer / v1 (112 score-decided) | trigram | 0.552 | 0.67 / 0.157 | 0.65 / 0.176 |
  | | dense | 0.547 | 0.90 / 0.078 | 0.85 / 0.176 |
  | | sparse | 0.601 | 0.83 / 0.078 | 0.83 / 0.078 |
  | | hybrid dense⊕trigram | 0.554 | 0.79 / 0.137 | 0.79 / 0.137 |

  Per category at the P ≥ 0.98 operating point: product `cross_script` 5/44 (trigram) → 5/44 (dense) → 8/44 (3-way hybrid); `suffix_variant` 25/37 for all but dense (23/37); `cross_vendor_same_tea` 17/68 for all (the rest are gate-vetoed); 0 FPs on `hard_negative_recipe_code` / `_year` / `_sheng_shou` for all. Producer `cross_script` 0/14 for every scorer; `producer_alias` 8/37 trigram vs 4/37 dense. Dense weight sweep for the blend (0.2–0.8) moves product P ≥ 0.98 recall only within 0.310–0.323.

  **Reading (preliminary).** (1) **The gate, not the scorer, caps product recall**: under the live H.2 gate every scorer hits the same ceiling (R 0.561 at t = 0); the remaining loss is one-sided weight/year vetoes (sub-q #7). (2) **Dense collapses the distinguishing token**, as the rationale predicted: `hard_negative_recipe_code` / `_name_variant` / `_sheng_shou` medians 0.90–0.93 vs `cross_script` positives median 0.75, so dense ranks hard negatives *above* true cross-script matches; the guard is what keeps precision, and P ≥ 0.98 forces dense to t = 0.99. (3) **Cross-script is not solved by BGE-M3 on short names** (sub-q #9). (4) **No scorer beats trigram materially at the 0.98 target on this set** — +0.6–1.3 pts product recall for the hybrids, −2 pts for dense alone, worse for producers. This set is ~50% adversarial hard negatives by design, so AUC ≈ 0.5 here is not production AUC; the shadow run (H.6) on real candidate sets is the real test. Implication for H.5: embeddings are more plausible as a **candidate generator / review-band signal** (kNN recall, LLM tiebreaker input) than as an auto-match score; revisit after #7 and #9 move the gate.

  **H.3 Option B — BGE-M3 problem or embeddings problem? (2026-11-04, ml-engineer; owner chose "Option B".)** Same 517 agent-proposed pairs, same harness and metric ("R@P≥.98" = max recall over t ∈ {0.00…0.99} with P ≥ 0.98; best-F1 shown as F1 at its t). Small deltas are noise: product has 155 positives (1 pair ≈ 0.006 R), producer 51 (1 pair ≈ 0.020 R). Models: `BAAI/bge-m3` @ `5617a9f6` (above); **`Qwen/Qwen3-Embedding-0.6B` @ `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`** (Apache-2.0, `tea_scrapers/embed/qwen3.py`, same `[embed]` extra with `transformers>=4.51`; last-token pooling, left padding, card prompt `Instruct: {task}\nQuery:{text}` applied to *both* sides because matching is symmetric; instruction is folded into `model_name` → `ModelKey`; **parity vs the card's transformers example: 4/4 scores within 3e-7**); `intfloat/multilingual-e5-small` @ `614241f6` (dense only, `query: ` prefix + mean pool; scratch adapter, not committed). "pinyin" = hanzi → toneless spaced pinyin in place (`pinyin_augment(…, "replace")`, eval path only; same text as H.2b's `pinyin_key` for these pairs — trigram rows reproduce H.2b's (a1) exactly). An `append` mode (keep hanzi, add pinyin) was also run: never better than `replace`.

  | model / scorer | product h2 R@P≥.98 (plain → pinyin) | product h2 best-F1 | producer v1 R@P≥.98 (plain → pinyin) | producer best-F1 (plain → pinyin) | `大红袍`/`da hong pao` cos (pctile among 247 non-match product pairs) |
  |---|---|---|---|---|---|
  | trigram | 0.310 → 0.323 | 0.540 → 0.552 | 0.157 → 0.196 | 0.615 → 0.667 | — |
  | BGE-M3 dense | 0.290 → 0.323 | 0.549 → 0.553 | 0.078 → 0.118 | 0.615 → 0.624 | 0.343 (0.4%) |
  | BGE-M3 dense⊕sparse⊕trigram | 0.323 → 0.336 | 0.542 → 0.551 | 0.078 → 0.118 | 0.615 → 0.667 | |
  | Qwen3 no instruction | 0.245 → 0.258 | 0.540 → 0.542 | 0.059 → 0.098 | 0.615 → 0.615 | 0.548 (3.6%) |
  | **Qwen3 + instruction**, dense | **0.336 → 0.348** | 0.551 → 0.550 | 0.157 → **0.216** | 0.638 → **0.689** | **0.620 (12.6%)** |
  | Qwen3 + instruction, ⊕trigram 0.5 | 0.316 → 0.329 | 0.544 → 0.554 | 0.137 → 0.176 | 0.618 → 0.689 | |
  | Qwen3 + instruction @512 / @256 (MRL) | 0.336 / 0.336 → 0.348 / 0.348 | ≈ same | 0.118 / 0.137 → 0.176 / 0.196 | ≈ same | 0.642 / 0.665 |
  | e5-small dense | none → none | 0.542 → 0.548 | 0.059 → 0.098 | 0.625 → 0.672 | 0.793 (0.4%) |

  Instruction: product pairs use `"Given a tea product name, retrieve names of the same product"`; producer pairs `"Given a tea producer or brand name, retrieve names of the same producer"` (using the product instruction for producers too gives 0.176 → 0.216 — same). Threshold-free separability (AUC over score-decided pairs) is where Qwen3+instruction is clearly better: product ungated 0.646 vs trigram 0.582 / BGE-M3 0.577; producer 0.654 vs 0.552 / 0.547 — but once trigram also gets pinyin, producer AUC is 0.716 vs 0.705 (the gap mostly closes).
  **Per category at P ≥ 0.98.** Product `cross_script`: trigram 5/44 → 7/44 with pinyin; BGE-M3 dense 5 → 8; Qwen3+inst 9 → 11. Producer `cross_script`: **0/14 for every model without pinyin**; with pinyin trigram 2/14, BGE-M3 2/14, Qwen3+inst 3/14. Producer `producer_alias`: trigram 8/37, Qwen3+inst 8/37 (9/37 single-instruction), BGE-M3 4/37, e5 3/37 — pinyin changes nothing here (aliases are abbreviations / brand names: `W2T`, `CLT`, `CNNP`↔`Zhong Cha`, `三鹤`↔`Three Cranes`). Short romanized ↔ hanzi names score **below 87–99.6% of non-match pairs in every model** (pctile column) — no model in this class treats `da hong pao` and `大红袍` as the same string.
  **On top of the shipped H.2b default** (pinyin + skeleton exact + curated aliases; trigram still drives the gate's producer resolution, dense only replaces the score slot; `matcher_variants` driven read-only from a scratch script): product 0.406 → 0.413 (+1 pair), producer 0.765 → 0.784 (+1 pair) with Qwen3+instruction dense; dense⊕trigram 0.406 / 0.765 (+0). 256-d: 0.413 / 0.765.
  **Answer: it is an embeddings problem, not a BGE-M3 problem.** A better model (Qwen3 + instruction) ranks better overall (AUC +0.06–0.10) and does narrow the Da Hong Pao gap, but at the P ≥ 0.98 auto-match point no model moves recall beyond noise — on plain text, with pinyin, or on top of H.2b. The product ceiling is the gate (0.561 at t = 0, sub-q #7), producer recall comes from transliteration + aliases (0.157 → 0.765), and the instruction matters more than the model (Qwen3 without it is *worse* than trigram). **Recommendation:** V1 matcher = trigram + pinyin/skeleton + curated aliases (H.2b) — do **not** build H.4 (`name_embedding` table) / embedding auto-match now. If embeddings come back (H.5 after owner label review, or as kNN candidate generation / LLM-tiebreaker context for the review band, where AUC matters more than the 0.98 point), use **Qwen3-0.6B + instruction, 256-d MRL** (≈ same as 1024-d here, ¼ the storage), not BGE-M3. No Qwen3 fixture vectors are committed (not recommended for V1); `scripts/build_name_embedding_fixtures.py --model qwen3 --instruct [--dim 256] [--augment replace] --out …` rebuilds them (~1 min CPU, 1.2 GB weights) and `--model qwen3 --parity` re-checks the card. The committed BGE-M3 fixture (1.44 MB) and adapter stay until the owner decides whether to retire them.
