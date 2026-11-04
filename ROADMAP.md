# Tea Recommendation Engine — Roadmap

Tracks progress against the V1 plan. Two sources of truth:
- **Ingest backbone**: §10 of `specs/tea_scrapers_v1_spec.md`
- **Recommender + UI**: §8 of `tea_rec_engine_design_v2.md`

Items prefixed with `SPEC:` are spec-drafting tasks. They block the dependent implementation work in their area.

---

## Specs (draft before implementing)

The scrapers spec is the only complete v1 spec. The rest are stubs. Drafting follows `/spec-draft <area>` and uses `specs/spec-template.md`.

- [x] `specs/tea_scrapers_v1_spec.md` — scraper architecture, postgres schema, anti-patterns
- [ ] **SPEC: ontology** → `specs/tea_ontology_v1_spec.md` — ontology-curator — **blocked on EDA.4** (which requires ingest steps 1–9). Drafting against real corpus statistics, not in the abstract.
- [ ] **SPEC: extraction** → `specs/tea_extraction_v1_spec.md` — ml-engineer
- [ ] **SPEC: recommender** → `specs/tea_recommender_v1_spec.md` — ml-engineer
- [ ] **SPEC: reliability** → `specs/tea_reliability_v1_spec.md` — ml-engineer
- [ ] **SPEC: ui** → `specs/tea_ui_v1_spec.md` — frontend-engineer
- [ ] **SPEC: testing** → `specs/tea_testing_v1_spec.md` — qa-engineer

---

## Ingest backbone (scrapers spec §10)

- [x] **1. Scaffolding** — pyproject, project structure (scrapers spec §3), Postgres docker-compose, alembic init, CLI shell. **Decision (2026-05-16, tech-lead): scaffolding lives under `tea-scrapers/` subdirectory of repo root.** Rationale: scrapers spec §3 already trees that way; the umbrella `tea-db/` repo will gain sibling `tea-recommender/` and `tea-ui/` trees per design §5 and §8.
      _Owners: scraper-engineer + data-engineer_
- [x] **2. Shared infra** — `HttpClient` with rate limit + retry (§4), `JsonlWriter` (lives at `storage/raw.py`) with partitioning (§5), structlog config, run tracking (§8). **Spec sync (2026-05-16, scraper-engineer):** §4 now documents `HttpClient.RETRYABLE_STATUSES` (line 163) and the `scrape.request` transport-error event shape (line 176); §12 adds the stale `scrape_run` row sweep open item (line 755).
      _Owner: scraper-engineer_
- [x] **3. Shopify scraper** — generic implementation, vendor config loader (§6.1), end-to-end VCR test against yunnansourcing.us. **Resolved (2026-05-16):** `ShopifyScraper` + `load_shopify_vendors` + CLI body landed; 14 unit tests + 15 integration tests (cassette + 10-record golden JSONL) pass; spec §12 first bullet closed (Bitterleaf is WooCommerce, removed from `vendors.yaml`); `database_url` default fixed to `postgresql+psycopg://` driver.
      _Owners: scraper-engineer + qa-engineer_
- [x] **4. Run remaining Shopify vendors** — white2tea, Crimson Lotus, Yunnan Sourcing .com (§6.1). **Resolved (2026-05-16, scraper-engineer + qa-engineer):** 3 new cassettes + 3 new 10-record golden JSONL + 3 new integration test files landed; 45 new tests pass (15 per vendor — 5 cassette/replay + 10 golden parametrize), full suite 100/100. `yunnan_sourcing_com` cassette hand-trimmed to pages 1+2 + a synthesized empty page-3 terminator per planning §2 (artifact-level trim only — no scraper changes; §11 anti-pattern preserved). Bitterleaf intentionally absent — removed from `config/vendors.yaml` in step 3 as WooCommerce (§12 first bullet closed). Live `--all` smoke test exercised the multi-vendor failure-isolation path correctly: 1 vendor succeeded, 3 vendors 403'd (likely bot mitigation triggered by the placeholder User-Agent + sustained IP traffic); CLI handled all 4 outcomes gracefully and finalized `scrape_run` rows accordingly. The 403 finding is filed as a new §12 open item for V1.1 ops follow-up. **V1.1 ops fix verified 2026-05-16 evening (live re-scrape post-merge):** `white2tea` ✅ 1052 records / 3.4s / 0 terminal blocks; `crimson_lotus` ✅ 410 records / 2.5s / 0 terminal blocks; `yunnan_sourcing_com` ❌ 1000 records persisted then 403 on page 5 — diagnosed as JA3 TLS-fingerprint block, not rate-based (curl from same IP/UA in the same minute returns 200; httpx 403s). Filed as spec §12 step-7 follow-up #9 with a 3-option mitigation sequence (httpx `http2=True` → `curl-cffi` → per-vendor `http_client` knob). vendors.yaml YS .com restored to `rate_limit_rps: 2` with inline KNOWN ISSUE comment.
      _Owners: scraper-engineer + qa-engineer_
