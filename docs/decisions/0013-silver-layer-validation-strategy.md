# 0013. Silver-layer validation strategy: recompute-vs-materialize, not fuzzy column matching

**Status:** Proposed
**Date:** 2026-09-24

## Context

Bronze-layer validation (already built, already working) compares four
heterogeneous source systems (PostgreSQL, Athena, MSSQL, Redshift) against
Snowflake. Column names, types, and even table shapes differ across the
source→target boundary, so that pipeline has to *discover* the mapping —
exact match, fuzzy match, AI for the ambiguous remainder
(`src/validation_pipeline.py::run_with_plan()`, `src/ai/rule_planner.py`).

Silver is a different problem, not a scaled-down version of the same one:

- **Both sides are Snowflake.** Bronze table and Silver table live in the
  same warehouse. There is no source-DB connector to write, and
  `Project/db/factory.py::get_database()` already builds a `Snowflake`
  connector for any `db_type == "snowflake"` — nothing there cares whether
  that connector is playing the "source" or "target" role. Two Snowflake
  connectors, pointed at `BRONZE_*` vs `*_SILVER` databases, already works
  with existing code.
- **`Project/main.py` already accepts `--layer_type silver`**
  (`Project/main.py:36-40`) — the row-level compare engine, the CSV/YAML
  report format, exclusions, quality checks, hybrid_v1 tiering are all
  layer-agnostic already. Nothing in the execution engine needs to change.
- **The column mapping is not ambiguous — it's given.** The ETL tool
  (Coalesce.io) already knows, per output column, exactly which upstream
  column(s) it read and exactly what SQL expression it applied
  (`metadata.columns[].sources[].transform` +
  `.columnReferences[].nodeID/columnID`, see ADR 0014). Running fuzzy/AI
  matching on top of that would be re-deriving a fact we're handed for free,
  and could disagree with the real mapping — worse than not matching at all.

So bronze validation's hard problem (*what maps to what?*) doesn't exist for
silver. Silver's hard problem is different: **did the transform Coalesce's
metadata says it applied actually produce what's sitting in the Silver
table?**

## Decision

Silver validation is a **recompute-and-diff** check, not a
**select-and-diff** check:

1. Take the Silver node's own Coalesce metadata as the source of truth for
   what *should* be true.
2. Rebuild the SELECT the node's metadata describes — same FROM/JOIN
   (`sourceMapping[].join.joinCondition`), same per-column expression
   (`columns[].sources[].transform`, with bare passthrough columns treated
   as `transform = <the referenced column>`) — and run it **against the
   Bronze table already sitting in Snowflake**.
3. Compare that recomputed result to a **plain, unfiltered `SELECT *`
   against the materialized Silver table** — no filter on the Silver side,
   because whatever filtering/scoping happened is already baked into the
   metadata-driven query in step 2 (mirrors the "Consistency" data-quality
   dimension in `CLAUDE.md`: the filter lives once, in the metadata, and
   both the plan we validate and the transform Coalesce actually ran must
   agree with it).
4. Feed the result into the **same** row-level engine bronze already uses:
   `Project/main.py --layer_type silver`, same PK-based `PASS`/`FAIL`/
   `SOURCE_ONLY`/`TARGET_ONLY` CSV output, same exclusions
   (`config/exclusions.yaml` + table-specific), same base/learned rules for
   type normalization.

This catches the failure mode that a naive "does Silver look reasonable"
check can't: Coalesce's metadata describing one transform while the node
actually ran with `overrideSQL`, a stale materialization, or a macro that
expanded differently than expected.

### What's new vs. reused

| Piece | Status |
|---|---|
| Coalesce metadata → mapping/transform extraction | **New** — see ADR 0014 |
| `CanonicalValidationPlan` (`src/core/validation_plan.py`) | **Reused as-is.** `MatchMethod.CONFIGURED` already exists for "user/metadata-specified explicit mapping" — this is exactly that case, not a new enum value. |
| SQL generation (`src/generated_queries/ai_sql_generator.py`) | **Reused** — it already turns a plan's `ColumnMappingEntry` list into source/target SELECTs; Coalesce-derived entries feed the same path bronze uses today. |
| YAML generation (`src/generated_queries/yaml_config_writer.py`) | **Reused as-is.** |
| Row-level execution (`Project/main.py`, `Project/runner.py`) | **Reused as-is** — `--layer_type silver` already exists and is layer-agnostic. |
| Type normalization / exclusions / base+learned rules | **Reused as-is.** |
| Fuzzy/AI column matching (`src/ai/rule_planner.py`) | **Not used for silver.** Bypassed entirely — Coalesce's metadata already resolved this. |

## Alternatives considered

- **Treat Silver like another bronze source (fuzzy/AI-match Bronze columns
  to Silver columns).** Rejected — throws away a ground-truth mapping
  Coalesce already gives us, and risks the matcher guessing a different
  mapping than what Coalesce actually ran, producing a false "mismatch"
  that's a validator bug, not a data bug.
- **Diff Silver against Bronze directly (no recompute), same as bronze-vs-source
  today.** Rejected — that only validates "Silver looks like a scoped/renamed
  copy of Bronze," not "Silver equals what Coalesce's own metadata claims it
  computed." It would miss exactly the failure mode (metadata/execution
  drift) that matters for an ELT tool doing the transform, not us.
- **Build a general Coalesce-macro SQL interpreter to recompute every
  transform, including surrogate-key macros.** Rejected for v1 — real
  over-engineering for a first cut; see ADR 0014's column-classification
  rule (macro-computed columns are marked `skip_validation`, not faked).

## Consequences

- Gets easier: silver validation needs **zero new execution-engine code** —
  it's a new *generator* (Coalesce metadata → plan) feeding pipes that
  already exist and are already tested for bronze.
- Gets harder: the generator has to correctly parse Coalesce's JSON,
  including resolving `columnReferences` across node boundaries (a column
  can reference another node's column by UUID, not name) and deciding which
  columns are safely recomputable vs. must be skipped. ADR 0014 is the
  detailed spec for that.
- Watch for: any Silver node using `overrideSQL: true` or `isMultisource:
  true` — the metadata shape sampled so far (`INT_FACILITIES`) has neither
  set, so the extraction rules in ADR 0014 haven't been validated against
  those cases yet. Treat them as unsupported until a real sample is seen.
- **Gold layer strategy is explicitly out of scope and undecided.** Nothing
  here assumes what Gold validation looks like — don't extrapolate this ADR
  to Gold. That's a separate decision for later, once Silver is running.
