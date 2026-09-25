# 0025. Bronze schema validation: explicit column-count check + dedicated UI section

**Status:** Accepted
**Date:** 2026-09-25

## Context

ADR 0024 added a type-mismatch check and a persisted "Mark OK" action into
`render_mapping_review()` ([webapp/app.py:874](../../webapp/app.py)). Using
it surfaced two real gaps, both raised directly by the test lead after
reviewing 0024's result:

1. **No explicit column-count check.** The original ask was "number of
   columns from source and target table are correctly mapping." Today the
   grid shows one row per *source* column and flags source columns with no
   target match (`unmatched`/`skipped_low`), but it never reports the
   reverse: target (Snowflake) columns that exist live but aren't claimed by
   any source column. `live_tgt_cols` is already fetched
   (`webapp/app.py:923`, via `cached_sf_columns()`) but only used to populate
   the "Corrected Target" dropdown — the "columns Snowflake has that nothing
   maps to" list is silently discarded. This matters because it's the other
   half of "did every column migrate": a source column with no target is one
   failure mode, a target column nothing populates (dead/renamed-without-a-
   mapping-fix) is the other.
2. **No dedicated "this is schema validation" UI.** Everything from 0024 —
   type mismatches, missing columns, Mark-OK buttons — is folded into the
   same visual flow as the exact/fuzzy/AI name-mapping review
   (`st.data_editor` grid + inline warnings). A user looking at the tab sees
   "column mapping," not "schema validation is happening here, and here are
   its results." There's no single place that says, in plain terms, "N
   source columns / M target columns / this many matched / this many don't."

## Decision

1. **Add a target-only-columns check.** Compute
   `extra_in_target = live_tgt_cols - {matched target columns} - Fivetran
   metadata columns (STATIC_EXCLUDE_COLUMNS)`. Report it the same way the
   existing missing-column groups are reported: flagged, with a "Mark OK"
   button (reusing the `bronze_schema` exclusion key from 0024) and a "Raise
   Jira ticket" button, filtered against already-reviewed
   `bronze_schema` exclusions like every other flag group.
2. **Give schema validation its own visible section**, rendered as soon as
   "Preview column mapping" runs, *before* the editable mapping grid:
   - A one-line count summary as the section header, e.g. `Source columns:
     42  |  Target columns: 44  |  Matched: 39  |  Missing in target: 3  |
     Extra in target: 2  |  Type mismatches: 1`.
   - A single status line: `✅ Schema validation: no issues` or `⚠️ Schema
     validation: 4 issue(s) need review` (count = sum of all unresolved flag
     groups after the `bronze_schema`-exclusion filter).
   - All four flag groups (missing-in-target, extra-in-target,
     low-confidence, type-mismatch) live inside this section, each with its
     existing Mark-OK / Raise-Jira actions — moved here from being scattered
     inline against the grid, not re-implemented.
   - The editable name-correction grid (`st.data_editor`) keeps its own
     heading below this section (e.g. "Column mapping (editable)") so it's
     visually distinct from the validation summary above it.
3. Everything computed and persisted by ADR 0024 (type-mismatch normalization,
   `save_global_user_exclusion("bronze_schema", ...)`, the read-filter against
   prior decisions) is reused as-is — this ADR only adds the missing
   target-only-column check and reorganizes existing pieces under one visible
   heading. No new persistence mechanism, no new exclusion file.

## Alternatives considered

- Leave column-count visibility as an implicit read of the existing grid
  (count rows, scan for blanks) — rejected, that's exactly what the test
  lead said is confusing: nothing states "schema validation happened, here's
  the count," so a reviewer has to reconstruct it by eye every time.
- Build target-only-column detection as part of `CandidateMatcher`/
  `MatchDecision` (the backend matching layer) instead of computing it in the
  UI from `live_tgt_cols` — rejected: `CandidateMatcher` operates per source
  column and has no reason to enumerate "target columns nobody claimed"; the
  UI already has the full live target column list in hand from the existing
  `cached_sf_columns()` call, so computing a set difference there is the
  smaller, correctly-scoped change.
- A separate tab/page for "Schema Validation" instead of a section inside the
  existing Bronze batch flow — rejected: schema validation only makes sense
  in the context of a specific table's mapping preview, which already lives
  in `tab_batch`; splitting it into another tab would force re-fetching the
  same preview data or passing it across tabs for no benefit.

## Consequences

- A reviewer gets one section that answers "is the schema OK" without
  reading the whole mapping grid, plus the previously-invisible
  extra-in-target case now has the same review/exclude workflow as every
  other flag.
- `render_mapping_review()` grows another flag group and a summary-line
  computation, but no new backend module, no new exclusion mechanism, and no
  new call site duplicating what 0024 already does — the diff from 0024 is
  additive, not a rewrite.
- Still advisory, not a hard gate on generation (same as 0024) — revisit that
  only if reviewers demonstrably skip past the summary without acting on it.
