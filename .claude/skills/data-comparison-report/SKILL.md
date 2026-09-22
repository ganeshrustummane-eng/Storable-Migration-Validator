---
name: data-comparison-report
description: "Use when working on row-level data comparison, or on how validation results get written to CSV. Project/main.py is the only live comparison engine (called by the webapp's Run Validation button via Project/runner.py). The former second, chat-agent-only engine (src/validation/data_validator.py, count_validator.py, validation_executor.py) was removed with the chatbot and moved to trash/validation/. Files: Project/main.py, Project/runner.py, Project/db/*.py."
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
- [ ] `py_compile` any touched `.py` file before calling the change done.
