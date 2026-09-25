# 0020. Whether Silver's Bronze-recompute SQL should filter `_FIVETRAN_ACTIVE = TRUE`

**Status:** Investigation only — awaiting user/product decision before any code change
**Date:** 2026-09-24
**Follows on from:** [0018](0018-silver-sql-verbatim-transform-no-generic-normalization.md), [0019](0019-silver-multisource-nodes-and-batch-node-ui.md)

## Context

CLAUDE.md states that `_FIVETRAN_ACTIVE = TRUE` must always be applied as a
filter on the Snowflake side so only the latest active record per key is
compared — written with Bronze-source-DB→Snowflake validation in mind. When
implementing ADR 0018/0019's Silver Bronze-recompute SQL emitter
(`src/silver/silver_sql_emitter.py`), this filter was not added, because
neither ADR mentions it and a verification pass was asked to establish
whether it's actually correct here rather than assume the CLAUDE.md rule
transfers unchanged to Silver's Snowflake-to-Snowflake pipeline. The user
separately stated a strong prior that the filter (or its semantic under
whatever real column name applies) "must be there" while generating the SQL.

The verification pass gathered concrete evidence instead of resolving this by
assumption in either direction:

1. **Bronze's existing behavior is conditional, not a blanket rule.**
   `src/validation_pipeline.py:476-477` auto-detects the filter via
   `SnowflakeExtractor.has_fivetran_active(tgt_columns)` — it only applies
   when the live Snowflake table actually has the column — and
   `src/generated_queries/sql_query_generator.py:525-533`'s `_where()`
   appends it only to the **Snowflake-side** query (never the source-DB
   side). So even for Bronze, "always filter" is really "filter the
   Snowflake side, conditionally on column presence."
2. **The one real Silver node sample proves this filter would be actively
   wrong for that node.** `docs/metadata_1.txt`'s `INT_FACILITIES` node
   declares:
   - `EFFECTIVE_FROM = TO_TIMESTAMP_NTZ("FACILITIES"."_FIVETRAN_START")`
   - `EFFECTIVE_TO = TO_TIMESTAMP_NTZ("FACILITIES"."_FIVETRAN_END")`
   - `IS_CURRENT = "FACILITIES"."_FIVETRAN_ACTIVE"`

   This is a **deliberately SCD-shaped** Silver table: it re-exposes
   Fivetran's own activity/history as first-class Silver columns and is
   built to retain every historical Bronze row (active and superseded), not
   just the current one. If the Bronze-recompute SQL added
   `WHERE _FIVETRAN_ACTIVE = TRUE`, every Bronze row with
   `_FIVETRAN_ACTIVE = FALSE` would silently disappear from the comparison —
   but those are exactly the rows this Silver table is designed to keep as
   `IS_CURRENT = FALSE` history. The result: real, correctly-migrated Silver
   rows would show up as false `TARGET_ONLY` mismatches. This is precisely
   the "asymmetric filter produces false mismatches that look like
   data-quality bugs but are actually validator bugs" failure mode CLAUDE.md's
   Consistency dimension warns about — plus a Completeness violation (Bronze
   rows that did migrate would be excluded from the comparison population).
3. **Coalesce's own metadata gives no signal to distinguish node shapes.**
   There is no flag saying "this Silver node wants latest-active-only Bronze
   rows" vs. "this Silver node is SCD/history-preserving over Fivetran
   activity." The only observable signal today is a heuristic pattern-match
   on declared column names/transform text (`IS_CURRENT`/`EFFECTIVE_FROM`/
   `EFFECTIVE_TO` derived from `_FIVETRAN_*`) — and inferring node intent from
   that pattern is exactly the kind of guessing ADR 0014/0018 already
   rejected in favor of trusting declared metadata verbatim.

## Decision

**Not resolved yet — no filter added, nothing else changed.** This ADR
records the investigation and evidence so the next pass doesn't have to
re-derive it, and states the decision this needs before any code is written:

Either:

- **(a) Metadata/config-driven per-node flag.** Add an explicit,
  human-set-or-Coalesce-declared signal distinguishing "latest-active-only"
  Silver nodes from "SCD/history-preserving" ones (e.g. a config entry
  alongside the existing per-node exclusion/business-key overrides already
  used in the Silver UI flow), and have the Bronze emitter apply the filter
  only when that flag says so — never inferred from column-name heuristics.
- **(b) Confirm no Silver node needs it.** Check with whoever owns the
  Coalesce node designs whether every Silver node in practice either (i) is
  SCD-shaped like `INT_FACILITIES` (filter would be wrong) or (ii) already
  has Coalesce's own materialization deciding what history to keep before it
  ever reaches Silver (making a Bronze-side filter redundant either way). If
  true for the real node population, this ADR closes with "no filter,
  anywhere, ever" and CLAUDE.md's rule is understood to apply to
  source-DB→Bronze validation only, not Bronze→Silver recompute.

Whichever direction, do not add a blanket `_FIVETRAN_ACTIVE = TRUE` filter to
`silver_sql_emitter.py` in the meantime — the one concrete sample available
proves it would produce false failures, not catch real ones.

## Alternatives considered

- **Add the filter now, unconditionally, per the user's stated prior.**
  Rejected for now — the evidence from the one real node sample shows this
  would actively break validation for at least that node (false
  `TARGET_ONLY` rows), which is worse than leaving the question open.
- **Infer node shape from column-name pattern
  (`IS_CURRENT`/`EFFECTIVE_FROM`/`EFFECTIVE_TO`) automatically.** Rejected —
  not a declared, authoritative signal; would silently misclassify any
  future node that doesn't happen to use those exact names, and repeats the
  inference mistake ADR 0014/0018 already ruled out for transforms/joins.

## Consequences

- Gets easier: nothing yet — this is intentionally a non-change. The
  benefit is not re-litigating the same investigation in a future pass.
- Gets harder: Silver validation currently has no active/inactive filtering
  at all on the Bronze side, so a genuinely latest-active-only Silver node
  (if one exists in the real node population) would currently compare
  against superseded Bronze rows too — a false-failure risk in the *other*
  direction until (a) or (b) above is resolved.
- Watch for: this ADR should be updated (or superseded) as soon as either a
  second real Coalesce node sample or a decision from the Coalesce/Silver
  team resolves which case actually applies — don't let it sit as
  "investigation only" indefinitely once more node shapes are known.
