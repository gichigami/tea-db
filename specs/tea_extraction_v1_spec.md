# Tea Extraction V1: Specification & Architecture

**Status:** Stub (awaiting draft from ml-engineer)
**Audience:** Implementation agents building the LLM extraction pipeline from silver-layer text to structured `product_profile` rows
**Parent doc:** `tea_rec_engine_design_v2.md` §4 (extraction schema), §6 #1 (quote evidence), §7 #3 (cold-start via transfer learning)
**Sibling specs:** `tea_ontology_v1_spec.md` (vocabulary inputs), `tea_recommender_v1_spec.md` (downstream consumer), `tea_testing_v1_spec.md` (hand-labeled validation set)
**Last updated:** 2026-05-16

---

## Why this spec exists

The design doc commits to per-tea LLM extraction with structured pydantic output and the non-negotiable `quote_evidence` field. It does not yet specify:
- Which LLM(s), with what prompt structure
- How descriptions, Steepster notes, TeaDB posts, and Reddit comments are *combined* per product (or kept separate)
- Versioning discipline for prompts (`extraction_version` populated for every row)
- Re-extraction triggers (description changed? prompt version bumped? new community notes added?)
- Confidence calibration on `flavor_tags[].confidence`
- How extraction interacts with ontology-curator when it encounters an unknown label

ml-engineer authors this spec via `/spec-draft extraction`.

---

## To be drafted (per `specs/spec-template.md`)

- [ ] §1 Architecture Overview — silver record → prompt assembly → LLM call → pydantic parse → `product_profile` write
- [ ] §2 Tooling — model (Anthropic / OpenAI / both?), pydantic v2, optional caching
- [ ] §3 Project Structure — `src/tea_extract/` likely; coordinate with tech-lead
- [ ] §4 Conventions — prompt versioning, retry on parse failure, cost ceiling per run
- [ ] §5 Per-input-type specification — how to compose prompts for vendor-only vs vendor+Steepster vs vendor+Steepster+TeaDB
- [ ] §6 CLI / API — `tea-extract run --since YYYY-MM-DD` and similar
- [ ] §7 Schema — `product_profile` columns (defined in scrapers spec §8) + any aux tables
- [ ] §8 Testing — schema validation, hand-labeled validation set (~200 teas per design §8 week 1.4), F1 on flavor-tag set + MAE on ordinal axes
- [ ] §9 Sequencing — schema validation → small-sample prompt iteration → hand-labeled validation → batch run
- [ ] §10 Anti-Patterns — fields written without quote_evidence; rows without extraction_version; silent L3 invention; mixing vendor + community text without attribution
- [ ] §11 Open Items — which LLM, prompt cache strategy, how to attribute the source when multiple texts feed one extraction

---

## Hard rules from design doc

- `quote_evidence` populated for every field that gets a value (§4, §6 #1)
- `extraction_version` stored with every extraction row (`product_profile.extraction_version`)
- Embedding model for V1: `text-embedding-3-large` (1536 dim) — coordinate with §10 of design doc open question
- Cold-start largely solved via producer-style transfer learning (§7 #3); reflect this in prompt context (include producer's prior similar teas)
