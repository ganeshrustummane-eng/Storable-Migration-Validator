---
name: silver-layer-coalesce-validation
description: "Use when validating a Silver-layer Coalesce node against its Bronze source in Snowflake. Covers fetching a Coalesce node's metadata, classifying its columns (passthrough / recomputable expression / macro-skip / non-deterministic-existence-only), resolving surrogate business keys, and turning the result into a CanonicalValidationPlan + SchemaDiff. Files: coalesce_client.py, coalesce_plan_builder.py. See docs/decisions/0013, 0014, 0015, 0016."
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
    is the only public entrypoint. Raises `UnsupportedNodeShapeError` for
    `isMultisource`, more than one `sourceMapping`/`dependencies` entry,
    `overrideSQL`, or `customSQL` (ADR 0014 section 6) — hard-stop, never guess.
  - `SchemaDiff` fields: `only_in_metadata`, `only_in_bronze_live`,
    `only_in_silver_live` (all `List[str]`) — declared metadata columns vs.
    live Snowflake columns on both sides, via
    `sql_extractor.extractors.ExtractorFactory`.
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

## Known limitation carried forward, not silently hidden

Recomputable-expression columns (bucket 2) store the raw Coalesce SQL
expression (e.g. a `ROW_NUMBER() OVER (...)` window function) verbatim in
`ColumnMappingEntry.source_column`. `ai_sql_generator.py`'s existing rule
templates (e.g. `TextRule`'s `TRIM(col)`) assume `source_column` is a plain
column identifier, not an arbitrary expression, and will wrap it
incorrectly if a type-normalization rule other than a no-op is applied.
Verify/extend `ai_sql_generator.py`'s handling of expression-valued
`source_column` entries before wiring end-to-end SQL generation for
Silver — this was out of scope for the `build_plan()` work this skill
documents.

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
  defaults to `COALESCE_WORKSPACE_ID` env var) — inputs for the fetch.
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
