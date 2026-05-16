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

Steps 1 (Scaffolding) and 2 (Shared infra) are complete — the `tea-scrapers/` package installs into a venv at `~/.venvs/tea-scrapers` (out of `~/Desktop` because iCloud sync flags in-tree `.venv/` files as hidden, breaking the editable-install `.pth`), the `tea-scrape` CLI runs, `alembic upgrade head` materializes all 10 tables with trigram + HNSW indexes, and the shared `HttpClient` / `JsonlWriter` / `RunTracker` / structlog helpers ship with 26 passing tests. Postgres runs in the `tea-postgres` Docker container on `localhost:5432` (`docker compose up -d` from `tea-scrapers/` if a fresh session finds it stopped). Next unblocked task is **ingest step 3 (Shopify scraper)** per `ROADMAP.md:31`: generic Shopify implementation + vendor config loader (scrapers spec §6.1) + end-to-end VCR test against `yunnansourcing.us` — owned by scraper-engineer + qa-engineer.
