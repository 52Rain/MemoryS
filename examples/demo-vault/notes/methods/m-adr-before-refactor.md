---
id: m-adr-before-refactor
kind: method
scope: shared/methods
status: active
basis: owner_confirmed
summary: Write a 1-page ADR before cross-module refactors
use_when:
- refactor
- architecture
- migration
effective_from: '2026-01-15'
subject_key: shared/methods.adr-before-refactor
---

Before refactoring interfaces shared across 3 or more modules, draft a short ADR covering:
1. Why the current boundary fails;
2. The proposed target boundary and migration sequence;
3. Rollback criteria if verification fails.