- [x] **5. Bronze loader** — JSONL → `raw_product_snapshot` with `payload_hash` dedup (§8). **Resolved (2026-05-16, data-engineer + code-reviewer):** `load/bronze.py` lands `payload_hash` + `LoadStats` + `BronzeLoader` (310 lines); `cli.py` `load_cmd` body wired with `--since YYYY-MM-DD` per spec §7, exit codes 0/1/2; per-vendor batched `INSERT ... ON CONFLICT DO NOTHING` against `uq_raw_source_external_hash`; per-batch transactions; `RunTracker(source='loader', mode='bronze')`. 24 new tests pass (13 unit + 7 integration + 4 hash-shape parametrized variants); full suite 124/124. Dedup verified end-to-end: golden fixture run 1 inserts 10, run 2 inserts 0. Spec §4 line 193 synced with §8 in the same PR (replaced `scraped_at` with `payload_hash` + canonicalization recipe). Code review flagged 5 non-blocking follow-ups (filed as new §12 open item — see `tea_scrapers_v1_spec.md`).
      _Owner: data-engineer_
- [x] **6. Canonical ID matcher + silver normalizer** — Shopify products → `product` / `vendor_product` / `product_snapshot` (§8). Trigram index from day 1. **Resolved (2026-05-16, data-engineer + code-reviewer):** `normalize/` module (`shopify_mapper.py`, `tags.py`, `canonical.py`, `silver.py`, `tier.py`) + `tea-scrape normalize --since YYYY-MM-DD [--source <key>] [--batch-size 500]` CLI body land; product matcher is 4-step (exact → trigram@0.85 via `%` + session-scoped `set_limit` → ambiguous-overcreates → create), producer matcher is 3.5-step (NFC norm → alias → exact → trigram → create); composite `vendor_external_id = "{shopify_product_id}:{shopify_variant_id}"` enables variant fan-out (1 Shopify product with N weight-variants → N `product` rows + N `vendor_product` rows); single set-based tier sweep (A=currently-available / B=available-within-24mo / C=older-or-never) scoped to `:touched_ids`. Alembic 002 adds `idx_producer_name_trgm` mirroring `idx_product_name_trgm`. `RunTracker(source='normalizer', mode='silver')`, exit codes 0/1/2 per §7. 65 new tests pass (38 unit + 27 integration); full suite 189/189. Code review flagged 5 file-cited follow-ups (landed in same PR) + 7 polish items (filed as new §12 silver-normalizer entry). Spec §8 synced (`idx_producer_name_trgm` DDL + composite `vendor_external_id` note); 10 new §12 OQs filed.
      _Owner: data-engineer_
