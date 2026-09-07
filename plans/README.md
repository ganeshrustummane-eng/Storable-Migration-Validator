# Plans — Migration Validator

Written against commit `cb3c859` (branch `version1.2`).

## Execution order

| # | Plan | Status | Effort | Depends on |
|---|---|---|---|---|
| 001 | [Fix YAML `source:` field in Custom SQL tab](001-fix-custom-sql-yaml-source-field.md) | DONE | XS | — |
| 002 | [Add AI-generated Snowflake SQL in Custom SQL tab](002-custom-sql-tab-snowflake-ai-generation.md) | DONE | S | 001 recommended first |
| 003 | [Surface failed-only CSVs (colored) on dashboard](003-failed-only-colored-csv-dashboard.md) | DONE | S | — |
| 004 | [Add AWS Redshift as a 4th source database type](004-add-redshift-source-support.md) | DONE (code) | M | — |

## Dependency note

Ship 001 first. It is a 2-line critical fix that makes Custom SQL YAMLs actually
executable. Plan 002 is additive and safe to do independently, but executing 001
first means you can test 002's YAML output end-to-end without the dialect bug
masking results.

## What was audited / not audited

Audited: `webapp/app.py` (Custom SQL Validation tab + `render_custom_sql_section`),
`src/generated_queries/ai_sql_generator.py`, `src/generated_queries/yaml_config_writer.py`,
`src/validation/config_schema.py`, `src/sql_extractor/extractors.py`, `src/rules/`.

Not audited in this session: `src/validate_cli.py`, `Project/main.py` execution path,
CI configuration, test suite coverage of the Custom SQL tab.

**Session 2 (plan 003):** Audited `Project/main.py` data-validation branch (lines
~180-340, including the already-existing `*_failed_*.csv` writer), `Project/runner.py`
`run_validation()`, `Project/results_store.py`, `src/utils/summary_reporter.py`,
`src/validation/{count_validator,data_validator}.py`, and the Run Validation tab
rendering in `webapp/app.py` (`_style_status`, `_render_diff_file`,
`render_paginated_df`, lines ~440-580 and ~2939-2977). Confirmed: no
`st.download_button` exists anywhere in `webapp/app.py`; every DB connector
(`Project/db/{postgres,mssqlserver,snowflake,athena}.py`) calls `.fetchall()` with no
chunking — relevant to the separate 100M-row scale review, not to plan 003 itself.

## Considered and rejected

- Adding MySQL support: no `mysql` extractor exists anywhere in the codebase. Adding
  one would be a separate, larger effort outside the scope of these two plans.
- Changing `render_custom_sql_section` (Surface B): it already handles "Both sides"
  correctly and saves `.sql` files. Not broken.
- Modifying `src/generated_queries/ai_sql_generator.py`: the AI generator is already
  fully dialect-parameterized. No changes needed there.
- Deleting `src/rules/{athena,mssql,snowflake}_rules.py` re-export shims (flagged as
  `yagni` in the over-engineering review): reconsidered and rejected once plan 004
  (Redshift) was scoped — these per-dialect modules are the exact extension point new
  sources hook into, not dead duplication. Keep them.

## Applied directly this session (not plans — small, low-risk, already fixed)

