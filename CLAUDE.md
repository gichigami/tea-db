# Tea Recommendation Engine

A precision tea recommendation engine pairing a **shoppable surface** (currently-purchasable products from premium English-language vendors) with a **reference surface** (historical catalog of discontinued and legendary teas). Every recommendation is traceable to source phrasing; every badge is explainable by axis; every comparison reveals structured difference.

## Sources of truth

| Doc | Scope | Authority for |
|---|---|---|
| `tea_rec_engine_design_v2.md` | Product / design | Scope, dimensional model, vocabulary normalization, display surfaces, V1 plan, open questions |
| `specs/tea_scrapers_v1_spec.md` | Implementation | Tooling, project structure, conventions, postgres schema, anti-patterns |
| `ROADMAP.md` | Status | What's done / in-progress / next, per §10 of the scrapers spec and §8 of the design doc |

When intuition disagrees with the design doc, the design doc wins. When generic best practice disagrees with the scrapers spec, the spec wins — it was deliberately authored to override generic patterns (see its §11 anti-patterns).

## How the team operates

This repository is staffed by specialized subagents in `.claude/agents/`. When a task fits a specialist, delegate via the Agent tool. Specialists are biased toward their concern and tend to push back on cross-cutting decisions — intentional.

| Agent | Owns |
|---|---|
| `tech-lead` | Design coherence, work breakdown, open-question arbitration, scope policing |
| `scraper-engineer` | `sources/`, `http/`, raw JSONL ingestion |
| `data-engineer` | Postgres schema, alembic, bronze→silver normalization, canonical product ID matching |
| `ml-engineer` | LLM extraction, embeddings, recommender service, vendor reliability scoring |
| `ontology-curator` | Flavor hierarchy (L1/L2/L3), multilingual labels, mouthfeel / qì / huí gān axes |
| `frontend-engineer` | Dual-surface UI, tea card, comparison views, "historical → current pivot" |
| `qa-engineer` | pytest + VCR cassettes, golden JSONL fixtures, hand-labeled validation set |
| `code-reviewer` | Design-doc adherence, anti-pattern enforcement; invoke proactively after non-trivial changes |

## Slash commands

| Command | Purpose |
|---|---|
| `/standup` | Read ROADMAP, summarize done / in-flight / next |
| `/next-task` | Pick the next task per §10 sequencing, identify the owning specialist |
| `/open-questions` | List unresolved items from §10 design doc + §12 scrapers spec |
| `/spec-check` | Audit current code against design-doc principles and scraper anti-patterns |

## Norms

