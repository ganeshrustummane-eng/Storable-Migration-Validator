# 0015. Silver v1 implementation: schema-diff gate + deferred surrogate-key PK resolution

**Status:** Accepted
**Date:** 2026-09-24
**Follows on from:** [0013](0013-silver-layer-validation-strategy.md), [0014](0014-coalesce-metadata-extraction-rules.md)

## Context

ADR 0013/0014 specified the recompute-vs-materialize strategy and the
Coalesce metadata extraction rules, but left two things for implementation
time to pin down:

- Neither ADR specs a UI-facing gate between "fetch the node's metadata"
  and "generate the plan/YAML." Coalesce's declared `metadata.columns[]`
  list can silently drift from what is actually sitting in the live
  Bronze/Silver Snowflake tables (a column renamed, dropped, or added on
  either side without the Coalesce node being touched). Generating a plan
  straight from stale metadata would produce a confidently wrong SQL diff —
  the exact "validator bug masquerading as a data bug" CLAUDE.md's
  Consistency dimension warns about.
- ADR 0014 section 5 flagged the macro-argument-parsing heuristic for
  surrogate business keys as needing a second real sample before it is
  "provably right," but did not say what v1 should do in the meantime.

## Decision

1. **Schema-diff-before-generation gate.** `build_plan()` always computes a
   `SchemaDiff` (declared metadata columns vs. live Bronze columns vs. live
   Silver columns) alongside the plan, using the same
   `sql_extractor.extractors.ExtractorFactory` extractor bronze validation
   already uses. The UI is expected to show this diff before letting the
   user generate/save YAML from the plan, with two actions per stray
   column: **Exclude** (writes a global exclusion via
   `write_schema_diff_exclusion()`, which calls the same
   `save_global_user_exclusion()` bronze's CLI already uses — no new
   exclusion-writing path) or **raise a JIRA bug** (via the existing
   `src/connector/jira_client.py`, unchanged). This mirrors bronze's
   existing exclusion UX instead of inventing a new one.
2. **Deferred macro-argument parsing.** When a table's business key
   includes a macro-computed column, v1 does not parse the macro's
   arguments (ADR 0014 section 5's heuristic). Instead: leave
   `source_primary_keys`/`target_primary_keys` empty, set
   `plan.requires_review = True` with a `review_reasons` entry naming the
   macro-computed column(s), and surface the passthrough-column candidates
   for a human to pick the real natural key from
   (`plan.population_scope["natural_key_candidates"]`). A human confirms
   the join grain once, through a manual UI override, before the plan is
   trusted — same pattern `CanonicalValidationPlan.requires_review` already
   uses for AI-resolved ambiguity in bronze.

## Alternatives considered

- **Generate YAML directly from Coalesce metadata with no diff check.**
  Rejected — silently trusts metadata that can drift from the live schema;
  the whole point of ADR 0013 was to catch metadata/execution drift, not
  add a new place it can hide.
- **Write the macro-argument parser now, using the one `INT_FACILITIES`
  sample.** Rejected — ADR 0014's own Consequences section already says
  not to write the production version of this parser until a second real
  sample node with a composite surrogate key confirms the argument
  pattern. One sample is not enough to generalize a parser from safely.

## Consequences

- Gets easier: the exclude-or-raise-bug UX and the exclusion write path are
  both fully reused from bronze — no new persistence format, no new JIRA
  integration code.
- Gets harder: any Silver table whose business key is macro-computed
  cannot be validated end-to-end without a manual step; this is a real
  limitation, not just a rough edge, until a future ADR builds the
  macro-argument parser.
- Watch for: **a future ADR needs a real second sample Coalesce node with a
  composite surrogate key** before automating ADR 0014 section 5's
  heuristic — do not attempt to generalize the parser from the one
  `INT_FACILITIES` sample alone.
