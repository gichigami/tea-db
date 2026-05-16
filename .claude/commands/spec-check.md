---
description: Audit current code against design-doc principles and scraper anti-patterns
---

Walk the repo and check for violations. Group findings by severity: **block / fix / nit**.

## From `specs/tea_scrapers_v1_spec.md` §11 (anti-patterns)

- Scraper writes to Postgres — look for `psycopg2` / `sqlalchemy` / `asyncpg` imports under `src/tea_scrapers/sources/`
- Filtering at scrape time — look for `if not …: continue` patterns that filter records before write
- Payload mutation — look for assignments to `payload[...]` or `del payload[...]` in source modules
- Direct `httpx.Client()` use outside `src/tea_scrapers/http/`
- Per-source retry logic — `tenacity` decorators or while-loops with exception catches in sources/
- `except Exception:` catches (any flavor)
- Browser automation (`playwright`, `selenium`) without an open justification in the PR / commit message
- Async outside justified contexts (sync sufficient for V1 per §11)
- Direct silver writes (skipping bronze) — look for normalize/ code that doesn't read from `raw_product_snapshot`

## From `tea_rec_engine_design_v2.md` §6 (differentiating features)

- Any flavor / mouthfeel / qì field stored without populated `quote_evidence`
- Any badge in the UI that isn't tappable to evidence
- `product_profile` rows without `extraction_version`
- Generative SVG fingerprint missing or non-deterministic (same input must produce same output)
- Color-only encoding in the UI (badges must have a non-color affordance too)

## From `specs/tea_scrapers_v1_spec.md` §8 (schema)

- Trigram index (`gin_trgm_ops`) missing on `product.canonical_name` if the canonical matcher exists
- HNSW index missing on `product_embedding.embedding`
- Missing `payload_hash` UNIQUE constraint on `raw_product_snapshot`

## From drafted module specs

For each `specs/*.md` that is drafted (not stub-only), apply its own Anti-Patterns section as additional checks.

## Output

A punch list. For each finding: file:line if known, the rule violated (with citation), severity, and a one-line description of the fix.

If the repo is still pre-implementation (no code under `src/`), report that explicitly and check only the spec drafts in `specs/` against `specs/spec-template.md`.
