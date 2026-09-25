---
name: silver-layer-coalesce-validation
description: "Use when validating a Silver-layer Coalesce node against its Bronze source in Snowflake. Covers fetching a Coalesce node's metadata, classifying its columns (passthrough / recomputable expression / macro-skip / non-deterministic-existence-only), resolving surrogate business keys, multisource (multi-table-join) nodes, and turning the result into a CanonicalValidationPlan + SchemaDiff, then emitting Silver's own verbatim SQL (no base_rules.py wrapper). Files: coalesce_client.py, coalesce_plan_builder.py, silver_sql_emitter.py. See docs/decisions/0013-0019."
---

# Silver-Layer Validation via Coalesce Metadata

Silver validation is recompute-and-diff, not select-and-diff (ADR 0013):
rebuild the SELECT Coalesce's own metadata says the node runs, execute it
against the Bronze table already in Snowflake, and diff that against a
plain `SELECT *` on the materialized Silver table. Both sides are
Snowflake — there is no source-DB connector involved and no fuzzy/AI column
matching, unlike bronze validation.

## Files

- `src/connector/coalesce_client.py` — thin, read-only Coalesce REST client.
  `get_node(workspace_id: str, node_id: str) -> dict`. Env: `COALESCE_API_TOKEN`,
  `COALESCE_WORKSPACE_ID`, `COALESCE_API_BASE` (optional). Raises
  `CoalesceNotConfiguredError` / `CoalesceError`.
- `src/silver/coalesce_plan_builder.py` — the extractor.
  - `build_plan(node_id: str, workspace_id: Optional[str] = None) -> Tuple[CanonicalValidationPlan, SchemaDiff]`
    is the main entrypoint (signature/return shape unchanged by ADR
    0018/0019 — still single-node, called once per UI interaction). It is a
    thin wrapper: fetch via `coalesce_client.get_node()`, then call
    `build_plan_from_metadata(node: dict, workspace_id=None)` (ADR 0021) —
    the shared post-fetch logic, also the entrypoint for a caller who
    already has the node JSON (e.g. pasted from a manual curl call because
    they lack Snowflake access to run the live fetch/diff yet). Raises
    `UnsupportedNodeShapeError` only for `overrideSQL`, `customSQL`, an empty
    `sourceMapping`, or a `sourceMapping` entry with zero `dependencies`
    (ADR 0014 section 6) — hard-stop, never guess. `isMultisource=true` and
    more than one `sourceMapping`/`dependencies` entry are now **supported**
    (ADR 0019 1) — see "Multisource nodes" below.
  - `SchemaDiff` fields: `only_in_metadata`, `only_in_bronze_live`,
    `only_in_silver_live` (all `List[str]`) — declared metadata columns vs.
    live Snowflake columns on both sides, via
    `sql_extractor.extractors.ExtractorFactory`. For multisource nodes the
    Bronze-live side is the *union* of every dependency table's live columns
    (ADR 0019 5), not just the first one. `unavailable_reason: Optional[str]`
    (ADR 0021) is set instead of raising when the live diff query itself
    fails (e.g. no Snowflake grant yet on the Bronze database) — the three
    lists stay empty in that case; treat that as "not checked", never as
    "confirmed clean".
  - The 4-bucket column classification (ADR 0014 section 3) happens in
    `_classify_column()`: passthrough, recomputable expression, macro-skip
    (`skip_validation=True`), non-deterministic-existence-only
    (`skip_validation=True`, `validation_rules=["null_check"]`).
  - Surrogate-key PK fallback (ADR 0014 section 5, deferred per ADR 0015):
    when the business key is macro-computed, `source_primary_keys`/
    `target_primary_keys` are left empty, `plan.requires_review = True`,
    and the natural-key candidate columns are written to
    `plan.population_scope["natural_key_candidates"]` — not a third return
    value; `CanonicalValidationPlan` already has this free-form dict field.
  - `write_schema_diff_exclusion(db_type: str, column: str, reason: str) -> bool`
    — thin wrapper over `validate_cli.save_global_user_exclusion()`; does
    not duplicate the write logic.
- `src/silver/silver_sql_emitter.py` — Silver's own SQL emitter (ADR 0018).
  - `emit_query_set(plan: CanonicalValidationPlan) -> ValidationQuerySet` is
    the only entrypoint. It does **not** call `sql_query_generator.py` /
    `ai_sql_generator.py` / `rules/base_rules.py` at all — no
    `COALESCE(CAST(col AS STRING), '<<NULL>>')` wrapper, no type-normalization
    rule lookup. Per column: passthrough/recomputable → selected verbatim
    (`"<alias>"."<col>" AS "<target>"` or the raw transform expression AS
    target); macro-skip → not selected, left as a `-- <target>: PENDING -
    needs <macro> macro definition. Coalesce transform: ...` comment;
    non-deterministic/other-skipped → omitted entirely (existing
    `skip_validation`/`validation_rules=["null_check"]` mechanism handles the
    existence/NOT-NULL-only check, nothing new built for that). The returned
    `ValidationQuerySet` feeds `YAMLConfigWriter.write_from_plan()` unchanged.
  - `resolve_ref_macro(text: str, bronze_schema: str) -> str` — resolves every
    Coalesce `{{ ref('LOCATION', 'NODE') }}` macro to
    `"LOCATION"."bronze_schema"."NODE"`, everything else in `text` passes
    through verbatim. Shared by `coalesce_plan_builder.py` (multisource
    `bronze_join_sql` construction) — implemented once, not twice (ADR 0019).
  - `Project/main.py --layer_type silver` / `QueryOutputManager.generate_from_plan(plan, layer="silver")`
    still need one branch (owned by `validation-query-yaml-generator`, not
    this skill) to call `emit_query_set(plan)` instead of
    `SQLQueryGenerator().generate_from_plan(plan)` when `layer == "silver"` —
    see that agent's contract note.

