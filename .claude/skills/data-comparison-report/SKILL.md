---
name: data-comparison-report
description: "Use when working on row-level data comparison, on how validation results get written to CSV, or on the large-table (200-300M row) hybrid Tier-1/Tier-2 execution strategy. Project/main.py is the correctness oracle and the only engine for tables that haven't opted in; Project/tiered_runner.py is an additive, opt-in (validation_plan.execution_strategy: hybrid_v1) alternate execution strategy for the same row-level comparison + quality-check contract, never a second independent engine. The former second, chat-agent-only engine (src/validation/data_validator.py, count_validator.py, validation_executor.py) was removed with the chatbot and moved to trash/validation/. Files: Project/main.py, Project/runner.py, Project/tiered_runner.py, Project/utils/quality_checks.py, Project/db/*.py."
---

# Data comparison and CSV reporting -- one engine

## History (why you may still see references to a second engine)

CLAUDE.md and this skill used to describe two live engines. The second one
existed only to back the chatbot/chat-bubble's `execute_validation` tool
(`src/connector/tools.py`). That chatbot feature was removed; `tools.py` no
longer exists, and `src/validation/data_validator.py`,
`src/validation/count_validator.py`, and `src/validation/validation_executor.py`
had no other caller anywhere in `webapp/`, `Project/`, or `src/validate_cli.py`
(verified by repo-wide grep before moving). They now live in
`trash/validation/` -- check there before assuming this logic no longer
exists anywhere, per CLAUDE.md's convention for `trash/`.

`src/validation/config_schema.py` and `src/validation/plan_validator.py` are
unrelated and still live -- imported directly by `webapp/app.py`,
`src/validate_cli.py`, and `src/validation_pipeline.py`. Don't move those.

## Engine -- row-level (`Project/main.py` + `Project/runner.py` + `Project/db/*.py`)

