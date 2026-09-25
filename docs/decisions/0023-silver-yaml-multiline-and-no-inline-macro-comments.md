# 0023. Silver YAML keeps multi-line SQL; macro-skip columns carry no inline comment at all

**Status:** Accepted
**Date:** 2026-09-25
**Follows on from:** [0022](0022-silver-macro-skip-block-comments-and-fixed-output-dir.md) (block-comment fix for the same symptom)

## Context

0022 fixed the immediate correctness bug (a `--` line comment swallowing the
rest of a flattened single-line query) by switching to `/* ... */` block
comments. The user reviewing the generated `LEADS.yaml` rejected that too:
the query was still being collapsed onto one unreadable YAML line by
`yaml_config_writer.py`'s `_to_single_line()` (shared with Bronze), and they
specifically didn't want any comment-style annotation (`--`, `/* */`, or
otherwise) inline in the SQL at all — pending-macro columns should either be
genuinely part of the runnable query or not mentioned in it.

Putting the raw Coalesce macro text (`{{ ids_to_surrogate_key(...) }}`)
into the query as a real, uncommented SELECT expression was considered and
explicitly rejected after asking the user directly: `Project/main.py` sends
`main_validation_source` to Snowflake as one statement, and `{{ }}` is Jinja,
not SQL — no macro-resolution step exists anywhere in this codebase to
rewrite it first (ADR 0014 3 deliberately scoped that out: "no general
Jinja/macro interpreter"). Inlining it uncommented would turn a single
excluded column into a hard failure for every column in that table.

## Decision

1. `yaml_config_writer.py::write()` / `write_from_plan()` gained a `layer`
   parameter. `_prep()` now branches on it: Bronze keeps
   `_to_single_line()` (unchanged, still relied on elsewhere); Silver uses
   the new `_to_indented_multiline()`, which just re-indents
   `silver_sql_emitter.py`'s already one-column-per-line output to the
   YAML's 10-space block-scalar indent, preserving every line break.
   `QueryOutputManager.generate_from_plan()`'s existing `layer` argument
   threads straight through — no new plumbing beyond passing it one level
   further.
2. `silver_sql_emitter.py::_bronze_select_lines()` no longer special-cases
   macro-computed columns at all. They're simply omitted from the SELECT —
   the same treatment already given to non-deterministic and unclassifiable
   skipped columns. No comment marker of any kind is emitted for them.
3. `coalesce_plan_builder.py` now appends a `review_reasons` entry listing
   every non-business-key macro-skip column by name (business-key macro
   columns already got one). This is the only place that information
   survives into the generated YAML — each mapping's own `skip_reason` is
   still recorded in the persisted plan JSON (`PlanStore`) but was never
   rendered into the YAML itself, so without this the exclusion would be
   invisible to someone reading just the YAML.

## Alternatives considered

- Inline the raw macro text uncommented, matching the exact format the user
  first pasted, and defer macro resolution to a future runtime step.
  Rejected after confirming the tradeoff with the user directly: it breaks
  `main_validation_source` as a whole (one Snowflake syntax error kills
  every column's validation for that table, not just the macro column's),
  and no macro-resolution step exists to build on top of it yet.
- Keep the `/* */` block-comment fix from ADR 0022 and just add multi-line
  formatting around it. Rejected per explicit user instruction: no comment
  syntax at all, in any form.

## Consequences

- Silver-generated YAMLs are now genuinely readable (real line breaks, one
  column per line) and always contain valid, runnable SQL — no comment
  syntax, no risk of a future flattening pass reintroducing the original bug
  class, since there's nothing comment-shaped left in Silver's SQL to break.
- Bronze's YAML formatting (`_to_single_line`) is untouched — this was
  scoped to Silver only, since Bronze's SQL has never contained inline
  comments and there was no reported problem there.
- Previously generated Silver YAMLs (e.g. `LEADS.yaml`, and anything from
  ADR 0022's block-comment version) must be regenerated to pick this up;
  neither this nor 0022 rewrites already-written files.
- The macro-resolution gap itself (turning `{{ ids_to_surrogate_key(...) }}`
  into real, runnable SQL) is still open and out of scope here, same as ADR
  0014 3 already flagged — this ADR only changes how its *absence* is
  represented in generated output, not the underlying gap.
