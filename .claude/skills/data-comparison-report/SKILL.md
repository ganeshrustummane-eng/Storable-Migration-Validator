---
name: data-comparison-report
description: "Use when working on row-level or table-level data comparison, or on how validation results get written to CSV. Covers the two live comparison engines -- Project/main.py's row-level engine (called by the webapp's Run Validation button via Project/runner.py) and src/validation/*.py's shallower table-level engine (called by the chat agent's execute_validation tool). Files: Project/main.py, Project/runner.py, Project/db/*.py, src/validation/data_validator.py, src/validation/count_validator.py, src/validation/validation_executor.py, src/connector/tools.py."
---

# Data comparison and CSV reporting -- two engines, both live

## Why two engines exist (known duplication, not a bug to "fix" silently)

Per CLAUDE.md: this duplication is acknowledged, real, and not yet resolved. Don't
delete or "consolidate" either engine without the user explicitly asking for that --
they currently serve two different entry points with different depth of output.

### Engine 1 -- row-level (`Project/main.py` + `Project/runner.py` + `Project/db/*.py`)

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

**Both CSVs matter.** CLAUDE.md is explicit: don't build a path that only reports
failures -- passed rows must be visible too, since "no failures reported" and
"validation didn't run" must never look the same. If you touch this file, keep
both CSV outputs intact.

### Engine 2 -- table-level (`src/validation/*.py`)

Called from `src/connector/tools.py`'s `execute_validation()` tool -- the
chat-agent's tool-callable validation path, separate from the webapp button.
`execute_validation()` loads a stored plan via `PlanStore`, finds the YAML under
`Project/config/{layer}/data_validation/`, instantiates
`ValidationExecutor(base_dir=..., environment="dev")`, and runs it per YAML file.

`src/validation/data_validator.py` (`execute_data_validation()`) and
`src/validation/count_validator.py` (`execute_count_validation()`) are the shallower
comparison logic here -- table-level pass/fail (or count match/mismatch), not a
per-row breakdown. `src/validation/validation_executor.py`'s `ValidationExecutor`
orchestrates a batch of these (`execute_batch()`) and builds a coverage report
(`_build_coverage_report()`). This engine does not produce the row-level
PASS/FAIL/SOURCE_ONLY/TARGET_ONLY CSV that Engine 1 does -- don't assume the two are
interchangeable outputs.

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

- [ ] Identify which engine you're actually changing -- row-level (`Project/main.py`)
  vs. table-level (`src/validation/*.py`) -- before touching comparison logic; a fix
  in one does not apply to the other.
- [ ] If touching `Project/main.py`'s output, confirm both the full-results CSV and
  the failed-only CSV are still written.
- [ ] Confirm any filter/exclusion applied on the source side is applied identically
  on the Snowflake side in whichever engine you're touching (asymmetric filters
  produce false mismatches that look like data-quality bugs but are validator bugs
  -- CLAUDE.md's Consistency dimension).
- [ ] Don't add XML or XLSX report writing without the user explicitly asking --
  neither exists today.
- [ ] `py_compile` any touched `.py` file before calling the change done.
