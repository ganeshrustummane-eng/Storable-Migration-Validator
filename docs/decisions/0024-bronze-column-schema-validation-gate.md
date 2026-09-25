# 0024. Bronze column-level schema validation gate (presence + type)

**Status:** Accepted
**Date:** 2026-09-25

## Context

Bronze YAML generation (`webapp/app.py` `tab_batch` Bronze branch) already
checks table-level presence (`src/core/table_presence_checker.py`, exclusion
via `config/exclusions.yaml` `table_exclusions`). At the column level,
`render_mapping_review()` (`webapp/app.py:874`) — the "Review columns" step
of the Bronze workflow — already previews exact/fuzzy/AI column matching per
table and flags anything the matcher couldn't confidently place: unmatched
columns, low-confidence matches, and skipped columns, each with a "Raise
Jira ticket" action. What it did NOT do:

1. Compare column **data types** between matched source and target columns
   at all (`preview_mapping()` already returns `source_type`/`target_type`
   per row — `src/validation_pipeline.py:409-435` — the review grid just
   never displayed or checked them).
2. Offer any way to say "I looked at this missing column / low-confidence
   match, it's fine" other than filing a Jira ticket — there was no
   persisted "reviewed, don't ask again" decision, so the same gap
   re-flags on every run.

Silver already solved the identical shape of problem for its own layer
(ADR 0015): a `SchemaDiff` + resolve-gate with Exclude/Raise-Bug actions,
persisted via `save_global_user_exclusion()`. Silver's diff has no
type-mismatch field because it's Snowflake-to-Snowflake; Bronze needs one
because it's cross-database.

## Decision

Extend `render_mapping_review()` rather than building a second
Silver-style diff/gate structure next to it — the review grid already has
every column's source/target type and match status in hand, so the type
check is a one-line normalize-and-compare, not a new extraction step.

- Add a **type-mismatch check**: for every row with a resolved target and
  not skipped, normalize both `source_type` and `target_type` (strip
  precision/scale, uppercase — same normalization `ColumnMetadata
  .normalized_type` already uses) and flag a mismatch. Informational only —
  `src/rules/rules_catalog.json` / `base_rules.py` compatibility isn't
  confirmed as the correctness bar yet, so this is a human-review flag, not
  an auto-fail.
- Add an **"✅ Mark OK" button** next to every existing flag group (missing
  column via `skipped_low`/`unmatched`, and the new type-mismatch group),
  alongside the existing "Raise Jira ticket" button — same UI slot, one more
  action. Persist the decision with the existing
  `save_global_user_exclusion(db_type, column, reason)`
  (`src/validate_cli.py`), under one new `db_type` key, `"bronze_schema"`,
  backed by a new `config/bronze_schema_exclusions.yaml`.
- On every subsequent preview, columns already marked OK
  (`_get_all_exclusions("bronze_schema")`) are filtered out of all three
  flag groups so a reviewed gap stops re-flagging.
- Not folded into the existing per-source-DB exclusion files
  (`postgresql_exclusions.yaml`, etc.): those exclude a column from *value*
  comparison; this is a distinct "known-acceptable schema gap" record — same
  rationale ADR 0015 used to justify `silver_exclusions.yaml` as its own
  file rather than reusing a source-DB file.
- Not gated as a hard block on YAML generation: `render_mapping_review()`'s
  flags are advisory today (same as the pre-existing unmatched/low-confidence
  warnings) — the test lead reviews before generating, but nothing stops
  generation if a flag is left unresolved. Revisit only if false confidence
  from an ignored flag turns out to be a real problem in practice.

## Alternatives considered

- Build a new `BronzeSchemaDiff` dataclass + a Silver-style resolver
  expander as a separate step from column-mapping review — rejected: the
  mapping-review grid already surfaces the same "column has no good home"
  cases (unmatched/low-confidence/skipped) that a fresh diff would just
  recompute; the only real gap was type comparison and a persisted OK,
  both small additions to the existing grid.
- Auto-fail generation on any missing column or type mismatch — rejected,
  the test lead explicitly wants a human-in-the-loop flag/approve step, and
  cross-database type mismatches are often legitimate (`NUMERIC(10,2)` vs
  `NUMBER`), not migration bugs.
- Store review decisions as a new `rule_book_learned.json` key instead of an
  exclusion YAML — rejected for v1: a flat exclude-by-column row is all this
  needs, and `save_global_user_exclusion` already does read-merge-write
  correctly for exactly this shape of record.

## Consequences

- Bronze's "Review columns" step now surfaces type drift, not just name
  drift, and a reviewed decision sticks instead of re-flagging every run.
- Type-mismatch flagging will be noisy until `rules_catalog.json`
  compatibility is confirmed — expect the first runs against each table to
  produce a batch of "Mark OK" clicks as the lead reviews known-fine cross-
  dialect type differences. That's the point: the record it leaves behind
  in `bronze_schema_exclusions.yaml` is the audit trail for future review.
- No new gate/blocking behavior was added — if this proves too easy to
  ignore in practice, a hard gate (mirroring Silver's `_all_resolved` check)
  is the natural next step, not a redesign.