Per explicit user request ("fix the issues you mentioned... act as the AI Engineer and
fix issue"), the following over-engineering findings from the ponytail-review were
fixed directly rather than turned into plans, since each was small and low-risk:

- Deleted `src/exclusions/` (`exclusion_manager.py` + `bidirectional_exclusion_handler.py`,
  1,124 lines), `src/ai_transformation/ai_rule_mapper_enhanced.py`, and the `use_enhanced`
  flag/branch in `src/ai_transformation/orchestrator.py` — confirmed dead: only reachable
  via `use_enhanced=True`, which nothing outside `examples/integration_test_orchestrator.py`
  ever set. Also deleted that example and `examples/bidirectional_exclusion_example.py`
  (they only existed to exercise the removed code).
- Deleted `src/rules_catalog_v4_multi_source.json` — confirmed zero Python references
  anywhere in the repo.
- `src/setup_wizard.py`: replaced the hand-rolled `_prompt`/`_yn` input helpers with
  `click.prompt`/`click.confirm` (click is already a dependency). Left the `_C` ANSI
  color class and `_pick`/`_box`/`_head` menu-drawing helpers alone — `_C` has 30+ call
  sites across this ~1000-line interactive wizard, and a full rewrite to `click.style`
  is a much bigger, untestable-here diff for a purely cosmetic finding. Revisit only if
  someone wants to actually test the wizard interactively while doing it.
- `src/learning/retrieval.py`: `_edit_distance_le1` now calls
  `rapidfuzz.distance.Levenshtein.distance` when available, falling back to the
  original hand-rolled loop on `ImportError` — matching the existing optional-rapidfuzz
  pattern already used in `src/matching/fuzzy_matcher.py`, rather than assuming
  rapidfuzz is a hard dependency.

All five changes verified with `ast.parse` (no syntax errors) and a repo-wide grep
confirming no remaining references to the deleted modules/flags.

## Session 3 — plans 003 and 004 implemented

**Plan 003 (DONE):** `Project/runner.py` now globs `*_failed_*.csv` into a new
`failed_files` key. `webapp/app.py`'s Run Validation tab renders those files (colored,
via the existing `_render_diff_file`) with a download button, adds a "Show failed only"
checkbox + download button on the per-table summary panel, and adds a download button
to the Historical Runs tab's already-filterable results table. Verified with `ast.parse`
on both files — no live run available in this session to verify the rendered output
end-to-end, do that on the next real Run Validation execution.

**Plan 004 (code DONE, confirmed 4th-source scope):** Redshift added as a source
throughout the extension points the file's own docstring names: `config/database_registry.yaml`
(`SRC_4`), `.env.example`, `Project/db/factory.py` (reuses `Postgres`, port 5439 default,
no new connector class), `src/sql_extractor/extractors.py` (`RedshiftExtractor` subclasses
`PostgresExtractor`, overrides only type normalization for `super`/`varbyte`/`hllsketch`/
`geometry`/`geography`), `src/rules/redshift_rules.py` (re-export shim, same shape as the
other three), `config/redshift_exclusions.yaml` (empty — no known Redshift-specific
system columns to exclude yet) + `src/validate_cli.py` wiring, `webapp/app.py`
(`SOURCE_TYPES` tuple — confirmed no other hardcoded source-type tuple exists), and
`src/setup_wizard.py` (`DB_TYPES`/`_DB_ALIASES`, plus fixed schema-discovery to route
Redshift through `_discover_postgres_schemas` and default its schema to `public` instead
of falling into the MSSQL `dbo` default — a real gap the plan's step 7 didn't spell out
but the same "reuse Postgres" logic implied).

Verified: `ast.parse` on every touched file, a live `ExtractorFactory.create("redshift", ...)`
smoke test (resolves to `RedshiftExtractor`, correct port), a `redshift_rules` import
check, and YAML parsing of both new/edited config files. **Not verified end-to-end** —
no live Redshift cluster in this session. First real test should be a `count_validation`
run against one small Redshift table through `Project/main.py`, per the plan's own test
section.

## Session 4 — `Project/main.py` correctness fixes (implemented directly, no plan)

