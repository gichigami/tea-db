# Tea Testing V1: Specification & Architecture

**Status:** Stub (awaiting draft from qa-engineer)
**Audience:** Implementation agents writing tests across scrapers, loaders, normalizers, extraction, recommender, and UI
**Parent doc:** `tea_scrapers_v1_spec.md` §9 (testing strategy for scrapers); `tea_rec_engine_design_v2.md` §8 week 1.4 (hand-labeled set for extraction)
**Sibling specs:** Every other `specs/*.md` defines what testing looks like for its area; this spec unifies the cross-cutting conventions.
**Last updated:** 2026-05-16

---

## Why this spec exists

`tea_scrapers_v1_spec.md` §9 covers test layers for scrapers only. The full system needs:
- Coverage taxonomy that spans scrapers, loaders, normalizers, extraction, recommender, UI
- Hand-labeled set spec (the ~200 teas referenced in design §8 week 1.4) — selection rules, labeling protocol, validation metric
- CI integration: what runs per-PR, nightly, manual
- VCR cassette discipline beyond scrapers (extraction LLM calls? embedding API calls?)
- Property-based testing where canonical ID matching needs it
- Playwright e2e coverage for the historical → current pivot golden path

qa-engineer authors this spec via `/spec-draft testing`.

---

## To be drafted (per `specs/spec-template.md`)

- [ ] §1 Architecture Overview — test layer taxonomy, where each layer lives, how they relate
- [ ] §2 Tooling — pytest, vcrpy, hypothesis (property tests), Playwright, coverage.py
- [ ] §3 Project Structure — `tests/unit/`, `tests/integration/`, `tests/golden/`, `tests/e2e/`, `tests/fixtures/`
- [ ] §4 Conventions — fixture naming, secret filtering, hashing PII before recording
- [ ] §5 Per-layer spec — unit / integration / golden / hand-labeled / e2e
- [ ] §6 CLI — `pytest` invocations, marker conventions (`@pytest.mark.integration`, etc.)
- [ ] §7 Schema — n/a, except the hand-labeled set's YAML/JSON shape
- [ ] §8 Testing — testing the tests (golden refresh cadence; metamorphic checks)
- [ ] §9 Sequencing — unit + schema validation before integration; hand-labeled set before extraction batch run
- [ ] §10 Anti-Patterns — re-recording cassettes every test run; committing PII in cassettes; asserting on LLM output verbatim instead of structured fields; skipping tests "because the spec is clear"
- [ ] §11 Open Items — snapshot-test scope for UI components; property-based test scope for canonical matcher; CI runner choice (GitHub Actions vs local-first)

---

## Hard rules

- VCR cassettes filter request headers containing secrets before saving
- Author names from scraped sources hashed (sha256) BEFORE the cassette records the response; coordinate with scraper-engineer to enforce in `HttpClient`
- CLI exit codes (scrapers spec §7) are asserted in e2e: 0 = success, 1 = partial, 2 = terminal
- Hand-labeled set: ≥10 examples per L1 macro category from the ontology
