# 0019. Support `isMultisource` Coalesce nodes and a Streamlit batch-node UI

**Status:** Accepted
**Date:** 2026-09-24
**Follows on from:** [0014](0014-coalesce-metadata-extraction-rules.md) (declared `isMultisource=true` out of
scope), [0018](0018-silver-sql-verbatim-transform-no-generic-normalization.md) (verbatim-transform
principle this ADR extends to joins)

## Context

0014 hard-stopped (`UnsupportedNodeShapeError`) on any node where
`metadata.isMultisource` is `true`, or where `sourceMapping` has more than
one entry, or a `sourceMapping` entry has more than one `dependencies[]`
entry — i.e. any Silver node whose Bronze side is actually a join across
more than one Bronze table (potentially from different source DBs, per the
user's example: Bronze can have 2-3 different source-DB origins that get
resolved together in a single Silver node). The user asked for this shape to
be implemented now, not deferred further, plus a Streamlit-side way to queue
several Coalesce node IDs (not just one) for a single Silver validation
batch.

Inspected a real Coalesce node payload (`docs/metadata_1.txt`,
`INT_FACILITIES`) to confirm the actual shape rather than guessing:

- `isMultisource` is a top-level boolean on the node (`"isMultisource":false`
  in the sample).
- `metadata.sourceMapping` is an array. Today's single-source code assumes
  exactly one entry with exactly one `dependencies[]` entry. For a
  multisource node, the realistic shape (per Coalesce's own docs and the
  `RelationshipSpec` comment already in 0014 anticipating this) is either
  multiple `sourceMapping[].dependencies[]` entries or multiple
  `sourceMapping[]` entries — both resolve to: more than one Bronze table
  feeding this node, joined together.
- Each `sourceMapping` entry already carries a `join.joinCondition` string —
  in the sample this is `"FROM {{ ref('BRONZE_EDGE', 'FACILITIES') }}
  \"FACILITIES\""`, i.e. Coalesce's own literal `FROM`/`JOIN` SQL fragment
  (with `{{ ref(...) }}` macros for table references), not a structured
  column-list join spec.
- Column `sources[].columnReferences[]` already reference `{nodeID,
  columnID}` pairs resolved against the `aliases` map — this mechanism
  already generalizes to multiple aliases/dependencies without change
  (`_resolve_reference` in `coalesce_plan_builder.py` loops `alias_map`
  looking for the matching `nodeID`; it was only ever blocked by the
  upstream `_check_supported_shape` guard, not by anything in the resolution
  logic itself).

## Decision

1. **Drop the `isMultisource` / multi-dependency hard-stop in
   `_check_supported_shape`.** Keep the `overrideSQL` / `customSQL` hard-stops
   (still genuinely unsupported — no declared column-level metadata to
   classify against). Allow `sourceMapping` to have N entries and each entry
   to have N `dependencies`.
2. **Resolve every `dependencies[]` entry into the alias map exactly as
   today** — `_upstream_columns_by_id` / `_resolve_reference` already key off
   `nodeID`, not off "the one dependency," so no change needed there beyond
   removing the length-1 assertions.
3. **The Bronze recompute SQL's `FROM`/`JOIN` clause is the concatenation of
   each `sourceMapping[].join.joinCondition` string, verbatim, after
   resolving `{{ ref(locationName, nodeName) }}` macros to the real
   `"<database>"."<schema>"."<table>"` (or aliased) reference** — the same
   verbatim principle ADR 0018 already established for column transforms,
   extended to joins: **we do not parse `joinCondition` into a structured
   join spec (no `RelationshipSpec` here) and do not infer join keys
   ourselves.** `RelationshipSpec` stays reserved for its existing use
   (population-scope joins driven by NL filter conditions, per the
   `reference-filter-joins` skill) — forcing Coalesce's raw join SQL through
   that column-list shape would require parsing the `ON` clause, which is
   exactly the kind of inference 0018 rejected for transforms.
4. **Store the resolved join SQL fragment in
   `plan.population_scope["bronze_join_sql"]`** (free-form dict field,
   already used for `natural_key_candidates` — no new dataclass field, per
   CLAUDE.md's no-speculative-abstraction rule and the existing precedent in
   `build_plan()`'s own docstring). The Bronze SQL emitter (0018 §1) reads
   this instead of building a single-table `FROM` clause when it's present;
   falls back to today's single-table `FROM "db"."schema"."table"` when
   `sourceMapping` has exactly one entry with exactly one dependency (the
   common case stays untouched — no regression risk for existing
   single-source nodes).
5. **`SchemaDiff` computation is unaffected** — it already only needs the
   Bronze *Silver-declared* column names against live Snowflake columns, not
   the join shape; it currently reads `dependencies[0]` for the live-column
   lookup target, which for multisource must become "every dependency's live
   columns, unioned" so the diff checks against the full multi-table column
   surface instead of just the first table.
6. **Streamlit UI: multi-node batch input on the existing Silver
   radio (`batch_layer_flow` in `webapp/app.py`'s `tab_batch`).** Replace the
   single node-ID text input with a small repeatable list (Streamlit
   `st.session_state`-backed list of node-ID rows + an "Add node" button,
   matching the existing repeatable-row pattern already used elsewhere in
   this file — no new UI library). Each row still goes through the existing
   per-node flow unchanged (`build_plan()` → schema-diff gate → human
   key/exclusion resolution → `generate_from_plan()`); batching only saves
   the user from re-opening the tab per node. This is a UI convenience, not a
   new backend concept — `build_plan(node_id, workspace_id)` is already
   single-node and stays that way; the UI simply calls it once per row in the
   list.

## Alternatives considered

- **Parse `joinCondition` into `RelationshipSpec`(s) so Silver joins reuse the
  same structured shape as NL-filter joins.** Rejected — `joinCondition` is
  Coalesce's own already-correct SQL text; parsing it into columns and
  rebuilding a `JOIN ... ON` clause ourselves is strictly more code and more
  risk (a parse bug silently changes the join) for zero benefit over emitting
  it verbatim, which is also the 0018 precedent.
- **A dedicated "multi-node batch" backend function
  (`build_plans(node_ids, workspace_id)`) instead of a UI-side loop.**
  Rejected for v1 — nothing behind `build_plan()` needs to know about "a
  batch"; each node is validated independently start to finish (own
  schema-diff, own key resolution, own YAML). A backend batch function would
  be a speculative abstraction with one caller.

## Consequences

- Gets easier: the Bronze→Silver validation flow now covers the real-world
  case the user described (Bronze assembled from 2-3 source-DB origins,
  joined before landing in Silver) instead of hard-stopping on it.
- Gets harder: the Bronze SQL emitter has two code paths (single-table `FROM`
  vs. multi-table verbatim `joinCondition` concatenation) instead of one —
  acceptable since the single-table path is untouched and is still the
  common case.
- Watch for: `{{ ref(locationName, nodeName) }}` macro resolution for
  multisource joins reuses whatever resolver 0018's Bronze emitter already
  needs for single-source `ref()` calls — implement that resolver once, not
  twice, when 0018 and this ADR land in the same pass.
