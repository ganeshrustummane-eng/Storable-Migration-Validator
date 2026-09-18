---
name: normalization-and-exclusions
description: "Use when a column's source and target types don't match structurally (hstore vs VARIANT, JSONB vs VARIANT, arrays, timestamps, booleans) and need canonical comparison, or when deciding what should never be compared at all (Fivetran metadata columns, global/system/table-specific exclusions). Files: Project/utils/semantic_normalize.py, Project/utils/quality_checks.py, config/*_exclusions.yaml, src/core/skip_classifier.py."
---

# Semantic normalization and exclusions

Two separate problems that get confused easily — keep them separate:

1. **Normalization**: two columns *should* be compared, but their raw representations differ because of the type mapping used during migration (Postgres `hstore` → Snowflake `VARIANT`, `jsonb` → `VARIANT`, etc.). Fix: canonicalize both sides to the same shape before comparing, never compare raw strings.
2. **Exclusion**: a column should *never* be compared at all — it's migration-tooling metadata (`_FIVETRAN_SYNCED`, `_FIVETRAN_ID`, ...), not business data. Fix: remove it from the mapping before SQL/YAML generation, don't normalize it.

Getting these swapped (trying to "normalize" a Fivetran column, or silently excluding a real business column because its type looked unusual) produces validation results that look clean but are wrong — a false pass, which is worse than a false fail because nobody investigates it.

## 1. Normalization — `Project/utils/semantic_normalize.py`

Read this file before changing type-comparison logic anywhere else. Key facts:

- **History matters**: an earlier version did canonicalization in SQL (recursive CTEs, `LISTAGG`). It was abandoned after hitting real, documented bugs — collation-order mismatches between engines, `LISTAGG` treating NULL and empty string the same, `jsonb_each()` erroring on top-level JSON arrays/scalars. Don't reintroduce SQL-side canonicalization to "optimize" this — the Python approach exists *because* the SQL approach was proven wrong, not because nobody tried.
- **Symmetry is the whole point**: `canonicalize_frames()` applies the *same* canonicalization function to both the source and target dataframe. If you add a new type case, add it once in the shared function — never write a source-side version and a target-side version separately, even if they look like they'd converge.
- **Pipeline**: `canonicalize_value()` → `_parse_document()` (tries `json.loads`, falls back to a hand-written `parse_hstore_text` scanner — this hand-written parser exists because a regex-based hstore parser had a documented bug with escaped quotes inside values; don't replace it with a "simpler" regex) → `_canonicalize_node()` (sorts dict keys, preserves array order, normalizes booleans to Python `True`/`False` tokens, rounds decimals to 2dp via `Decimal`) → `_serialize()`.
- `NULL` passes through untouched as `<<NULL>>` on both sides — don't let a normalization change accidentally turn a real NULL into an empty object/string, that silently breaks completeness checks.
- Tests: `Project/utils/test_semantic_normalize.py` (~25 cases, including the escaped-quote hstore case, empty-object/empty-hstore symmetry, top-level arrays/scalars). Run these after any change here — they encode real bugs that were already found and fixed once.
- `Project/utils/quality_checks.py` is a separate, smaller, config-gated layer (null-rate tolerance, distinct-count tolerance, aggregate tolerance, sample-hash percent, expected grain) — this is where a *tolerance* belongs (e.g. "0.1% null-rate drift is fine"), not inside `semantic_normalize.py`. Keep the "make things comparable" logic and the "how much drift is acceptable" logic in these two separate files.

### Adding a new type pair

1. Check if `_canonicalize_node()` already handles the target shape generically (dict/list/scalar) — most new type pairs don't need new code, just a new entry point into the existing recursive canonicalizer.
2. If it's genuinely new (e.g. a new array-like type), add a test to `test_semantic_normalize.py` first with the actual tricky case (empty value, NULL, nested structure) before writing the implementation.
3. Never special-case a Fivetran column here — it should have been excluded already (see below); if you find yourself normalizing `_FIVETRAN_*`, that's a sign the exclusion step was skipped upstream.

## 2. Exclusions — three tiers, one source of truth for Fivetran

The exclusion system has three levels, checked in this order:

1. **Global** — `config/exclusions.yaml`: applies to every source regardless of DB type. Mostly Fivetran metadata patterns.
2. **System (per-source-DB)** — `config/postgresql_exclusions.yaml`, `mssql_exclusions.yaml`, `athena_exclusions.yaml`, `redshift_exclusions.yaml`: same Fivetran patterns plus DB-specific extras (e.g. MSSQL's rowversion `UTS`/`uTS` columns). These 5 files are ~90% duplicate content today — if you're touching exclusions and the change applies to all sources, prefer editing the shared pattern once (or consolidating the files) over copy-pasting into all 5.
3. **Table-specific** — set at runtime in the UI (Exclusions tab / batch generation form), not stored in a static YAML. Layered on top of global + system.

### Fivetran-active detection — currently scattered, be careful not to add a 6th copy

The `_FIVETRAN_*` prefix/pattern check exists independently in at least these places today:

- `src/core/skip_classifier.py` — regex-based `_FIVETRAN_PATTERNS`
- `src/matching/candidate_matcher.py` — hardcoded `_FIVETRAN_PREFIX` string check
- `src/ai_transformation/ai_rule_mapper.py` — another hardcoded prefix constant
- The SQL generators (`WHERE _FIVETRAN_ACTIVE = TRUE` injection)
- All 5 exclusion YAML files

This is known duplication, not a design goal — if you're adding Fivetran-related logic, check whether one of these five already does what you need before adding a sixth. If you're refactoring this area, the goal is one shared constant/function, not a new abstraction layered on top of the existing five.

`_FIVETRAN_ACTIVE = TRUE` is a filter, not an exclusion — it's applied to the Snowflake side so only the latest active record per key is compared (soft-deleted/superseded Fivetran rows aren't validation failures). Don't confuse this with excluding the `_FIVETRAN_ACTIVE` *column itself* from comparison (which also happens, separately, via the exclusion lists above).

## Verification checklist

- [ ] A normalization change was applied identically to both source and target (one function, not two)
- [ ] NULL still round-trips as NULL, not as an empty object/string
- [ ] `test_semantic_normalize.py` passes after the change
- [ ] A new Fivetran-related check reuses an existing detection point instead of adding a new one
- [ ] Table-specific exclusions layer on top of (don't replace) global + system exclusions
- [ ] If editing exclusion YAML for a cross-source rule, it wasn't copy-pasted into all 5 files separately
