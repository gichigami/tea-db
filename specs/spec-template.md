# <Area Name> V1: Specification & Architecture

**Status:** [Stub | Draft | Approved]
**Audience:** Implementation agents building <area> V1
**Parent doc:** `tea_rec_engine_design_v2.md` (sections: …)
**Sibling specs:** [list relevant `specs/*.md`]
**Last updated:** YYYY-MM-DD

This spec is the source of truth for V1 <area> implementation. When in doubt, prefer this doc's conventions over generic patterns. Deviate only with explicit justification.

---

## 1. Architecture Overview

What this component is, where it sits in the larger system, and what it owns end-to-end. Include an ASCII diagram if there are >2 moving parts.

```
[upstream]  ────→  [this component]  ────→  [downstream]
```

Separation rationale: why the boundary lives where it does.

---

## 2. Tooling

| Layer | Choice | Rationale |
|---|---|---|
| Language | … | … |
| … | … | … |

**Tools NOT to add without justification:** …

---

## 3. Project Structure

Where the code lives, mapped to repo paths. Mirror the shape of `tea_scrapers_v1_spec.md` §3.

```
src/<module>/
├── …
```

---

## 4. Conventions

Cover whichever of these apply:

- Configuration (env vs YAML; what goes where)
- HTTP / API client conventions
- Logging events emitted (structured log shape)
- Error handling (what's a recoverable error vs terminal)
- Idempotency contract (re-run safety)
- Versioning (e.g. `extraction_version`, schema version)

---

## 5. <Component>-specific specification

The bulk of the spec. For each sub-component / source / module / surface, document:
- Input shape
- Output shape
- Edge cases / gotchas
- Implementation skeleton (Python class outline or equivalent)

---

## 6. CLI Interface or Public API

The contract the rest of the system calls into. Exit codes, request/response shapes, error responses, auth.

---

## 7. Schema (if relevant)

Postgres DDL or other persistent schema. Indexes and constraints belong here, not buried in code.

---

## 8. Testing Strategy

- Unit
- Integration (fixtures, replay strategy)
- Golden data
- E2E
- Validation metric (if ML)

Fixture conventions: where they live, naming, refresh cadence.

---

## 9. Implementation Sequencing

Numbered steps. Each step must be independently validatable before moving on.

1. **Step 1**: …
2. **Step 2**: …
3. …

State which steps unlock which downstream tasks.

---

## 10. Anti-Patterns

Things implementers should NOT do. Each bullet is a concrete don't, not a vague principle. This section is what protects future work — populate it carefully.

- **Don't …** — because …
- **Don't …** — because …

---

## 11. Open Items For Implementer

Unresolved decisions and verifications needed. Each item names:
- The question
- The current leaning (if any)
- Who arbitrates
- Whether it blocks a roadmap item

- **<question>** — current leaning: …. Arbiter: …. Blocks: ROADMAP.md item N? yes / no.