- **Scrapers write JSONL; loaders read JSONL.** Never merge those steps. (scrapers spec §11)
- **Capture every record at scrape time; filter downstream.** (§11)
- **Quote-evidence is non-negotiable.** Every flavor tag, mouthfeel rating, and qì axis stores the source sentence that produced it. (design §4, §6 #1)
- **Data quality tier (A/B/C/D) is a first-class column**, not a derived afterthought. (design §3)
- **The "historical → current pivot" is the killer feature** (design §5, §6 #7). Treat as primary, not nice-to-have.
- **Open questions are tracked, not silently resolved.** (design §10, scrapers §12)

## Current state

Steps 1 (Scaffolding), 2 (Shared infra), 3 (Shopify scraper), 4 (Run remaining Shopify vendors), 5 (Bronze loader), 6 (Canonical ID matcher + silver normalizer), and 7 (Steepster scraper) are complete — the `tea-scrapers/` package installs into a venv at `~/.venvs/tea-scrapers` (out of `~/Desktop` because iCloud sync flags in-tree `.venv/` files as hidden, breaking the editable-install `.pth`), `tea-scrape ingest shopify --vendor <key>` / `--all` runs end-to-end against the 4 configured Shopify vendors (paginates `/products.json`, writes Hive-partitioned JSONL, finalizes `scrape_run`), `tea-scrape ingest steepster --vendor <slug> | --all [--max-teas N] [--mode full|incremental]` walks Steepster's per-vendor company index and tea-detail pages with HTML parsing via `selectolax` (no new dep — already in pyproject), `tea-scrape load --since YYYY-MM-DD` streams JSONL into `raw_product_snapshot` with `payload_hash` dedup, `tea-scrape normalize --since YYYY-MM-DD [--source <key>] [--batch-size 500]` reads bronze and writes silver `product` / `vendor_product` / `product_snapshot` rows with composite Shopify `vendor_external_id = "{product_id}:{variant_id}"`, a 4-step product canonical matcher (exact → trigram@0.85 via `%` + session-scoped `set_limit` → ambiguous-overcreates → create) + 3.5-step producer matcher (NFC norm → alias → exact → trigram → create), single set-based tier sweep (A=currently-available / B=available-within-24mo / C=older-or-never), `RunTracker(source=…, mode=…)` per stage, exit codes 0/1/2 per §7, and **242 tests pass** (full suite cassette-driven via vcrpy with one cassette + one 10-record golden JSONL per source; the step-7+ Steepster cassette is gzipped per §11 anti-pattern, 8.3 MB compressed / 48 MB raw / 5.8× ratio). The `yunnan_sourcing_com` cassette is hand-trimmed to pages 1+2 + a synthesized empty page-3 terminator (artifact-level trim only, §11 preserved). `alembic upgrade head` lands at `002 (head)` and materializes all 10 tables with trigram (on `product.canonical_name` AND `producer.canonical_name`) + HNSW indexes. Postgres runs in the `tea-postgres` Docker container on `localhost:5432` (`docker compose up -d` from `tea-scrapers/` if a fresh session finds it stopped); credentials are `postgres:postgres` per `docker-compose.yml`. **`.env` gotcha (filed as spec §12 step-7 follow-up #7):** `.env.example` ships with `DATABASE_URL` commented out — uncomment + set to `postgresql+psycopg://postgres:postgres@localhost:5432/tea` for live runs, else `fe_sendauth: no password supplied`. Step 7's `SteepsterConfig` lives in `config.py`; `config/vendors.yaml` has a sibling `steepster:` block (`rate_limit_rps: 0.1` honors robots.txt `Crawl-Delay: 10`, `timeout_seconds: 60` for 15–25s tea-detail render times, 7-slug V1 allowlist); `STEEPSTER_RATE_LIMIT_RPS` env override lets integration tests bypass the rate-limiter for fast cassette replay. `hash_author` is SHA-256 unsalted with `sha256:` prefix (NFC + lowercase + strip) — unsalted is intentional per spec §12 step-7 OQ #5 to enable cross-run returning-reviewer dedup at silver. Active Shopify vendors: `yunnan_sourcing_us`, `yunnan_sourcing_com`, `white2tea`, `crimson_lotus`. **Shopify bot mitigation (§12, V1.1 fix verified 2026-05-16):** the V1.1 UA fix (`Settings` validates against placeholder UA substrings; `HttpClient` emits `WARNING scrape.request` with `terminal_block=True` on 401/403; `crontab.example` per-vendor 15-min stagger) was live-verified: `white2tea` ✅ 1052 records / 3.4s clean, `crimson_lotus` ✅ 410 records / 2.5s clean, `yunnan_sourcing_com` ❌ 1000 records persisted then 403 on page 5 — diagnosed as **JA3 TLS-fingerprint block** (curl from same IP/UA in same minute returns 200, httpx 403s), not rate-based. Filed as spec §12 step-7 follow-up #9 with 3-option mitigation sequence (`httpx(http2=True)` → `curl-cffi` → per-vendor `http_client` knob). `yunnan_sourcing_com.rate_limit_rps` reverted to `2` (rate was never the issue) with KNOWN ISSUE comment in `vendors.yaml`. Until #9 is implemented, YS .com is the only Shopify vendor with a live-scrape gap — this is the **biggest shoppable surface** in V1 (~3,700+ SKUs), so prioritize before the bulk-seed cycle. Other Steepster operational follow-ups: #6 (max_teas slice post-walk wastes ~10 min on every `--max-teas` run; cheap early-terminate fix waiting on a polish PR), #8 (HttpClient retry backoff ~2 min per 504 is wildly too patient for Steepster's render-flaky origin; 5–10s would absorb at 1/12 wall-clock cost; matters most for the ~12–15-day bulk seed). **Bronze loader follow-ups (§12, non-blocking):** `LoadStats._vendor_bucket` rename, `insert_errors` counter, `reset_session_caches()` centralization, `test_since_filter_skips_older_partitions` strengthening, broader §4 idempotency rewrite. **Silver normalizer follow-ups (§12, non-blocking):** 10 OQs (non-Shopify external_id schemes, `variant.grams` tare quirk, USD V1 hardcode, cultivar/region extraction for non-YS vendors, LLM tiebreaker stub, tier-sweep perf, `--since` semantic divergence, `canonical_name` non-UNIQUE intent, variant-id republish edge, multi-writer idempotency) + 7 polish items (trgm late-init, aliases docstring drift, no-snapshots-tier-C comment, cross-batch cache survival, tier-test boundary brittleness, `len(.all())` idiom, AMBIGUOUS_GAP fall-through test gap). **Steepster live-verification (2026-05-16):** `crimson-lotus-tea --max-teas 3` ran in 1736s (29 min), 3 records / 81 notes / 69 unique author hashes, 9×504 + 1×502 absorbed by retry, 0 terminal blocks; cassette↔live equivalence proven on all 3 overlap records. Next unblocked task is **ingest step 8 (TeaDB scraper)** per `ROADMAP.md:41`: WordPress JSON API preferred (§6.3) — owned by scraper-engineer; prerequisite for EDA along with step 9 (Reddit). Steps 5–6 silver/bronze pipeline already handles Shopify; Steepster bronze-load + Steepster→`product` join are gated on data-engineer work (spec §12 step-7 OQs #1–#4) and V.4 respectively.