User handed over 7 findings from an independent review of `Project/main.py` +
`Project/utils/utility.py`, all verified against current code before fixing, plus one
more found while verifying (#8). Implemented directly per explicit request:

1. **Count validation had zero tolerance, no live-table awareness** (`main.py` count
   branch): added opt-in `count_mismatch_threshold_pct` per-table YAML field, default 0
   (exact match — unchanged behavior for every existing YAML unless a table sets it).
   Mirrors data_validation's existing `mismatch_threshold_pct` pattern. UI field added
   in `webapp/app.py`'s Run Validation tab, same injection pattern as the existing
   mismatch-threshold field. Decision logic extracted to `count_validation_match()` in
   `utility.py` (testable, no DB needed).
2. **One bad `--tables` entry killed the whole run**: `open(yamlfile)` (`main.py`, was
   line 118) is now wrapped in try/except — a missing/bad config file is logged,
   counted as a failure, and skipped; every other table in the run still executes.
3. **No manifest/completeness check**: added an empty-`tables_to_process` guard per
   YAML file, plus a whole-run check at the end comparing requested tables against
   `processed_tables` — any table never validated is now logged and counted as a
   failure (previously: silent, exit 0).
4. **Swallowed exceptions never counted as failure** (`main.py`, was lines 381-388):
   both the `(pyodbc.Error, psycopg2.Error)` and generic `Exception` branches now
   increment `failure_count`, set `system_error = True` (so `notify_failure` fires and
   exit code is non-zero), and write a best-effort `ERROR` summary row via the new
   `_write_error_summary()` helper — a crashed table now shows up in the CSV/dashboard
   instead of silently vanishing.
5. **`row_hash` fallback conflated all-column drift with one bad column**: didn't
   change the matching algorithm (a full-row hash genuinely is the only identity
   available with no PK) — added a diagnostic: when SOURCE_ONLY and TARGET_ONLY counts
   are roughly equal across most of the table, log a warning that this usually means
   one un-normalized column desynced the hash, not real row loss, and to configure a
   real PK for accurate column-level diffs. Heuristic extracted to
   `row_hash_fallback_looks_like_column_drift()` in `utility.py`.
6. **Exclusion rules were generation-time only, no staleness check**: added a one-time-
   per-(yaml, exclusions-file) warning when `config/{source}_exclusions.yaml`'s mtime is
   newer than the YAML being run — doesn't re-apply the rules (that still needs YAML
   regeneration), just surfaces the drift instead of it being silent.
7. **`generate_sql()` dead code**: deleted from `utility.py` — confirmed zero callers
   anywhere in the repo (the incremental/historical delta-validation path it implied
   never existed). The other half of this finding — whole-table `fetchall()` with no
   pagination/streaming — was **not** implemented: it's a real architectural change to
   the core comparison engine (ties into the earlier 100M-row scale review) that
   deserves its own reviewed plan, not a same-session patch.
8. **(found while verifying #1) Source DB always connected via `"local"` environment**,
   ignoring `--environment` — only the target respected the flag. Fixed to use
   `environment` for both, matching the target's existing behavior. **This was a
   judgment call, not a confirmed-intentional fix** — flagged to the user as needing
   confirmation before this session ended; implemented anyway per "implement all," but
   if source credentials were deliberately meant to stay environment-independent, this
   is the line to revert (`main.py`, `get_database(source, BASE_DIR, environment, ...)`).

**Not implemented, needs its own design round:** point-in-time consistency between
source/target queries (finding — sequential queries with no snapshot/lag cutoff mean
rows written mid-run are indistinguishable from real drift). A real fix needs a
snapshot/watermark strategy decided deliberately, not patched under time pressure.

**Verification:** `ast.parse` on every touched file; a new `Project/utils/test_utility_checks.py`
(self-check, no DB, run via `python Project/utils/test_utility_checks.py`) covers the
two extracted pure-logic functions (#1's threshold decision, #5's heuristic) — passes.
**Not verified against a live database run** — no live source/target available this
session. First real test should be a normal `Project/main.py` run against known-good
tables to confirm PASS/FAIL behavior is unchanged for existing YAMLs (no
`count_mismatch_threshold_pct`/`mismatch_threshold_pct` set), then one deliberate test
of each new path (a typo'd `--tables` entry, a forced exception, a live/CDC-like table
with the new count threshold set).
