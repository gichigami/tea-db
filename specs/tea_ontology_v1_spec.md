# Tea Ontology V1: Specification & Architecture

**Status:** Stub (awaiting draft from ontology-curator)
**Audience:** Implementation agents building the flavor vocabulary, the synonym map, and the soft-axis schemas
**Parent doc:** `tea_rec_engine_design_v2.md` §3 (soft dimensions), §4 (Vocabulary Normalization), §11 (glossary)
**Sibling specs:** `tea_extraction_v1_spec.md` (consumes the ontology), `tea_ui_v1_spec.md` (renders L1 color-coding and Hanzi/pinyin)
**Last updated:** 2026-05-16

---

## Why this spec exists

The design doc commits to a three-level flavor hierarchy with multilingual surfaces and to qì + mouthfeel + huí gān + shēng jīn as separate soft axes. It does not yet specify:
- File format and on-disk layout of the lexicon
- Seeded L1 + L2 + initial L3 set (~80-120 nodes targeted for V1)
- Synonym-map maintenance rules
- Hanzi + tone-marked pinyin contribution standards
- How extraction-discovered labels are routed back to the curator

ontology-curator authors this spec via `/spec-draft ontology`. Until then, this file is a placeholder.

---

## To be drafted (per `specs/spec-template.md`)

- [ ] §1 Architecture Overview — where the lexicon file lives, who reads it (extraction, UI, recommender)
- [ ] §2 Tooling — file format (YAML vs JSON), validation library, build step if any
- [ ] §3 Project Structure — repo path for the lexicon, helper scripts
- [ ] §4 Conventions — Hanzi forms, pinyin tone marks, synonym list discipline
- [ ] §5 Per-node schema — L1, L2, L3 with multilingual fields, tradition_hints
- [ ] §6 CLI / API — how other components consume the ontology
- [ ] §7 Schema — if any ontology data lives in Postgres
- [ ] §8 Testing — invariants (each L3 has exactly one L2 parent; every node has Hanzi + pinyin where culturally apt)
- [ ] §9 Sequencing — seed L1 → seed L2 → seed L3 → wire to extraction
- [ ] §10 Anti-Patterns — multi-parent L3, English-only labels, silent invention of new L3 by extraction
- [ ] §11 Open Items — build vs buy (design §10), Tier D scope, region/cultivar dimension treatment

---

## Open items already known (design §10)

- **Build vs buy on the ontology**: curate tea-specific or fork an existing flavor lexicon (SCA-style, wine lexicons)? Currently leaning curate. Arbiter: ontology-curator with tech-lead sign-off. Blocks ROADMAP item V.1.
