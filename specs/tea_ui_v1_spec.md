# Tea UI V1: Specification & Architecture

**Status:** Stub (awaiting draft from frontend-engineer)
**Audience:** Implementation agents building the dual-surface UI (shoppable + reference), tea card, comparison views, and the historical→current pivot flow
**Parent doc:** `tea_rec_engine_design_v2.md` §5 (display), §6 (differentiating features)
**Sibling specs:** `tea_recommender_v1_spec.md` (API contract), `tea_ontology_v1_spec.md` (L1 color mapping, Hanzi labels), `tea_extraction_v1_spec.md` (provides quote_evidence for badges)
**Last updated:** 2026-05-16

---

## Why this spec exists

The design doc commits to a tea card with 8 specific elements, five comparison views, dual-surface display, and a generative SVG fingerprint that is the marketing-lead feature. It does not yet specify:
- Framework choice (design §10 leans Next.js for V1; not yet committed)
- The fingerprint determinism algorithm (input field hashing → SVG path generation)
- Component decomposition and state model
- API client and caching strategy for the recommender service
- Accessibility minimums beyond color coding
- Mobile / responsive treatment

frontend-engineer authors this spec via `/spec-draft ui`.

---

## To be drafted (per `specs/spec-template.md`)

- [ ] §1 Architecture Overview — app shell, routing, server/client split, data flow
- [ ] §2 Tooling — framework, state lib, charting lib (radar / scatter / sensory matrix)
- [ ] §3 Project Structure — `web/` or `ui/` at repo root
- [ ] §4 Conventions — component naming, accessibility checks, test scaffolding
- [ ] §5 Component inventory:
  - `Card`, `BadgeRow`, `MouthfeelPill`, `QiBadgeCluster`, `AgingStatePill`, `ProvenanceLine`, `DataQualityBadge`, `AvailabilityPill`
  - `RadarOverlay`, `SensoryMatrix`, `GalaxyScatter`, `VendorConsensus`, `AgingTrajectory`
  - `PivotPanel` (historical → current)
  - `FilterRail`
  - `FingerprintSVG`
  - `PalateVectorView`
  - `ProducerBodyOfWork`
- [ ] §6 API contract with recommender (coordinate with ml-engineer)
- [ ] §7 Schema — none server-side; client state shape if Redux/Zustand
- [ ] §8 Testing — Playwright golden path for pivot; component snapshot tests where pixel-stable
- [ ] §9 Sequencing — Card → BadgeRow with evidence wiring → RadarOverlay → PivotPanel → galaxy + matrix views → fingerprint → palate vector
- [ ] §10 Anti-Patterns — badge rendered without evidence; color-only encoding; prose where structured fields belong; fingerprint non-determinism
- [ ] §11 Open Items — Next.js vs Remix vs Astro; web-only V1 vs iOS path (design §10); palate-vector storage + privacy (design §10)

---

## Hard rules from design doc

- Every badge tappable for evidence; if `quote_evidence` is missing, the badge is not rendered (§6 #1)
- Generative fingerprint is deterministic from the structured profile (§6 #6)
- Provenance line uses structured fields, not prose (§5)
- Shoppable surface is the default; reference is a toggle (§5)
- Personal palate vector is surfaced explicitly to the user, not hidden (§6 #3)
