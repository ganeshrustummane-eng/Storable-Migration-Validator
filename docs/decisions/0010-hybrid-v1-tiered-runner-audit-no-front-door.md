# 0010. Audit: hybrid_v1 tiered runner — wired and tested, but has no front door

**Status:** Investigation only — no code changed
**Date:** 2026-09-23

## What you asked

While browsing the generated knowledge graph (`graphify-out/`), the nodes for
the hybrid_v1 / tiered-runner / row_hash canonicalization / quality-check /
"200M-row bottleneck" design looked isolated — disconnected from the rest of
the graph, as if it might be documentation-only scaffolding rather than
something actually implemented and reachable. You asked for a read-only
audit of whether it's really wired in, and why it looks isolated — no
implementation, just findings, into this decision log.

## What was checked

Full read-only trace of `Project/tiered_runner.py`, its dispatch point in
`Project/main.py`, the predicate in `Project/utils/utility.py`, the YAML
schema/validator (`src/validation/config_schema.py`, `plan_validator.py`),
the `CanonicalValidationPlan` dataclass (`src/core/validation_plan.py`), the
YAML generator (`src/generated_queries/yaml_config_writer.py`), the UI
(`webapp/app.py`), the test suite (`Project/test_tiered_runner.py`,
`Project/test_hybrid_dispatch.py`), and the design docs
(`docs/decisions/0001-pyspark-vs-streaming-chunking.md`,
`docs/large-table-scalable-architecture/README.md`).

## Findings

**The execution engine is real, live, and tested — not orphaned code.**

- `Project/main.py:270-303` calls `should_dispatch_hybrid()`
  (`Project/utils/utility.py:32-54`) for every table/validation, and on
  `True` dispatches to `Project/tiered_runner.py:run_table_hybrid()`
  (tiered_runner.py:570-779).
- Tier 1 (tiered_runner.py:590-609) does a cheap SQL-side row-hash pass
  (`_collect_hash_multimap`, tiered_runner.py:107-129) to classify keys
  without materializing full rows. Tier 2 (tiered_runner.py:699-719) re-fetches
  only the disagreeing keys and reuses the **same** canonicalization/compare
  primitives as the default path — `canonicalize_frames`
  (`Project/utils/semantic_normalize.py`) and `compare_indexed_frames`
  (`Project/utils/row_compare.py`) — so there is no second, diverging
  comparison engine, per the design intent in ADR
  [0001](0001-pyspark-vs-streaming-chunking.md).
- 42 tests pass right now (`Project/test_tiered_runner.py`,
  `Project/test_hybrid_dispatch.py`), including a differential test against
  the non-hybrid oracle, plus a documented live oracle-vs-hybrid_v1 run
  against real Postgres/Snowflake in
  `docs/large-table-scalable-architecture/README.md` (§T, line 625) that
  found and fixed six real divergences.
- Explicit, deliberate scope limits: single-column PK only; composite PK
  refused outright (tiered_runner.py:577-582); PK-less tables only pass
  through if Tier 1 finds zero disagreement, otherwise it raises rather than
  guess (tiered_runner.py:611-626).

**The confirmed gap — this is why it looks isolated in the graph:**

There is no producer of the trigger key anywhere in the surface a user
actually touches. `execution_strategy` does not exist on
`CanonicalValidationPlan` (`src/core/validation_plan.py:271-349`), is absent
from `src/validation/config_schema.py` and `plan_validator.py`, is never
written by the YAML generator's `plan_intent` block
(`src/generated_queries/yaml_config_writer.py:194-208, 470-477`), and has
zero references anywhere in `webapp/app.py`. The generator *does* auto-emit
the sibling `row_hash_validation:` block when a `row_hash` plan spec exists
(yaml_config_writer.py:508-526) — but not the `execution_strategy:
hybrid_v1` flag that actually activates hybrid dispatch.

So today, hybrid_v1 is reachable only by hand-editing a
already-generated YAML to add `execution_strategy: hybrid_v1` under
`validation_plan:`. Nothing in the normal generate → run flow (UI or CLI)
can produce that key on its own. That's a real, narrow disconnect — not
dead code, but **an engine with no front door yet** — and it's exactly the
kind of gap that would show up as an isolated cluster in a knowledge graph
built from actual code references, since nothing in the reachable
UI/config-writer graph points at it.

## Consequences (for you to decide on, nothing done yet)

- If hybrid_v1 is meant to be usable beyond manual YAML edits, the missing
  piece is a plan/schema field (`CanonicalValidationPlan.execution_strategy`
  or equivalent) plus a writer change in `yaml_config_writer.py` and/or a UI
  toggle in `webapp/app.py` — not a rewrite of `tiered_runner.py` itself.
- If hybrid_v1 is intentionally an expert/manual-opt-in escape hatch for
  the largest tables (not meant to be UI-driven), no action is needed —
  worth stating that explicitly somewhere (this ADR, or a short note in
  `docs/large-table-scalable-architecture/README.md`) so the next person
  doesn't mistake "no front door" for "not implemented."
