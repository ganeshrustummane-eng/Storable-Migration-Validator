---
name: webapp-yaml-generation
description: "Use when webapp/app.py needs to write a validation YAML config directly from the UI (Generate Single YAML 'prompt' tab, the reference/filter/join 'RPJ' tab, or the Custom YAML manual editor), or when working on src/excel_batch_loader.py's Excel-upload batch YAML path. Covers the fact that these are INDEPENDENT yaml.dump code paths, separate from the backend src/generated_queries/yaml_config_writer.py generator. Files: webapp/app.py, src/excel_batch_loader.py."
---

# Webapp-side YAML generation (independent of the backend generator)

## Why this skill exists

`src/generated_queries/yaml_config_writer.py` (`YAMLConfigWriter`) is the backend's
canonical YAML generator, driven by `src/validation_pipeline.py`'s `run_with_plan()`
and `src/generated_queries/query_output_manager.py`. The **reference-filter-joins**
and **validation-query-yaml-generator** agent/skill both describe that path only.

That is not the only place YAML gets written. `webapp/app.py` and
`src/excel_batch_loader.py` each build and `yaml.dump()` their own YAML documents,
with their own schemas, calling into `AISQLQueryGenerator`/`SQLQueryGenerator`
directly for the SQL text rather than going through `YAMLConfigWriter`. Neither of
these two files imports or calls `yaml_config_writer.py` at all (verified by grep —
zero references either direction).

**This is a real, currently-live architectural fact, not a bug to silently fix.**
Treat it the way `CLAUDE.md` treats the two validation engines and the five
exclusion YAMLs: known, real, duplicated-but-intentional-for-now. Do not consolidate
these paths into `yaml_config_writer.py` without the user explicitly asking for that
— it's a bigger refactor than a normal skill/agent task, and the schemas the three
paths produce are not identical today.

## Path 1 -- `webapp/app.py`, "prompt" YAML (Generate Single YAML tab)

Around line 1968: builds a per-table YAML document (local var `_pt_doc`) and writes
it with `_pt_yaml.dump(_pt_doc, ...)` to `_pt_path = _pt_out_dir / f"{src_table}.yaml"`
(path built around line 1940). This is the simplest of the three UI-side writers --
one table, one YAML file, no join/filter fields.

## Path 2 -- `webapp/app.py`, "RPJ" YAML (reference-filter-join tab)

Around line 2396: builds the reference/filter/join YAML document (local var
`_rpj_doc`) and writes it with `_rpj_yaml.dump(_rpj_doc, ...)` to
`_rpj_path = _rpj_out_dir / f"{_rpj_fname}.yaml"` (path built around line 2369).
This is the UI surface for the natural-language filter/join condition described by
the **reference-filter-joins** skill -- but that skill documents the backend
`CanonicalValidationPlan` -> SQL/YAML path, not this direct dump call. When editing
either one, check both: the backend skill's plan fields and this UI doc's fields can
drift out of sync since they aren't the same code.

## Path 3 -- `webapp/app.py`, custom YAML manual editor

Around line 3095: takes a manually-edited YAML payload from the UI, writes it with
`_yaml.dump(yaml_payload, ..., Dumper=_yaml.SafeDumper)` to
`save_path = save_dir / f"{custom_yaml_filename}.yaml"` (path built around line 3139).
This is the escape hatch for a test lead hand-editing a config directly -- no
generator involved at all, whatever the user typed goes to disk as-is (aside from
`SafeDumper`'s normal YAML-safety serialization).

## Path 4 -- `src/excel_batch_loader.py`, `write_yaml()`

A distinct, legitimate feature (per the user: not the same thing as
`yaml_config_writer.py`, and not to be merged with it). Entry point: `load_excel()`
(around line 118) reads an uploaded `.xlsx` mapping sheet via `pandas.read_excel` --
this is the only place `.xlsx` is used as an *input* in the whole repo; there is no
`.xlsx` writer anywhere in the codebase (do not describe or build one without the
user asking -- verified NOT FOUND across `src/`, `Project/`, `webapp/`).

`write_yaml(spec, src_sql, tgt_sql, env, output_dir, dry_run)` (around line 410-451)
builds its own "DataValidation YAML block" dict (lines ~419-441) and writes it via
plain `open(out_path, "w") + yaml.dump(doc, ..., sort_keys=False, default_flow_style=False)`
to `out_path = pack_dir / f"{spec.yaml_file_name}.yaml"` (line ~413). SQL text for
this path comes from `generated_queries.ai_sql_generator.AISQLQueryGenerator`
directly (imported around line 288) -- not `SQLQueryGenerator`, not
`YAMLConfigWriter`. `src/validate_cli.py` also references `.xlsx` as CLI input
(`--file mapping.xlsx`) for the same batch-loading flow.

## Planned extension (not yet built)

There is a planned sub-tab for batch YAML generation from an uploaded Excel file
where the AI proposes a preview (source DB, schema, join condition) before
generating -- see the separate `excel-batch-ai-review-planned` skill for that design.
That feature does not exist in code yet; do not treat this skill's Path 4 above as
already having that preview/review UI.

## Verification checklist

- [ ] Confirm which of the 3 `webapp/app.py` paths (or `excel_batch_loader.py`) you're
  actually touching before changing YAML schema -- they are not interchangeable.
- [ ] If you change the RPJ doc's fields, check whether the backend
  `CanonicalValidationPlan` (reference-filter-joins skill) needs the same fields --
  they can drift, but an intentional new field in one should usually appear in both
  eventually.
- [ ] Don't add a fifth independent YAML-writing code path. If backend generation is
  what's needed, call into `yaml_config_writer.py` or `AISQLQueryGenerator`/
  `SQLQueryGenerator` -- don't hand-build another `yaml.dump()` site.
- [ ] Don't build an `.xlsx` writer/export feature under this skill without the user
  explicitly asking -- none exists today, and the webapp's "Excel" file-browser filter
  in the Output Files tab is currently dead code (nothing ever produces a matching file).
- [ ] `py_compile` any touched `.py` file before calling the change done (repo-wide rule).
