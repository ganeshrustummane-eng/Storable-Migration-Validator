# 0007. Fix: "Raw stdout/stderr — non-zero exit" shown even though every table passed

**Status:** Fixed
**Date:** 2026-09-23

## What you reported

After the [0006](0006-run-validation-results-never-rendered-misindented-block.md) fix,
the preview rendered correctly — count validation summary, data validation
summary, then data validation row-level for each table — but an extra "Raw
stdout/stderr (non-zero exit — some tables may have errored)" expander also
showed up, even though nothing looked actually wrong. You suspected this was
happening dynamically/unpredictably and asked to check the real cause rather
than just hide it, pointing at the YAML config writer as a possible source.

## What I checked before touching anything

Read the actual output of that run instead of guessing:

- `count_validation_summary.csv`: `customers` PASS, `orders` PASS (both 8/8, `count_difference=0`).
- `data_validation_summary.csv`: `customers` PASS, `orders` PASS (both 8/8, no mismatches).

Every real validation result was a pass. So the non-zero exit code that
triggered the "raw stdout" expander was **not describing a real data-quality
failure** — the raw-log expander logic in `webapp/app.py` (only shows on
`result["returncode"] != 0`) is correct as written; the bug is upstream, in
what set that exit code in the first place.

## Root cause

`Project/main.py:178-185`. Each validation type's config directory
(`config/silver/count_validation/`, for example) holds **one file per source
system** — `postgres.yaml`, `mssql.yaml`, and eventually `athena.yaml` /
`redshift.yaml` per `CLAUDE.md`'s four source types. `main.py` loops over
every file in that directory and, for each one individually, checks whether
the requested tables exist in *that* file:

```python
if not tables_to_process:
    logger.error("No tables to process for validation=%s from %s ...")
    failure_count += 1
    system_error = True
    continue
```

`customers`/`orders` are Postgres tables — they exist in `postgres.yaml` and
validated there successfully. But the loop *also* opens `mssql.yaml` (a
completely different source, SiteLink), finds neither table in it (correctly
— they were never supposed to be there), and treats that as a failure anyway.
This isn't a corner case — it fires on **every run** that validates
Postgres-only or MSSQL-only tables while the sibling source file also exists
in the same directory, which is the normal, expected layout described in
`CLAUDE.md`. The log confirmed it exactly:

```
ERROR | main.py:179 | No tables to process for validation=count_validation from
  .../count_validation/mssql.yaml (requested tables=['customers', 'orders'] not found in this config)
```

Nothing in `src/generated_queries/yaml_config_writer.py` or the "failed rows
only" handling was involved — both YAML files were correctly generated and
correctly scoped to their own source. The bug was purely in how `main.py`
interpreted "not in this one file" as "not validated anywhere."

The correct check for "was this table validated by *nobody*" already exists,
separately, at the end of the run (`main.py:545-553`): it unions
`processed_tables` across every file and every validation type, and only
flags a real gap if a requested table never showed up anywhere. That check is
right and untouched.

## Fix

Removed the per-file `failure_count += 1; system_error = True` at
`main.py:178-185`. A table not being present in one particular source file is
expected/routine, not an error — downgraded the log to `debug` and just
`continue` to the next file. The end-of-run `missing_tables` check remains
the sole authority on real coverage gaps.

## Verification

- Re-ran the exact command from your log: `python main.py --layer_type silver
  --tables customers orders --count_validation yes --data_validation yes
  --environment local`. Before: `Total failures: 1`, exit code 1. After:
  `Total failures: 0`, exit code 0 — matching the summaries, which were
  already all-PASS both times.
- Full suite: 95 passed (94 before + fixed a pre-existing pytest-collection
  bug in `test_runner_subprocess.py`'s `tmp_main_py` param while in there —
  unrelated to this bug, just noticed it while running the suite).

## Consequences

- Any run that validates a subset of tables from only one source system,
  while other source config files exist in the same validation-type
  directory (the normal case for a multi-source layer), no longer reports a
  false failure or triggers the "raw stdout" expander in the UI.
- A *real* "this table was never validated by anything" gap is still caught
  — by the existing end-of-run check, not the per-file one that was removed.
