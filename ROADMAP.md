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
- [ ] **3. Shopify scraper** — generic implementation, vendor config loader (§6.1), end-to-end VCR test against yunnansourcing.us
      _Owners: scraper-engineer + qa-engineer_
- [ ] **4. Run remaining Shopify vendors** — white2tea, Crimson Lotus, Bitterleaf, Yunnan Sourcing .com (§6.1)
      _Owner: scraper-engineer_
- [ ] **5. Bronze loader** — JSONL → `raw_product_snapshot` with `payload_hash` dedup (§8)
      _Owner: data-engineer_
- [ ] **6. Canonical ID matcher + silver normalizer** — Shopify products → `product` / `vendor_product` / `product_snapshot` (§8). Trigram index from day 1.
      _Owner: data-engineer_
- [ ] **7. Steepster scraper** — separate module, 1 rps, HTML parsing, author hashing (§6.2)
      _Owner: scraper-engineer_ — prerequisite for EDA. The scrape runs here; tag extraction from notes still waits for V.3.
- [ ] **8. TeaDB scraper** — WordPress JSON API preferred (§6.3)
      _Owner: scraper-engineer_
- [ ] **9. Reddit scraper** — PRAW with date-windowed crawl (§6.4)
      _Owner: scraper-engineer_
- [ ] **10. Cron integration** — shell script + crontab entry as `scripts/crontab.example`
      _Owner: scraper-engineer_

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
- Framework choice for UI (Next.js leaning) — blocks SPEC: ui
