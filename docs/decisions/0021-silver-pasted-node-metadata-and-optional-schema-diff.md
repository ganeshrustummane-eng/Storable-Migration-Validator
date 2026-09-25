# 0021. Pasted Coalesce node metadata + optional/degradable schema diff

**Status:** Accepted
**Date:** 2026-09-25
**Follows on from:** [0014](0014-coalesce-metadata-extraction-rules.md), [0015](0015-silver-v1-schema-diff-gate-and-deferred-pk-resolution.md), [0019](0019-silver-multisource-nodes-and-batch-node-ui.md)

## Context

`build_plan(node_id, workspace_id)` does two live network calls: fetch the
node from the Coalesce REST API, then `_compute_schema_diff()` queries live
Snowflake to diff declared metadata columns against the Bronze and Silver
tables' actual columns. A user whose Snowflake role has access to Coalesce
metadata (proven via a manual curl against the Coalesce API, which never
touches Snowflake) but not yet to the specific Bronze database the node
declares (`locationName`, e.g. `BRONZE_EDGE` vs. their granted
`dev_edge_bronze`) hit an unhandled Snowflake authorization error from
`_compute_schema_diff()` that aborted `build_plan()` entirely — even though
the metadata fetch itself, and everything after the diff, needs no Bronze DB
access at all. This is a real, temporary access-grant lag (a lead's pending
grant), not a code bug, and confirmed reproducible independent of the node
itself (the same node's metadata fetches fine via curl).

The user asked to unblock on this directly: let them paste the same JSON
`coalesce_client.get_node()` would have returned (which they already have
from the curl call) and have the framework build the plan/SQL/YAML from
that, tolerating that live Bronze/Silver schema verification isn't possible
for them yet.

## Decision

1. **Factor `build_plan()`'s post-fetch logic into `build_plan_from_metadata(node: dict, workspace_id=None) -> (plan, schema_diff)`.**
   `build_plan(node_id, workspace_id)` is now a 2-line wrapper: call
   `coalesce_client.get_node()`, then `build_plan_from_metadata()`. No
   parsing/classification logic is duplicated between the two entrypoints —
   the pasted-metadata path and the live-fetch path share every line after
   the node dict is in hand, including cross-node `columnReferences`
   resolution (which still calls `coalesce_client.get_node()` for the
   *referenced* upstream node, only if the pasted node actually has one).

2. **`_compute_schema_diff()`'s call site catches any exception and degrades
   to `SchemaDiff.unavailable_reason` instead of raising.** A new optional
   field, `SchemaDiff.unavailable_reason: Optional[str] = None`, is set (and
   the three drift lists left empty — nothing was actually compared) when
   the live Snowflake diff query itself fails for any reason (auth, missing
   grant, network). This is a *different* state from "diff ran, found zero
   drift" — the field exists specifically so the UI can't conflate "not
   checked" with "confirmed clean". Plan/SQL/YAML generation is unaffected;
   only the diff step degrades.

3. **`webapp/app.py`'s Silver batch tab gets a per-row input-mode radio**
   ("Node ID (fetch live)" / "Paste metadata JSON"), additive to the existing
   node-ID-row list from ADR 0019. A pasted-JSON row is parsed with
   `json.loads` and routed to `build_plan_from_metadata()` directly, skipping
   the Coalesce API fetch; its schema-diff still runs against live Snowflake
   (still useful if the user has Silver-side access but not Bronze-side, or
   vice versa) and degrades per (2) rather than raising. When
   `SchemaDiff.unavailable_reason` is set, the UI shows an `st.warning` banner
   ("drift was NOT checked") instead of the "no drift found" success message,
   and does **not** gate the Generate-YAML button on it — only genuine
   unresolved drift (a non-empty drift list) still gates, exactly as before.

## Alternatives considered

- **Retry/backoff on the Snowflake call.** Rejected — this is an
  authorization gap (missing grant), not a transient failure; retrying
  can't fix it and just delays the same error.
- **A separate `build_plan_offline()` that skips schema diff entirely.**
  Rejected — the diff should still be attempted (it may partially succeed,
  e.g. Silver-side access without Bronze-side), and "optional, degrades
  gracefully" is a smaller/more honest contract than "never even tries".
- **Silently treat a diff failure as "no drift".** Rejected outright — would
  let a real, unverified schema drift slip through disguised as a clean
  diff; CLAUDE.md's Consistency/Completeness bar requires the gap be visible,
  not hidden.

## Consequences

- Gets easier: a user blocked purely on a pending Snowflake grant can still
  generate Silver validation YAML today, from metadata they already have.
- Gets harder: nothing structurally — `build_plan()`'s public signature is
  unchanged, and `build_plan_from_metadata()` is additive.
- Watch for: `SchemaDiff.unavailable_reason` must keep being treated as "not
  checked" everywhere it's read (today only `webapp/app.py`) — don't let a
  future caller default it to "clean" by only checking `only_in_*` list
  emptiness without also checking this field.
