# 0011. hybrid_v1 gets a front door — config/schema opt-in, no automatic row-count switch

**Status:** Accepted
**Date:** 2026-09-23
**Follows on from:** [0010](0010-hybrid-v1-tiered-runner-audit-no-front-door.md) (read-only audit that found the gap this closes)

## Context

ADR 0010 found that `Project/tiered_runner.py`'s hybrid_v1 large-table engine
was real, tested (42 tests), and already wired into the dispatch point in
`Project/main.py` — but nothing in the normal generate → run flow could ever
produce the `validation_plan.execution_strategy: hybrid_v1` key that turns it
on. The only way to use it was hand-editing an already-generated YAML file.
This ADR is the follow-up: give it a front door, without inventing a
row-count threshold the project has explicitly decided not to guess at yet
(see "What we deliberately did NOT do" below).

## Decision

Added `execution_strategy` as a first-class, typed, opt-in field on the plan
→ schema → YAML chain. Nothing about *how* hybrid_v1 runs changed —
`Project/tiered_runner.py` and `Project/main.py`'s dispatch (`should_dispatch_hybrid()`
in `Project/utils/utility.py`) are untouched. What changed is: how a table
gets marked eligible for it, and how early a mistake gets caught.

### 1. `CanonicalValidationPlan.execution_strategy` (`src/core/validation_plan.py`)

A new field, default `"standard"`. Round-trips through `to_dict()`/`from_dict()`
so it survives being written to and read back from the persisted plan JSON.

### 2. `PlanValidator` eligibility checks (`src/validation/plan_validator.py`)

