# Tea Recommender V1: Specification & Architecture

**Status:** Stub (awaiting draft from ml-engineer)
**Audience:** Implementation agents building the recommender service (shoppable mode, reference mode, historical→current pivot)
**Parent doc:** `tea_rec_engine_design_v2.md` §5 (display surfaces, comparison views), §6 (differentiating features), §9 (architecture sketch)
**Sibling specs:** `tea_extraction_v1_spec.md` (produces the profiles), `tea_ui_v1_spec.md` (consumes the API), `tea_reliability_v1_spec.md` (provides per-product confidence)
**Last updated:** 2026-05-16

---

## Why this spec exists

The design doc commits to a recommender service with shoppable + reference modes, the historical→current pivot as the killer feature, "why this rec" annotations, similarity by mouthfeel + intensity + qì vectors, and confidence-bounded aging prediction. It does not yet specify:
- Service shape (FastAPI? library imported by the frontend?)
- The exact similarity function across structured + embedding signals (weights, normalization, axis pinning)
- How `data_quality_tier` enters the ranking (filter? penalty? confidence label only?)
- API contract with the UI (parameters, response shape, pagination, caching)
- How the "why this rec" annotation is generated (axis-difference report off the same vectors)
- Aging trajectory output format (point estimate + CI per axis? sampled curves?)

ml-engineer authors this spec via `/spec-draft recommender`.

---

## To be drafted (per `specs/spec-template.md`)

- [ ] §1 Architecture Overview — service layout, query path, caching
- [ ] §2 Tooling — FastAPI vs library, pgvector queries, ranking implementation
- [ ] §3 Project Structure — `src/tea_recommender/`
- [ ] §4 Conventions — request logging, pagination defaults, cache invalidation on new snapshot
- [ ] §5 Per-endpoint spec:
  - `/similar/{product_id}?mode=shoppable|reference`
  - `/pivot/{product_id}` (historical → current, the killer feature)
  - `/palate/{user_id}` (personal palate vector)
  - `/aging-trajectory/{product_id}`
  - `/producer/{producer_id}/body-of-work`
- [ ] §6 CLI / API — full API contract
- [ ] §7 Schema — read paths over `product_profile`, `product_embedding`, `product_snapshot`
- [ ] §8 Testing — ranking regression tests against golden expectations; pivot golden path
- [ ] §9 Sequencing — filter+score v1 → embedding blend → reliability weighting → "why this rec" annotation → aging trajectory
- [ ] §10 Anti-Patterns — forking the code path between shoppable / reference instead of parametrizing; ranking that ignores `data_quality_tier`; hiding the N-of-comparison in aging trajectory output
- [ ] §11 Open Items — exact similarity weights, embedding model revisit threshold, palate vector storage + privacy (design §10)

---

## Hard rules from design doc

- Shoppable mode hard-filters on availability; reference mode does not (§5, §9)
- Both modes share the underlying similarity computation; parametrize the filter, don't fork (§9)
- Historical → current pivot is a first-class endpoint, not an afterthought (§5, §6 #7)
- Every recommendation includes a "why" annotation in axis terms (§6 #4)
- Aging prediction includes confidence interval AND N-of-comparison (§6 #5)
