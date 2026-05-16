# Vendor Reliability Scoring V1: Specification & Architecture

**Status:** Stub (awaiting draft from ml-engineer)
**Audience:** Implementation agents building the per-producer reliability score that calibrates vendor descriptions against community ground truth
**Parent doc:** `tea_rec_engine_design_v2.md` §4 (vendor description quality calibration), §6 #2 (vendor reliability scoring)
**Sibling specs:** `tea_extraction_v1_spec.md` (extraction signal), `tea_recommender_v1_spec.md` (consumer of confidence), `tea_ui_v1_spec.md` (renders the confidence badge)
**Last updated:** 2026-05-16

---

## Why this spec exists

This feature was deferred in design v1 and promoted to V1 in v2 because the historical vendor catalog (now in scope) provides the calibration set: each producer has discontinued products with both vendor descriptions and independent community signal on Steepster.

The design doc does not yet specify:
- The exact scoring function (cosine over extracted profiles? KL over flavor-tag distributions? something more interpretable?)
- The minimum sample size per producer to compute a score (and what to display below that threshold)
- How the score updates when new community signal arrives
- How the score enters the recommender ranking (weight on vendor-only profiles, or just a UI badge?)

ml-engineer authors this spec via `/spec-draft reliability`.

---

## To be drafted (per `specs/spec-template.md`)

- [ ] §1 Architecture Overview — what reads the score, what produces it, refresh cadence
- [ ] §2 Tooling — leans Python + scikit-learn or just pure pandas-free numpy; no new heavy deps
- [ ] §3 Project Structure — `src/tea_reliability/` or fold into `src/tea_extract/`?
- [ ] §4 Conventions — minimum N per producer, missing-data behavior, version bumps trigger recompute
- [ ] §5 Scoring spec — input (vendor extraction vs community extraction per discontinued product), method, output (0-1 reliability per producer)
- [ ] §6 CLI / API — `tea-reliability compute` and read endpoint or library function
- [ ] §7 Schema — `producer_reliability` table or column on `producer`?
- [ ] §8 Testing — synthetic producers with known alignment / misalignment
- [ ] §9 Sequencing — depends on extraction producing structured profiles for both vendor + Steepster paths; runs after both are in place
- [ ] §10 Anti-Patterns — using current-product community signal in the score (creates circularity); penalizing producers without enough history vs marking the score as low-confidence
- [ ] §11 Open Items — how the score enters ranking (weight vs annotation-only); what "low confidence" looks like in the UI

---

## Hard rules from design doc

- White2tea-style high-prose calibration must be possible (§4)
- An unknown vendor calibrates low until proven otherwise (§4)
- The score is surfaced as a confidence indicator on every product (§6 #2)
- This is a V1 Week 2 deliverable (design §8, item 2.4); don't let it slip into V2