This is what the webapp's "Run Validation" button calls: `webapp/app.py`'s Run
Validation tab -> `Project/runner.py`'s `run_validation(layer, environment, tables,
count_validation, data_validation, timeout=900)` -> builds a `subprocess.run([...,
"main.py", "--layer_type", ..., "--tables", ..., "--count_validation", "yes"/"no",
"--data_validation", "yes"/"no", "--environment", ...])` -> `Project/main.py`
(argparse CLI script). `runner.py`'s own docstring explains the subprocess
indirection: `main.py` has module-level side effects and "isn't import-safe" --
don't try to replace this with a direct Python import/call without confirming that
constraint no longer holds.

`Project/main.py` reads already-generated YAML configs (produced upstream by the
generation paths -- see `webapp-yaml-generation` and `reference-filter-joins`
skills), calls `Project/db/factory.py`'s `get_database()` to get the right connector
(`Project/db/postgres.py` / `mssqlserver.py` / `athena.py` / `snowflake.py`, all
implementing the same two-method `Database(ABC)` interface -- `connect()` and
`execute_query()`), and produces **row-level** output: every row gets a status of
`PASS`, `FAIL`, `SOURCE_ONLY`, or `TARGET_ONLY`. It writes a full results CSV
(`result_df.to_csv(...)`) AND a separate failed-only CSV (`failed_df.to_csv(...)`).

Comparison is on full row value, not just the PK: for a matched key on both
sides, every shared (non-excluded) column is sorted and string-compared --
a row with a corrupted non-PK column correctly reports `FAIL`, not `PASS`.
When no primary key is configured, `main.py` falls back to a Python-computed
`row_hash` (MD5/SHA256 over the common or configured columns, NULL-safe) used
as the join key -- the same value comparison still runs on top of it, and a
heuristic (`row_hash_fallback_looks_like_column_drift`) warns when
SOURCE_ONLY/TARGET_ONLY counts look suspiciously balanced (usually one
un-normalized column desyncing every hash, not real missing rows).

**Both CSVs matter.** CLAUDE.md is explicit: don't build a path that only reports
failures -- passed rows must be visible too, since "no failures reported" and
"validation didn't run" must never look the same. If you touch this file, keep
both CSV outputs intact.

## Large-table hybrid engine (`Project/tiered_runner.py`) -- opt-in, additive, not a second oracle

Full design investigation and rationale: `docs/large-table-scalable-architecture/README.md`
(§A-§L for the architecture and correctness invariants, §Q/§S for what's actually
implemented). Read that doc before making any change here -- it traces exactly
which of `main.py`'s behaviors are load-bearing (§B) and which candidate designs
were rejected and why.

**Dispatch**: `main.py` calls `tiered_runner.run_table_hybrid()` instead of its own
inline fetch+compare block only when `utils.utility.should_dispatch_hybrid()` returns
`True` -- exactly `validation_name == "data_validation"`, a sibling
`validation_plan.execution_strategy: hybrid_v1`, and a real (non-placeholder)
`row_hash_validation` query pair. Every table that hasn't opted in is completely
unaffected; `main.py`'s own inline path is untouched by this engine's existence.

**Why it exists**: `main.py`'s `fetchall()` + Python PK-union loop can't scale to
200-300M rows without materializing full source + full target + full result in
memory at once (the thing `docs/large-table-scalable-architecture` §N forbids).
`tiered_runner.py` never does that:
- **Tier 1**: streams `(key, hash)` pairs only (via the existing generated row-hash
  SQL, `execute_query_stream`) to classify every key as
  `SOURCE_ONLY`/`TARGET_ONLY`/`HASH_MATCH`/`HASH_MISMATCH`. Classification compares
  the raw hash *string* from each side directly, so both sides must use the same
  hash algorithm (`src/generated_queries/sql_query_generator.py`'s
  `_hash_expression`/`_row_hash_queries`): `SHA2_256`/`SHA256` for every source
  dialect except PostgreSQL, which uses `MD5()` on both the Postgres source and the
  paired Snowflake target -- PostgreSQL has no SHA-256 without the `pgcrypto`
  extension, which the controlled Postgres databases don't have (§T.2). Don't
  "fix" a Postgres-only hash mismatch by changing just one side.
- **Tier 2**: re-fetches full rows only for the narrow `HASH_MISMATCH`/`SOURCE_ONLY`/
  `TARGET_ONLY` subset, batched (`TIER2_BATCH_SIZE`), through the **same**
  `canonicalize_frames` + `compare_indexed_frames` (`Project/utils/row_compare.py`)
  the non-hybrid path uses -- one comparison implementation, not two. `HASH_MATCH`
  keys are written straight to PASS, never re-fetched.

**Quality checks (`quality_failures`/`grain_failures`) get the same treatment**:
`Project/utils/quality_checks.py`'s `run_quality_checks`/`validate_expected_grain`
need a fully-materialized frame (pandas `.isna()`/`.nunique()`/`.sum()`/
`.duplicated()`), which this engine never has. `tiered_runner.py` reimplements each
check as a bounded SQL aggregate or capped sample over the *existing*
`sourcequery`/`targetquery` text (never an independent filter/WHERE builder) --
`_validate_expected_grain_hybrid` derives duplicate-row counts from the Tier-1 hash
multimap it already has (§Q); `_run_quality_checks_hybrid` covers `null_rate`,
`distinct_count`, `sum`/`min`/`max`, `sample_hash` (§S, fixed against a live
oracle-vs-hybrid run in §T). Four things to know before touching these:
- `distinct_count` **excludes** JSON/HStore/decimal-precision columns from the SQL
  push-down rather than give them a wrong answer -- canonicalization (JSON key
  reordering) is Python-only by design (see the linked doc's §F), and no SQL
  dialect here reproduces it (investigated again in §T.5, exclusion kept).
- `sum`/`min`/`max` does **not** exclude numeric-string columns (`'400000.00'` vs
  `'400000.000000'`) -- it `ROUND(..., 2)`s them in the SQL aggregate itself
  (`_dialect_numeric_cast`'s `round2` param), matching `canonicalize_frames`'
  pre-aggregation rounding exactly (§T.4). Don't reintroduce an exclusion here;
  the live-observed divergence this fixed (`0.1234` summing to `1.2334` instead of
  the oracle's `1.23`) is the reason it doesn't exist.
- `sample_hash` is capped at `SAMPLE_HASH_ABSOLUTE_CAP` (10,000 rows/side) --
  the oracle's own `sample_hash_percent` has no cap and would refetch millions of
  rows at 300M-row scale if reproduced literally. It also runs the fetched sample
  through `canonicalize_frames()` before hashing (§T.3) -- hashing the raw sample
  would flag JSON-key-order/HStore-formatting differences the oracle treats as
  equal.
- **Any new SQL that wraps `sourcequery`/`targetquery` as a subquery and then
  references one of its projected columns must quote that column with
  `_quote_ident(dialect, name)`.** Snowflake's generated aliases are quoted
  lowercase (`AS "id_normalized"`); an unquoted outer reference resolves to the
  uppercase-folded name instead and errors or silently targets nothing (§T.1 --
  this was a real, live CRITICAL bug in `_fetch_batch` and
  `_quality_aggregate_sql` before it was fixed). `_probe_columns` and
  `_limit_query`/`_fetch_sample` are exempt -- both use `SELECT *`.

**Scope limits, current as of this session**: single-column PK or PK-less
(`row_hash`-keyed) tables only. Composite PKs raise `NotImplementedError`
immediately. PK-less tables get row-level PASS/FAIL only when Tier 1 finds a clean
match on every key -- any mismatch raises rather than guessing, and PK-less tables
never reach the quality-check code at all (same as composite PKs never do).

## Report format facts (verified, not aspirational)

- Reports are **CSV and YAML only.** There is no XML report generation anywhere in
  this codebase (verified: zero hits for `xml.etree`/`ElementTree`/`lxml` repo-wide).
- There is no `.xlsx` report *writer* either. `.xlsx` only appears as an *input*
  format (uploaded mapping sheets, read via `pandas.read_excel` in
  `src/excel_batch_loader.py`). The webapp's Output Files tab has a passive file
  filter that includes `.xlsx`, but nothing in the codebase ever produces a matching
  file today -- don't treat that filter as evidence of an XLSX writer existing.
- If a genuine need for XML or XLSX report output comes up, that is a new feature
  to design and confirm with the user first -- not something to build under the
  assumption it's a documentation gap.

## Verification checklist

- [ ] If touching `Project/main.py`'s output, confirm both the full-results CSV and
  the failed-only CSV are still written.
- [ ] Confirm any filter/exclusion applied on the source side is applied identically
  on the Snowflake side (asymmetric filters produce false mismatches that look like
  data-quality bugs but are validator bugs -- CLAUDE.md's Consistency dimension).
- [ ] Don't add XML or XLSX report writing without the user explicitly asking --
  neither exists today.
- [ ] If touching `Project/tiered_runner.py`, verify the change is behind the
  existing `hybrid_v1` opt-in and doesn't alter `main.py`'s non-hybrid behavior --
  run `Project/test_tiered_runner.py` (differential checks against the untiered
  oracle) alongside `Project/test_hybrid_dispatch.py` and
  `Project/utils/test_quality_checks.py`.
- [ ] `py_compile` any touched `.py` file before calling the change done.
