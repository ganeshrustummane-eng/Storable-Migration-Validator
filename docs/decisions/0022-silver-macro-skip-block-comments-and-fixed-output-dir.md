# 0022. Silver macro-skip comments use `/* */`, and Silver YAML output dir is fixed, not picked

**Status:** Accepted
**Date:** 2026-09-25

## Context

`yaml_config_writer.py`'s `_prep()` runs every generated SQL string through
`_to_single_line()` before writing it into the YAML's `sourcequery`/
`targetquery` block — collapsing all newlines to spaces, one physical line,
for every layer (this predates Silver and Bronze SQL never contained inline
comments, so it was harmless there).

`silver_sql_emitter.py` (ADR 0018) emits a `-- <col>: PENDING ...` line
comment for every macro-skip column (one Coalesce macro-computed surrogate
key needing manual resolution per column). A `--` comment runs to
end-of-line. Once `_to_single_line()` removes every line break, "end of
line" becomes "end of the entire query" — the first macro-skip column
silently commented out every column generated after it. Confirmed against a
real generated file (`Project/config/silver/data_validation/snowflake/LEADS.yaml`):
the `sourcequery` was one line where everything after the first `--` comment
was swallowed, including the real `FROM` clause.

Separately, the Silver batch-generation UI (`webapp/app.py`, the Coalesce
node-ID/paste-metadata tab) called the shared `pick_layer()` helper —
bronze/silver/gold selectbox, built for the CLI's original generic flow —
to choose the output directory, even though this tab only ever builds a
Silver plan (`layer="silver"` is already hardcoded in the
`generate_from_plan()` call two lines below it). The selectbox offered a
choice that had no effect on correctness but looked like it did, and let a
user accidentally write a Silver-sourced config under `config/bronze/` or
`config/gold/`.

## Decision

1. `silver_sql_emitter.py::_macro_comment()` now emits a `/* ... */` block
   comment instead of `-- ...`. Block comments have an explicit close
   delimiter, so they stay correct regardless of whether the surrounding SQL
   is later flattened to one line or kept multi-line — fixing the root cause
   (comment style vulnerable to line-flattening) rather than special-casing
   Silver inside the shared `_to_single_line()`/`_prep()` path that Bronze
   also depends on.
2. The Silver batch tab no longer calls `pick_layer()`. Output directory is
   fixed to `Project/config/silver` inline, matching the `layer="silver"`
   already hardcoded at the `generate_from_plan()` call site.

## Alternatives considered

- Skip `_to_single_line()` for Silver only, keep the emitter's original
  multi-line, one-column-per-line formatting in the YAML. Rejected for this
  pass: it would special-case Silver inside a shared helper Bronze also
  calls, and the block-comment fix alone already makes the flattened output
  correct — readability of the single-line form is a separate, smaller
  complaint than the correctness bug, and can be revisited later if a human
  reviewer finds the single line genuinely hard to read in practice.
- Keep `pick_layer()` but default it to "silver" and leave it changeable.
  Rejected: Silver validation is Snowflake-to-Snowflake by definition (ADR
  0013) — there is no valid reason to write a Silver plan's config anywhere
  but `config/silver/`, so offering the other two options is not a real
  choice, just a chance to misfile output.

## Consequences

Existing generated Silver YAMLs with macro-skip columns after the first one
(e.g. `LEADS.yaml`) were generated with the query cut short and must be
regenerated to pick up the fix — this ADR does not retroactively repair
already-written files. Any other Silver-only UI flow that still calls the
generic `pick_layer()` should get the same fix if/when found.
