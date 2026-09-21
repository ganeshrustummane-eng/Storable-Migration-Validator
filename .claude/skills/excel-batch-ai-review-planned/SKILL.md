---
name: excel-batch-ai-review-planned
description: "Backend and UI for the Excel batch AI-preview feature are now implemented. Header-classification and filter-explain AI methods live on AISQLQueryGenerator (src/generated_queries/ai_sql_generator.py); load_excel()/derive_row_plan() in src/excel_batch_loader.py derive a per-row plan (tables, grain, filter SQL + English, join_needed). webapp/app.py:2408-2681 wires these into the 'Report Pack (AI Preview)' radio branch with a batch grid, per-row drill-down, and generate button."
---

# Excel-upload batch YAML generation with AI preview

## Status: backend and UI implemented

The backend pieces are built and self-checked:

- `src/generated_queries/ai_sql_generator.py`: `AISQLQueryGenerator.classify_headers()`
  (one AI call per sheet, header -> role incl. `unknown`) and
  `AISQLQueryGenerator.explain_and_derive_filter()` (prose/SQL filter ->
  `{filter_sql, filter_english, warnings}`). Both reuse the class's existing
  DIAL/Claude client selection -- no new AI backend.
- `src/excel_batch_loader.py`: `ReportSpec` gained `filter_condition` and
  `transformation_note` fields (default `""`). `load_excel()` keeps the regex
  fast-path unchanged and falls back to `classify_headers()` only for headers
  regex couldn't place, via new optional `unrecognized_out` and `ai_generator`
  params (both default `None` -- old callers unaffected). New `derive_row_plan()`
  returns `{tables, grain_columns, filter_english, filter_sql, join_needed, warnings}`
  per row without generating the actual comparison SQL.
- `src/validation_pipeline.py`: `run_with_plan()` gained an additive
  `candidate_keys: Optional[List[List[str]]] = None` param, threaded into the
  existing (previously always-empty) `CanonicalValidationPlan.candidate_keys` field.

**UI status**: `webapp/app.py:2408-2681` implements the "AI Preview" radio
branch, batch grid, per-row drill-down, and generate button described below.

For the plain "no AI" flow still used by today's UI, see the
**webapp-yaml-generation** skill -- `load_excel()`/`write_yaml()` with no
`unrecognized_out`/`ai_generator` args behave exactly as before this feature
was added.

## Resolved design decisions

1. UI: a third radio option next to "Standard"/"Report Pack (Excel)" --
   "Report Pack (AI Preview)" -- implemented at `webapp/app.py:2408-2681`.
2. Single-table rows route through `src/validation_pipeline.py`'s
   `run_with_plan()` (base rules -> learned rules -> AI-only-for-ambiguous),
   passing `derive_row_plan()`'s `grain_columns` as `candidate_keys`.
3. Multi-table/join rows keep using the existing
   `_generate_queries()` / `AISQLQueryGenerator.generate_schema_aware_query()`
   path unchanged -- joins are explicitly NOT routed through
   `CanonicalValidationPlan` (see `reference-filter-joins` skill: join support
   there is planned, not built).
4. Review/adjust is a hybrid: one batch grid + a per-row `st.expander`
   drill-down reusing `render_mapping_review()` for single-table rows.

## Remaining work

- [ ] Manual UI verification click-through (see the implementation plan for
  the exact steps).