- [x] **7. Steepster scraper** — separate module, 1 rps, HTML parsing, author hashing (§6.2). **Resolved (2026-05-16, scraper-engineer):** `sources/steepster.py` lands `SteepsterScraper` + `hash_author` (`sha256:` prefix, NFC + lowercase + strip; unsalted is intentional per spec §12 step-7 OQ #5). `tea-scrape ingest steepster --vendor <slug> | --all [--max-teas N] [--mode full|incremental]` CLI body wired; `RunTracker(source='steepster', mode=<mode>)`. `SteepsterConfig` + `load_steepster_config` added to `config.py`; `config/vendors.yaml` gains a sibling `steepster:` block (`rate_limit_rps: 0.1` honors robots.txt `Crawl-Delay: 10`; `timeout_seconds: 60` for the 15–25s tea-detail render times; 7-slug V1 allowlist). Pagination terminator pinned (§12 step-7 OQ #6 resolved): zero items OR no "Next + `?page=`" link, applied identically on the company-index walk and the tasting-note walk. 43 new tests pass (27 unit + 16 integration — 5 cassette/replay tests + 1 cassette↔golden equivalence check + 10 per-line parametrized golden tests), full suite **242/242**. 1 VCR cassette (`tests/fixtures/cassettes/steepster_crimson_lotus_tea.yaml.gz`, 8.3 MB compressed / 48 MB raw, 5.8× gzip ratio, 1 company page + 10 tea-detail pages) + 1 10-record golden JSONL (`tests/fixtures/golden/steepster.jsonl`, derived from the committed cassette per §9 procedure with synthetic `scraped_at` / `run_id` placeholders). Leak audit (`zcat | grep -iE 'set-cookie|authorization'`) clean. `STEEPSTER_RATE_LIMIT_RPS` env override added (lets the integration tests bypass the rate-limiter for fast cassette replay without slowing live runs). 6 step-7 OQs (in §12 "Step-7 Steepster kickoff risks") status-updated: #5 (author hashing) + #6 (pagination terminators) resolved; #1 (vendor_external_id scheme) status note added; #2 (bronze schema) + #3 (silver join) + #4 (tier applicability) confirmed scraper-side neutral and pinned for data-engineer / tech-lead at silver-load time. 5 new §12 step-7 follow-ups filed (env-override consolidation, `max_teas_per_vendor` lift, structural author-leak audit caveat, sitemap fallback non-implementation rationale, cassette size trajectory). **Live re-verification 2026-05-16 evening:** `tea-scrape ingest steepster --vendor crimson-lotus-tea --max-teas 3` completed in 1736s (29 min) with `scrape_run.status='success'`, 3 records / 81 notes / 69 unique author hashes, 9×504 + 1×502 absorbed by retry, 0 terminal blocks; cassette↔live equivalence proven on all 3 overlap records (same `steepster_id` set, payload top-level keys identical, note counts identical including the 36-note Bulang Shan tea). 3 additional §12 step-7 follow-ups filed from the live run: #6 status-update (`max_teas_per_vendor` post-walk slice empirically validated — ~10 of 29 min was wasted company-walk overhead), #7 (`.env.example` ships `DATABASE_URL` commented out so first-time operators hit `fe_sendauth: no password supplied`), #8 (HttpClient retry backoff tuned too patient for Steepster's 504 pattern — ~2 min per retry where 5–10s would suffice).
      _Owner: scraper-engineer_ — prerequisite for EDA. The scrape runs here; tag extraction from notes still waits for V.3.
- [ ] **8. TeaDB scraper** — WordPress JSON API preferred (§6.3)
      _Owner: scraper-engineer_
- [ ] **9. Reddit scraper** — PRAW with date-windowed crawl (§6.4)
      _Owner: scraper-engineer_
- [ ] **10. Cron integration** — shell script + crontab entry as `scripts/crontab.example`
      _Owner: scraper-engineer_

### Hybrid canonical matcher (amends step 6; scrapers spec §8 + §12 "Hybrid canonical matcher")

**Decision (2026-10-31, project owner; recorded by tech-lead): Option B — hybrid matcher.** Product + producer matchers move from pure trigram to exact → attribute guard (recipe code / sheng-shou / year / weight; mismatch vetoes) → trigram ∪ embedding-kNN candidates → calibrated hybrid score → LLM tiebreaker (ambiguous band) → create. Local BGE-M3 name embeddings as `halfvec(1024)` in a new `name_embedding` table (`model` / `revision` / `text_hash`), separate from description-level `product_embedding`. Shadow mode behind a `trigram | hybrid` flag (default `trigram`); flip only if hybrid beats the trigram baseline on the labeled set. Trigram is not removed. Precision-first (auto-match ≥ ~0.98) because merges are irreversible. Sub-questions #1–#8 in the spec §12 entry stay open. Runs in parallel with steps 8–9; does not block EDA.

- [x] **H.0 OQ + spec amendment** — §8 reframed (trigram = V1 baseline, hybrid = approved evolution); §12 entry filed with decision, rationale, phased plan, 7 open sub-questions; design §10 embedding OQ scoped to descriptions.
      _Owner: tech-lead_
- [ ] **H.1 Hand-labeled pair set + trigram baseline** — producer + product pairs (agent-proposed labels, owner-reviewed); trigram baseline precision / recall. **Built** (402 product + 115 producer pairs, 39 low-confidence in `tests/fixtures/matching/low_confidence_review.csv`, harness `tea_scrapers.eval.matcher_eval`); awaiting owner label review. Baseline @0.85: product P 0.65 / R 0.34 (with H.2; P 0.87 / R 0.34 after the 2026-11-01 guard follow-up), producer P 1.00 / R 0.06.
      _Owner: qa-engineer_
- [ ] **H.2 Attribute-guard parser + suffix near-dup fix** — parse recipe code / sheng-shou / year / weight; fix `":: Seattle Inventory"`-style suffix near-duplicates. **Implemented 2026-10-31, pending code-reviewer pass:** `normalize/attributes.py` (parser + guard + `ONE_SIDED_POLICY`, provisional all-`VETO` per spec §12 hybrid sub-q #7) wired into the trigram product matcher (`veto_created` decision + `guard_vetoes` stats); suffix-stripped matching key (`suffix_exact` decision; raw title still stored); AMBIGUOUS_GAP fall-through test added (silver polish #7). One-sided policy re-check waits on H.1 labeled set. Code-review nits fixed on top of 581b9b1. **Ops note:** databases normalized before H.2 still hold split base / `:: Seattle Inventory` products and pre-guard Raw-vs-Ripe merges — the matcher never revisits existing `vendor_product → product` links, so a clean re-normalize (truncate silver, `tea-scrape normalize --since <first bronze date>`) is required to collapse them. `canonical_name` order-dependence filed as spec §12 hybrid sub-q #8 (decide before H.3 / bulk seed). **H.1 follow-up 2026-11-01:** guard now falls back to the name-parsed vintage year when the mapper's `harvest_year` is NULL (`"Spring 2017 X"` vs `"Spring 2018 X"`; matching-time only, stored column unchanged) and adds an `item_number` attribute (`Part N` / `Pt.N` / `No.N` / `#N` / `Grade N` / model codes); H.1 h2 product P 0.650 → 0.867, R unchanged. See spec §12 hybrid sub-q #7.
      _Owner: data-engineer_
- [ ] **H.2b Cheap matcher fixes (owner: "Option A — cheap fixes first", 2026-11-04)** — three independent toggles, measured on the H.1 harness (`python -m tea_scrapers.eval.matcher_variants --aliases config/producer_aliases.yaml`). **Implemented, pending code-reviewer pass.** Spec §12 hybrid sub-qs #7 / #9 hold the tables.
  - [x] **H.2b.1 Hanzi → pinyin matching keys** (`normalize/translit.py`, new base dep `pypinyin`, MIT, §8 table) — pinyin key for trigram + space/punctuation/script-insensitive *skeleton* exact (producer: unique owner only; product: block scan + attribute guard, `skeleton_exact`). Default ON (`TRANSLITERATE_MATCHING_KEYS`). Product R@P≥.98 0.310 → 0.406, producer 0.157 → 0.255, 0 new FP. Collapsed-form trigram measured, adds nothing, not shipped.
  - [x] **H.2b.2 Curated producer-alias seeding** (`normalize/producer_aliases.py`; file `config/producer_aliases.yaml` by ontology-curator) — idempotent upsert into existing `producer.aliases` at `tea-scrape normalize` start (`--producer-aliases`); never renames / merges; conflicts reported. With H.2b.1: producer R@.85 0.059 → 0.765 at P 1.000. Remaining misses = curator's bare-toponym rule vs H.1 labels (owner to rule).
  - [ ] **H.2b.3 One-sided year / weight policy** (sub-q #7) — `canonical.ONE_SIDED_BLOCKING` (`veto` | `exact_only`), **default stays `veto`**; owner decision pending: flip `weight_grams` → `exact_only` (+12 `:: Seattle Inventory` twins, low-confidence labels, 0 FP). Not needed for the Steepster pivot (V.4 joins on producer + style; relaxation recovers ≤ 9/46 there).
      _Owner: data-engineer_ — ops note: the curator flagged mapper attribution bugs (single `"Other"` producer bucket; shop name as producer for resold teas) — filed in spec §12 sub-q #9, fix before bulk seed.
- [ ] **H.3 Embedder module** — `Embedder` protocol, cache keyed by `(model, revision, text_hash)`, fake embedder for tests; §11 new-dep justification (`sentence-transformers` + `torch` vs `FlagEmbedding`). **Implemented 2026-11-01, pending code-reviewer pass:** `tea_scrapers/embed/` (protocol + `ModelKey` + `text_hash`; `EmbeddingStore` interface with in-memory / gzip-JSONL float16 stores for H.4's table to implement; read-through `CachedEmbedder`; `FakeEmbedder` + cache-only `fixture_embedder` that raises on miss); `BgeM3Embedder` (dense 1024-d + sparse) behind optional `[embed]` extra = `torch` (CPU) + `transformers` directly — neither FlagEmbedding nor sentence-transformers (spec §12 "H.3 embedder module" for why); model `BAAI/bge-m3` pinned @ `5617a9f6`, parity-checked vs model card. Embeds the suffix-stripped matching key, not `canonical_name` (sub-q #8). Fixture vectors 672 texts / 1.44 MB; pytest needs no torch. **Preliminary eval (not final; H.5 calibrates):** at P ≥ 0.98, product/h2 recall trigram 0.310 vs dense 0.290 vs dense⊕trigram 0.316 vs 3-way 0.323; producer trigram 0.157 vs dense 0.078 vs hybrid 0.137 — no material lift; gate vetoes cap recall and BGE-M3 does not bridge pinyin↔hanzi short names (new sub-q #9: transliteration). **Option B (2026-11-04):** added `Qwen3Embedder` (Qwen3-Embedding-0.6B @ `97b0c614`, card parity ✓, instruction + MRL) and a pinyin probe; at P ≥ 0.98 no model beats trigram beyond noise (best: Qwen3+instruction product 0.336 / producer 0.157 plain, 0.348 / 0.216 with pinyin vs trigram+pinyin 0.323 / 0.196; on top of H.2b +1 pair each). Embeddings problem, not a BGE-M3 problem → V1 stays trigram + pinyin + aliases; if embeddings return, Qwen3+instruction @256-d. Spec §12 "H.3 Option B".
      _Owner: ml-engineer_ — depends on H.1 (eval harness)
- [ ] **H.4 Migration 003 `name_embedding` + matcher-strategy flag** — `trigram | hybrid`, default `trigram`.
      _Owner: data-engineer_ — depends on H.3 (dim / model metadata)
- [ ] **H.5 Threshold calibration** — auto-match ≥ ~0.98 precision + review band; pick scoring formula (weighted vs RRF); scope LLM tiebreaker.
      _Owners: ml-engineer + qa-engineer_ — depends on H.1–H.4
- [ ] **H.6 Shadow-mode run on bulk seed** — log trigram-vs-hybrid disagreements for owner review; default flips only on a labeled-set win.
      _Owner: data-engineer_ — depends on H.5
- [ ] **H.7 Tests + review** — fixture vectors, no network / no model download in CI; code-reviewer pass.
      _Owners: qa-engineer + code-reviewer_ — runs alongside H.2–H.6, closes after H.6

Ingest steps 1–9 are all prerequisites for **EDA** (which now precedes SPEC: ontology — see next section). After step 6, drafting of SPEC: recommender, SPEC: reliability, SPEC: ui, and SPEC: testing can proceed in parallel with steps 7–9.

---

## Exploratory Data Analysis (informs SPEC: ontology and downstream extraction)

Look at what flavor / mouthfeel / qì / huí gān language actually appears across real vendor descriptions, long-form reviews, encyclopedic entries, and community threads — before committing to a vocabulary structure. Per user direction: scope is all four scraped sources.

- [ ] **EDA.1** Corpus assembly — pull descriptions / reviews / threads from silver tables into a working analytical corpus, tagged by source register (marketing / review / encyclopedic / forum)
      _Owners: data-engineer + ml-engineer_ — depends on ingest steps 1–9
- [ ] **EDA.2** Vocabulary frequency + KWIC — n-grams and keyword-in-context for flavor, mouthfeel, qì, huí gān, shēng jīn terms; broken out by source register and by tea style (sheng / shou / oolong / hong / bai / lu / etc.)
      _Owners: ml-engineer + ontology-curator_ — depends on EDA.1
- [ ] **EDA.3** Multilingual surface audit — how often Hanzi / tone-marked pinyin / Cantonese romanizations appear and where; clustering on tradition_hints; coverage gaps by region
      _Owner: ontology-curator_ — depends on EDA.1
- [ ] **EDA.4** Findings memo (markdown + supporting notebooks) — proposed L1 / L2 seed nodes, L3 long-tail sample, identified gaps and ambiguities, build-vs-buy reassessment in light of observed vocabulary. Seeds SPEC: ontology.
      _Owner: ontology-curator_ — depends on EDA.2 + EDA.3

EDA findings unblock SPEC: ontology, which unblocks V.1 and everything downstream of it.

---

## Vocabulary + Extraction (design doc §8 Week 1-2)

- [ ] **V.1** Curate flavor hierarchy seed (L1 / L2 / L3, multilingual) — ontology-curator — depends on SPEC: ontology
- [ ] **V.2** Hand-label set: ~200 teas — qa-engineer + ontology-curator — depends on SPEC: testing + V.1
- [ ] **V.3** LLM extraction validated against hand-labeled set — ml-engineer — depends on SPEC: extraction + V.1 + V.2 + ingest step 6
- [ ] **V.4** Steepster → catalog join at producer + style level — data-engineer + ml-engineer — depends on ingest steps 6, 7
- [ ] **V.5** Vendor reliability scoring — ml-engineer — depends on SPEC: reliability + V.3 + V.4
- [ ] **V.6** Description embeddings → pgvector — ml-engineer — depends on V.3

---

## Recommender + UI (design doc §8 Week 3)

- [ ] **R.1** Recommender v1: filter + soft scoring — ml-engineer — depends on SPEC: recommender + V.3
- [ ] **R.2** UI: tea card with badges, dual surfaces — frontend-engineer — depends on SPEC: ui + R.1
- [ ] **R.3** Historical → current pivot feature — frontend-engineer + ml-engineer — depends on R.1 + R.2; the killer feature, treat as first-class
- [ ] **R.4** Comparison view (radar overlay) — frontend-engineer — depends on R.2

---

## Open questions (live; see `/open-questions`)

Tracked verbatim in:
- §10 of `tea_rec_engine_design_v2.md`
- §12 of `specs/tea_scrapers_v1_spec.md`
- Open Items section of every drafted spec in `specs/`

Highlights blocking near-term work:
- Ontology build vs buy — re-evaluated at EDA.4 with corpus evidence in hand, then resolved in SPEC: ontology. No longer a blocker for near-term work; the EDA corpus + frequency analysis is the deciding input.
- LLM provider for extraction — blocks SPEC: extraction
- Hybrid matcher sub-questions (spec §12 "Hybrid canonical matcher" #1–#8) — scoring formula + one-sided-attribute policy block H.5; two-embedding-stacks + `product_embedding` dim mismatch must be settled before V.6
- Framework choice for UI (Next.js leaning) — blocks SPEC: ui
