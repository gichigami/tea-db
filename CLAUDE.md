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

Steps 1 (Scaffolding), 2 (Shared infra), 3 (Shopify scraper), 4 (Run remaining Shopify vendors), 5 (Bronze loader), and 6 (Canonical ID matcher + silver normalizer) are complete — the `tea-scrapers/` package installs into a venv at `~/.venvs/tea-scrapers` (out of `~/Desktop` because iCloud sync flags in-tree `.venv/` files as hidden, breaking the editable-install `.pth`), the `tea-scrape ingest shopify --vendor <key>` and `--all` CLI runs end-to-end against all 4 configured Shopify vendors (paginates `/products.json`, writes Hive-partitioned JSONL, finalizes the `scrape_run` row), `tea-scrape load --since YYYY-MM-DD` streams those JSONL files into `raw_product_snapshot` with `payload_hash` dedup, `tea-scrape normalize --since YYYY-MM-DD [--source <key>] [--batch-size 500]` reads bronze and writes silver `product` / `vendor_product` / `product_snapshot` rows with composite Shopify `vendor_external_id = "{product_id}:{variant_id}"`, a 4-step product canonical matcher (exact → trigram@0.85 via `%` + session-scoped `set_limit` → ambiguous-overcreates → create) + 3.5-step producer matcher (NFC norm → alias → exact → trigram → create), single set-based tier sweep (A=currently-available / B=available-within-24mo / C=older-or-never), `RunTracker(source='normalizer', mode='silver')`, exit codes 0/1/2 per §7, and **189 tests pass** (54 + 13 unit, 81 + 41 integration, all cassette-driven via vcrpy with one cassette + one 10-record golden JSONL per vendor). The `yunnan_sourcing_com` cassette is hand-trimmed to pages 1+2 + a synthesized empty page-3 terminator (artifact-level trim only, §11 anti-pattern preserved). `alembic upgrade head` lands at `002 (head)` and materializes all 10 tables with trigram (on `product.canonical_name` AND `producer.canonical_name`) + HNSW indexes. Postgres runs in the `tea-postgres` Docker container on `localhost:5432` (`docker compose up -d` from `tea-scrapers/` if a fresh session finds it stopped). Active Shopify vendors: `yunnan_sourcing_us`, `yunnan_sourcing_com`, `white2tea`, `crimson_lotus`. **Shopify bot mitigation (§12, ADDRESSED V1.1):** `Settings.user_agent` and `Settings.reddit_user_agent` now field-validate against the well-known placeholder substrings — the project refuses to start without a real `USER_AGENT` in `.env`. `HttpClient` emits a `WARNING`-level `scrape.request` event with `terminal_block=True` + `status=<401|403>` before raising on auth / edge-mitigation responses. `tea-scrapers/scripts/crontab.example` documents the recommended hourly per-vendor 15-minute-staggered schedule. `rate_limit_rps: 2` per vendor is unchanged (page-1 failures aren't a rate effect, per tech-lead audit). Live re-scrape verification of the 3 previously-blocked vendors is an operator task post-merge (allow ≥1 hour cool-down from the last 403). **Bronze loader follow-ups (§12, non-blocking):** rename `LoadStats._vendor_bucket` → `bucket_for`, add `LoadStats.insert_errors` counter, centralize `reset_session_caches()` test helper (partially done — fixture in `tests/conftest.py`, but the bronze-loader test still inlines the original; retire at next-touch), strengthen `test_since_filter_skips_older_partitions` with distinct per-partition fixtures, broader §4 idempotency rewrite. **Silver normalizer follow-ups (§12, non-blocking):** 10 OQs filed (non-Shopify external_id schemes, `variant.grams` tare quirk codified, USD V1 hardcode, cultivar/region extraction for non-YS vendors, LLM tiebreaker stub, tier-sweep perf, `--since` semantic divergence, `canonical_name` non-UNIQUE intentional, variant-id republish edge case, multi-writer idempotency) + 7 polish items (`_trgm_initialized_sessions` late-init, aliases docstring drift, no-snapshots-tier-C comment, cross-batch cache survival, tier-test boundary brittleness, `len(.all())` count idiom, AMBIGUOUS_GAP fall-through test gap). Next unblocked task is **ingest step 7 (Steepster scraper)** per `ROADMAP.md:39`: separate module, 1 rps, HTML parsing, author hashing (§6.2) — owned by scraper-engineer; prerequisite for EDA, with tag extraction from notes still gated on V.3.
