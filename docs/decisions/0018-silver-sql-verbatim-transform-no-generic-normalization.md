# 0018. Silver recompute SQL must use the Coalesce-declared transform verbatim — bypass the generic Bronze normalization wrapper entirely

**Status:** Accepted
**Date:** 2026-09-24
**Follows on from:** [0013](0013-silver-layer-validation-strategy.md), [0014](0014-coalesce-metadata-extraction-rules.md), [0015](0015-silver-v1-schema-diff-gate-and-deferred-pk-resolution.md), [0017](0017-silver-layer-specialist-agent-added.md)
**Corrects:** the SQL-generation behavior shipped in the 0015 implementation pass

## Context

The user reviewed a real hand-written example of the Bronze-side "recompute"
SQL they expect for a Coalesce node (`INT_FACILITIES`) and it does not match
what the current implementation produces. Two things were checked against the
actual code (both confirmed, not assumed):

1. **Current behavior is wrong for Silver.** `coalesce_plan_builder.build_plan()`
   feeds its `CanonicalValidationPlan` through the same generator path Bronze
   uses (`sql_query_generator.py` → `ai_sql_generator.py` → `base_rules.py`),
   which wraps *every* column in
   `COALESCE(CAST(col AS STRING), '<<NULL>>')` — a generic string-cast +
   null-placeholder normalization that exists so **structurally different**
   source/target types (Postgres `hstore` vs Snowflake `VARIANT`, etc.) can be
   compared as strings. Silver is Snowflake-to-Snowflake; that heterogeneity
   problem doesn't exist here, and the wrapper actively fights the user's
   actual requirement: **use the Coalesce metadata's own declared transform,
   exactly as written — if it trims, we trim; if it doesn't, we don't add
   anything.**
2. **The wrapper isn't needed for NULL-safety either.** Traced the real
   row-comparison code: `Project/utils/row_compare.py`'s `_cell_str()`
   (called from `compare_indexed_frames`, which `Project/main.py:414` calls
   directly on the raw `source_df`/`target_df`) already maps Python
   `None`/`NaN` to a literal `"<<NULL>>"` string **in Python**, independent of
   whatever the SQL selected, and already does the int/float/Decimal→string
   coercion for comparison. So "handle nulls so it won't fail" is already
   satisfied downstream — the SQL doesn't need to add its own null-placeholder
   logic for the *comparison* to work. The only legitimate reason to touch
   NULL-handling in the generated SQL is to prevent a **SQL execution error**
   from a specific expression (e.g. string concatenation choking on NULL) —
   not to help the comparator, and not as a blanket rule.

The user re-confirmed the intended end-to-end shape, which this ADR now
records precisely so there's no more back-and-forth re-deriving it:

> Given two Coalesce IDs (workspace ID + node ID) → fetch that node's
> metadata → run schema validation (0015, already built and correct) →
> generate exactly two SQLs, one against the Bronze table (source) and one
> against the Silver table (target), each using **only** the column names and
> transformations the metadata declares, nothing added or inferred by us →
> hand those two SQL strings to the existing YAML writer to produce the YAML
> config, same as Bronze does.

## Decision

**Silver SQL generation gets its own lightweight emission path — it does not
route through `base_rules.py`'s type-normalization rules at all.**

1. For each `ColumnMappingEntry` the plan builder already classifies
   (0014 §3), emit the column verbatim:
   - **Passthrough**: `"<bronze_alias>"."<resolved_column>" AS "<target_column>"`
     — no cast, no wrapper, exactly the referenced column.
   - **Recomputable expression**: the resolved transform string itself
     (already stored verbatim in `source_column` per 0014/0016), `AS
     "<target_column>"` — no re-wrapping.
   - **Macro-skip**: **not selected**, but the column list keeps a SQL
     comment at that position showing the literal Coalesce transform and why
     it's skipped (matches the user's own example's
     `-- FACILITY_KEY: PENDING - needs ids_to_surrogate_key macro definition.
     Coalesce transform: {{ ... }}` style) — visible, not silently dropped.
   - **Non-deterministic**: still excluded from value comparison per 0014 §3
     (existence/NOT-NULL check only, unchanged from today).
2. **No blanket NULL-placeholder/TRIM/CAST-to-STRING wrapper is added by us,
   anywhere, for Silver.** If the metadata's own transform already contains a
   `TRIM`/`CAST`/`COALESCE`, that's what gets emitted, because it came from
   the transform string, not because we added it. Rely on
   `row_compare.py`'s existing Python-side NULL/type-coercion handling
   (already layer-agnostic, already used by Bronze via the same call from
   `Project/main.py`) — no change needed there.
3. **Target (Silver) SQL is a plain, unfiltered `SELECT` of the declared
   Silver columns** — no transform applied (nothing to recompute on that
   side), matching 0013 step 3 exactly (already decided, unchanged).
4. Build the two resulting SQL strings directly into a `ValidationQuerySet`
   (the shape `yaml_config_writer.py` already accepts — confirmed it takes an
   already-built query set, not raw plan-to-generator wiring) and hand that to
   the existing `YAMLConfigWriter.write_from_plan()` unchanged. This is the
   only reuse point that survives from 0015's implementation — the SQL
   *generation* step (`SQLQueryGenerator.generate_from_plan()` /
   `AISQLQueryGenerator`) is **not used for Silver at all** going forward;
   those stay Bronze-only.

## Resolved: no `ORDER BY`

User confirmed 2026-09-24: skip it. `row_compare.py` doesn't need pre-sorted
input, so an `ORDER BY` would be cosmetic-only cost with no correctness
benefit — not worth adding.

## Alternatives considered

- **Keep routing through `base_rules.py` but add a "verbatim" rule type that
  skips the wrapper.** Rejected — the rule-based path
  (`get_rule_for_type(source_type, target_type)`) exists to pick a
  *type-pair-specific* wrapper; Silver's requirement is "no wrapper, ever,"
  which is simpler to express as "don't call the rule engine" than as a new
  rule that always no-ops.
- **Patch `base_rules.py`'s existing wrapper to be conditional on
  `db_type == "snowflake" and target_db_type == "snowflake"`.** Rejected —
  that would silently change Bronze's Snowflake-target behavior too
  (Bronze's target is always Snowflake already), risking a real regression in
  the tested Bronze path for an unrelated Silver requirement.

## Consequences

- Gets easier: Silver SQL is now a direct, auditable reflection of the
  Coalesce metadata — a DQE reviewer (or the user) can diff the generated SQL
  against the node's own `transform` strings column-by-column, which is
  exactly the trust model ADR 0013 was built around.
- Gets harder: Silver now has its own (small) SQL-emission function instead of
  reusing `AISQLQueryGenerator` — one more code path to keep in sync if the
  `ValidationQuerySet` shape `yaml_config_writer.py` expects ever changes.
- Watch for: this reverses part of what 0015 shipped (the plan was being fed
  into the shared Bronze generator, which was wrong for this layer) — once
  implemented, re-run `test_coalesce_plan_builder.py` and add a new assertion
  on the literal generated SQL text (not just the plan object) so this
  regression can't silently reappear.