## Multisource nodes (ADR 0019)

A Silver node's Bronze side can be a join across 2-3 Bronze tables (from the
same or different source DBs). `sourceMapping` may have N entries, each with
N `dependencies`; alias resolution (`_resolve_reference`) already worked
across every alias without change, it was only ever blocked by the
now-removed shape hard-stop. The Bronze recompute query's `FROM`/`JOIN`
clause is the **verbatim** concatenation of each `sourceMapping[].join.joinCondition`
string (macro-resolved via `resolve_ref_macro`), stored in
`plan.population_scope["bronze_join_sql"]` — no `RelationshipSpec`, no
parsing Coalesce's own join SQL into a structured join spec. Single-source
nodes (one `sourceMapping` entry, one dependency — the common case) don't
set this key at all; `silver_sql_emitter.py` falls back to the plain
single-table `FROM "db"."schema"."table"` it always built.

## Known limitation carried forward, not silently hidden

Recomputable-expression columns (bucket 2) store the raw Coalesce SQL
expression (e.g. a `ROW_NUMBER() OVER (...)` window function) verbatim in
`ColumnMappingEntry.source_column`, and `silver_sql_emitter.py` emits it
verbatim too (ADR 0018) — the previous concern ("`ai_sql_generator.py`'s rule
templates assume a bare identifier") no longer applies to Silver, since
Silver never calls `ai_sql_generator.py` at all now. That concern still
stands for **Bronze**, which does route through `ai_sql_generator.py` — not
this skill's problem to fix.

## webapp/app.py Silver sub-flow

Lives inside `with tab_batch:` ("Generate Batch YAML" tab), gated by a
top-level `st.radio("Layer", ["Bronze", "Silver"], key="batch_layer_flow")`
placed before the existing source-table-picking flow. This is a different
widget from the pre-existing `pick_layer(key)` selectbox (which only picks
the *output directory* for generated YAML, `bronze`/`silver`/`gold`) — the
Silver sub-flow uses both: `batch_layer_flow` picks which subflow runs,
`pick_layer("silver_layer_output")` still picks where the YAML lands
(`Project/config/silver/...`).

Bronze branch (`layer_flow == "Bronze"`): the entire pre-existing
Standard/Report-Pack flow, unchanged, just re-indented one level.

Silver branch (`layer_flow == "Silver"`), session-state keys and call sites:

- `silver_node_id` (`st.text_input`), `silver_workspace_id` (`st.text_input`,
  defaults to `COALESCE_WORKSPACE_ID` env var) — inputs for the fetch. Each
  node row also has a `st.radio` (key `silver_mode_{row_idx}`) to switch that
  row from "Node ID (fetch live)" to "Paste metadata JSON" (ADR 0021) — the
  pasted-JSON path calls `coalesce_plan_builder.build_plan_from_metadata()`
  directly instead of `build_plan()`, skipping the Coalesce API round-trip.
- `st.button("Fetch node and build plan", key="silver_fetch_btn")` calls
  `coalesce_plan_builder.build_plan(node_id, workspace_id=...)` and stores the
  result in `st.session_state["silver_plan"]` / `st.session_state["silver_schema_diff"]`
  (also resets `st.session_state["silver_diff_resolution"] = {}`), so the plan
  survives reruns instead of being rebuilt every render. Catches
  `CoalesceNotConfiguredError` and `UnsupportedNodeShapeError` with dedicated
  `st.error()` messages (same UX pattern as the existing
  `JiraNotConfiguredError` handling elsewhere in this file), and a generic
  `CoalesceError`/`Exception` fallback.
- Schema-diff resolution UI: one row per drifted column (from
  `SchemaDiff.only_in_metadata` / `.only_in_bronze_live` / `.only_in_silver_live`)
  inside `st.expander("Schema drift — resolve every column before generating")`.
  Two buttons per column, keyed `silver_excl_{group}:{column}` and
  `silver_bug_{group}:{column}`:
  - "Exclude" calls `write_schema_diff_exclusion(db_type, column, reason)`
    (`db_type` = `plan.source_db_type`, reason is the fixed string
    `"Coalesce/live schema drift — excluded via Silver validation UI"`).
  - "Raise Bug" calls the existing `connector.jira_client.create_ticket()`
    (same function/signature the rest of `app.py` already uses), pre-filled
    with the column, table, and drift-type.
  - Resolution state tracked in `st.session_state["silver_diff_resolution"]`,
    a `{"{group}:{column}": "excluded"|"bugged"}` dict.
- Macro-computed surrogate key case: when `plan.requires_review` and a
  `"macro-computed"` review reason is present, an `st.selectbox` (key
  `silver_natural_key_pick`) is populated from
  `plan.population_scope["natural_key_candidates"]`. On pick, sets
  `plan.source_primary_keys = [picked]` and `plan.target_primary_keys =
  [target_column or picked]` (uses the plan's own passthrough
  `target_column` for that source column if one exists, else assumes the
  same name — this assumption is surfaced via `st.caption`, not silent).
- `st.button("Generate YAML", key="silver_generate_btn")` is disabled until
  every schema-diff column is resolved and (if applicable) the natural key is
  picked. When enabled, calls
  `generated_queries.QueryOutputManager().generate_from_plan(plan, output_dir=output_dir, layer="silver")`
  directly — the same call `ValidationPipeline.run_with_plan()` already makes
  internally for Bronze once column-matching is done; Silver's plan is
  already fully built by `build_plan()`, so there is no matching step to run
  first.
