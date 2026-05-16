---
description: Draft a v1 spec for an area of the system that doesn't yet have one. Follows the shape of specs/tea_scrapers_v1_spec.md.
---

Usage: `/spec-draft <area>` where `<area>` is one of:
- `ontology` (ontology-curator owns)
- `extraction` (ml-engineer owns)
- `recommender` (ml-engineer owns)
- `reliability` (ml-engineer owns)
- `ui` (frontend-engineer owns)
- `testing` (qa-engineer owns)
- Or any other area the team has identified

Steps:

1. Read `specs/spec-template.md` — this is the canonical shape every v1 spec follows. It mirrors `specs/tea_scrapers_v1_spec.md`.
2. Read the existing stub at `specs/tea_<area>_v1_spec.md` (create from the template if missing).
3. Read the relevant sections of `tea_rec_engine_design_v2.md`:
   - ontology → §4 + §3 soft dimensions + §11 glossary
   - extraction → §4 (extraction schema, vocabulary)
   - recommender → §5 (display surfaces) + §9 (architecture) + §6 (differentiating features)
   - reliability → §4 (vendor calibration) + §6 #2
   - ui → §5 in full + §6 differentiating features
   - testing → §8 (V1 plan, week 1.4 hand-labeled set)
4. Delegate to the owning specialist via the Agent tool. The specialist drafts the spec, you (the orchestrator) review the result against the template shape before committing.

The drafted spec must include:
- All template sections (Status / Audience / Parent doc, Architecture, Tooling, Conventions, Module spec, CLI or API, Schema if relevant, Testing, Sequencing, Anti-Patterns, Open Items)
- A populated **Anti-Patterns** section — the section that protects future work
- A populated **Open Items** section — the section that surfaces unresolved decisions
- Citations back to `tea_rec_engine_design_v2.md` and any sibling specs

A spec without populated Anti-Patterns + Open Items is incomplete. code-reviewer will flag it.

After drafting, update `ROADMAP.md` to check the spec-drafting task and unblock the dependent implementation tasks.