This is the actual "front door" — it catches at **generation time** the two
ways hybrid_v1 refuses to run at **runtime**, so a table owner finds out
before generating output/plans/*.json and config YAML, not after a live run
against Postgres/Snowflake fails partway through:

| Check | Runtime behavior today (unchanged) | Now also caught at plan-validation time |
|---|---|---|
| Composite PK (`source_primary_keys` has >1 column) | `tiered_runner.py:577-582` raises `NotImplementedError` | `PlanValidator` adds an issue, generation is blocked |
| No `row_hash` spec configured | `should_dispatch_hybrid()` silently stays on the standard path (Tier 1 has nothing to hash with) | `PlanValidator` adds an issue: "hybrid_v1 requires a row_hash spec" |
| Unknown `execution_strategy` value (typo, e.g. `hybrid_v2`) | Nothing — `should_dispatch_hybrid()` just returns `False`, table quietly runs standard | `PlanValidator` adds an issue |

### 3. YAML generator writes the flag (`src/generated_queries/yaml_config_writer.py`)

`plan_intent` (the dict that becomes the `validation_plan:` YAML block) now
includes `execution_strategy`. Before this change, even a plan object that
had `execution_strategy` set programmatically would never see that value
reach the generated YAML — the generator silently dropped it. This was the
literal missing wire from ADR 0010.

### 4. Config schema catches typos instead of swallowing them (`src/validation/config_schema.py`)

`TableValidations` previously had `model_config = ConfigDict(extra="allow")`
with no typed model for the `validation_plan:` sibling block at all — any key
under it, valid or not, passed through unchecked. Added a
`ValidationPlanBlock` model with `execution_strategy: Literal["standard", "hybrid_v1"]`.
Run through `src/validate_cli.py`'s config-lint command (`validate_config_dir`),
a typo like `execution_strategy: Hybrid_V1` now fails lint with a clear
message instead of silently running the standard engine and leaving whoever
wrote it thinking hybrid_v1 was active.

### 5. Informational display only (`CanonicalValidationPlan.summary_lines()`)

Added one line, `Execution strategy: <value>`, to the plan summary that
already prints in the CLI (`src/validation_pipeline.py`'s `run_with_plan()`
step 6/7). No new UI selector was added — see below.

## Worked example — what a table owner actually does now

Before this change, to run table `bookings` under hybrid_v1 you had to:
1. Generate the YAML normally.
2. Open `config/bronze/data_validation/bookings.yaml` by hand.
3. Add `execution_strategy: hybrid_v1` under `validations.validation_plan`.
4. Hope you didn't typo it, and hope the table had a `row_hash` block and a
   single-column PK — nothing would tell you if not, until the run either
   silently ran standard (missing row_hash) or crashed 20 minutes into a
   300M-row Tier 1 pass (composite PK).

Now:
1. Set `plan.execution_strategy = "hybrid_v1"` and `plan.row_hash = RowHashSpec(columns=[...])`
   on the `CanonicalValidationPlan` before calling `write_from_plan()` /
   `generate_from_plan()`.
2. `PlanValidator.validate_or_raise(plan)` (already called in the generation
   flow) blocks generation immediately if the PK is composite or `row_hash`
   is missing, with a message naming exactly what's wrong.
3. The generated YAML now actually contains `execution_strategy: hybrid_v1`
   under `validation_plan:` — this part was silently broken before.
4. `Project/main.py` picks it up exactly as it already did (unchanged):
   `should_dispatch_hybrid()` sees `execution_strategy: hybrid_v1` plus a
   real (non-placeholder) `row_hash_validation` query pair, and routes that
   table's `data_validation` block to `tiered_runner.run_table_hybrid()`
   instead of the normal fetch-both-sides-into-pandas path.

Nothing in `src/validation_pipeline.py`'s `run_with_plan()` (the pipeline the
UI's "Run Validation" flow actually calls) sets `execution_strategy` or
`row_hash` today — so as of this change, every table generated through the
UI still defaults to `"standard"`, unchanged. The front door exists; nobody
walks through it automatically yet. That's deliberate — see below.

## What we deliberately did NOT do (and why)

**No automatic "large table → hybrid_v1" switch based on row count.**
`docs/large-table-scalable-architecture/README.md` (§O Phase 6, §P.8)
explicitly says this threshold is undecided pending real benchmark data and
warns against picking a round number like "10M rows" without evidence. That
warning is still true today — nothing in this change, or anywhere else in
the repo, has since produced that benchmark data. Two things would be needed
before automatic selection is even possible, neither of which exists:

1. **A row-count signal at plan-generation time.** Today's schema-extraction
   path (`src/sql_extractor/extractors.py`) does not fetch `reltuples` /
   `information_schema` estimates or run a `COUNT(*)`, so there is nothing
   in `src/validation_pipeline.py`'s `run_with_plan()` to threshold against
   even if a number were decided.
2. **A decided number**, informed by the ladder benchmark
   `docs/large-table-scalable-architecture/README.md` §L already proposes
   (1M / 10M / 50M / 100M / 200M / 300M rows, oracle vs. hybrid_v1) — not run
   yet.

Building an automatic switch on a guessed threshold would risk the exact
failure mode this ADR's eligibility checks exist to prevent elsewhere: a
table silently gets a different execution strategy than intended, and the
first sign of trouble is a runtime crash or (worse) a quietly wrong result.

**No UI selector.** The task explicitly asked to avoid a "complicated UI
selector," and since nothing today produces `row_hash` from the UI's
`run_with_plan()` path, a selector would let someone pick `hybrid_v1` for a
table that has no `row_hash` spec — which `PlanValidator` would then reject
anyway. A selector is only worth adding once there's a `row_hash`-generation
path from the UI to pair it with.

## Decision needed from you (not decided here)

If/when someone wants tables to pick up `hybrid_v1` automatically:
1. Decide the row-count threshold using the §L benchmark ladder against real
   Postgres/Snowflake data, or against the documented live-oracle run in §T.
2. Add a row-count lookup to the schema-extraction step (one query per
   table, cached in the plan) so `run_with_plan()` has something to compare
   against.
3. Only then does auto-selection become "wire the decided number to the
   eligibility checks already built here" — not a new subsystem.

Until that happens, `hybrid_v1` remains an explicit, validated opt-in for the
largest tables (an "expert escape hatch," per ADR 0010's second consequence
option) — which is itself a legitimate, documented end state, not a
placeholder for something broken.

## What this has to do with completeness/uniqueness/accuracy (for context)

This front door doesn't change *what* gets checked — `tiered_runner.py`
still reuses the same `canonicalize_frames` / `compare_indexed_frames`
primitives as the standard oracle path (ADR 0001), so accuracy and
consistency are unaffected either way. What this changes is purely *which
execution path* a table's `data_validation` block runs through:

- **PK-based tables** (the common case): Tier 1 hashes every row on both
  sides using the columns in `row_hash.columns` (or all mapped columns if
  left empty), classifies each PK key as `SOURCE_ONLY` / `TARGET_ONLY` /
  `HASH_MISMATCH` / `HASH_MATCH` without ever materializing full rows for
  matches, then Tier 2 re-fetches and does the full row-level compare only
  for the keys that disagreed. Completeness (`SOURCE_ONLY`/`TARGET_ONLY`)
  and accuracy (`HASH_MISMATCH` → full row diff) both still get reported.
- **PK-less tables**: Tier 1 hashes every row; if all hashes match on both
  sides it reports `PASS` for the whole table without ever re-fetching full
  rows. If anything disagrees, it refuses outright (`RuntimeError`) rather
  than guess at row-level detail with no key to re-fetch by — you'd re-run
  that table without `execution_strategy: hybrid_v1` to get the normal
  full row-level `PASS`/`FAIL`/`SOURCE_ONLY`/`TARGET_ONLY` CSV.
- **Composite-PK tables**: not supported by hybrid_v1 at all yet (refused,
  same as always) — this is exactly what `PlanValidator` now also catches
  before generation, per the table above.

## Consequences

- A table owner can now opt a table into hybrid_v1 through the normal
  plan/generation flow and get an immediate, specific error if it's
  ineligible — instead of a silent no-op or a runtime crash discovered later.
- A typo in `execution_strategy` in hand-written or generated YAML is now a
  lint failure (`validate_cli.py`'s config-lint command), not a silent
  fallback to the standard engine.
- Every table generated through the UI's `run_with_plan()` path is
  unaffected — still defaults to `"standard"`, exactly as before.
- The row-count-based automatic switch remains an open, explicitly-flagged
  decision — see "Decision needed from you" above. Anyone asking "why don't
  large tables just use hybrid_v1 automatically" should be pointed at that
  section and at `docs/large-table-scalable-architecture/README.md` §O
  Phase 6 / §P.8, not have a number invented for them.
