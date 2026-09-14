# Plan 002 — Add AI-generated Snowflake SQL in the Custom SQL Validation tab

**Written against commit:** `cb3c859`  
**Status:** TODO  
**Depends on:** Plan 001 (fix `source:` field) — not a hard blocker, but ship 001 first  
**Effort:** S (~1–2 hours)  
**Risk:** Low — additive UI change; no pipeline or schema changes  

---

## Why this matters

The Custom SQL tab's AI generator (Step 4, `webapp/app.py:2011–2197`) currently
produces **only source-side SQL**. When the user clicks "➕ Add as validation entry",
only `entry["source_sql"]` is populated; `entry["target_sql"]` is blank. The flash
message at line 2186 explicitly tells the user to "add the Snowflake SQL there" —
they must write it by hand.

`AISQLQueryGenerator.generate_custom_query(prompt, table_fqn, cols, "snowflake")`
already exists and works. This plan wires it into the UI so that after generating
source SQL, the user can immediately generate the matching Snowflake SQL with one
click — using the same English prompt and the mapped target column names.

This also applies to the **`render_custom_sql_section`** expander (used in the
single-table and batch YAML tabs, `webapp/app.py:1029–1175`), which already has a
"Both sides" radio option but has no way to populate a matching Snowflake entry into
the Custom SQL tab's entry list.

---

## Architecture: how the two SQL generation surfaces work

There are **two separate surfaces** in the app:

### Surface A — Custom SQL Validation tab (Step 4, lines 2011–2197)
- User picks source tables, types a plain-English prompt.
- Calls `generate_schema_aware_query(prompt, schema_context, db_type=cst_db_type)`.
- Returns source SQL only. Stored in `st.session_state["cst_ai_result_sql"]["sql"]`.
- User clicks "➕ Add as validation entry" → `entry["source_sql"]` is set, `entry["target_sql"]` is blank.

### Surface B — render_custom_sql_section expander (lines 1029–1175)
- Embedded in single-table and batch YAML tabs, after the column mapping grid.
- Has radio "Source only / Snowflake only / Both sides".
- When "Both sides" selected, already calls `generate_custom_query` for both sides.
- Saves `.sql` files but does **not** feed into the Custom SQL tab's `_CST_KEY` entry list.

This plan targets **Surface A** only. Surface B already works correctly.

---

## What to build

### Part 1 — After AI generates source SQL, also offer "Generate Snowflake SQL" button

**Location:** `webapp/app.py`, inside the `if _ai_result:` block starting at line 2163.

Currently the block shows:
1. Generated source SQL (`st.code`)
2. Confidence badge
3. "➕ Add as validation entry below" button
4. "🔄 Regenerate" button
5. `st.info` telling user to manually add Snowflake SQL

**Change:** Between the `st.code` and the buttons row, add a new collapsible section:

```
▸ Also generate matching Snowflake SQL (optional)
  [Snowflake table FQN]  [Generate Snowflake SQL ✨]
  <code block when result available>
```

When the user clicks "Generate Snowflake SQL":
1. Prompt user for the Snowflake schema + table name (or auto-populate from Step 2 fields `cst_sf_database` / `cst_sf_schema` if set, with a text input for the table name).
2. Map source columns to Snowflake column names: for each source column in `_schema_ctx`, use the column name as-is for Snowflake (since the user hasn't run a mapping preview here, we cannot auto-map — explain this in the UI caption).
3. Call `AISQLQueryGenerator(model=ai_model).generate_custom_query(prompt, sf_table_fqn, sf_cols, "snowflake")`.
4. Store result in `st.session_state["cst_ai_sf_result_sql"]`.

### Part 2 — "➕ Add as validation entry" populates BOTH sides when Snowflake SQL is available

Modify the "➕ Add as validation entry below" button handler (lines 2176–2187):

```python
# Current (line 2181):
new_entry["source_sql"] = _ai_result["sql"]

# New:
new_entry["source_sql"] = _ai_result["sql"]
sf_result = st.session_state.get("cst_ai_sf_result_sql")
if sf_result:
    new_entry["target_sql"] = sf_result["sql"]
    st.session_state.pop("cst_ai_sf_result_sql", None)
```

Also update the flash message at line 2186:
- If both sides are set: `"Added '{name}' — review the SQL in the entry below."`
- If only source set: `"Added '{name}' — add the Snowflake SQL in the entry below."` (unchanged)

### Part 3 — Source SQL placeholder dialect-aware hints (minor, no logic change)

**Location:** `webapp/app.py` lines 2266–2274 (the `placeholder=` argument in the
source SQL text area).

Currently the placeholder always shows PostgreSQL `DATE_TRUNC('month', order_date)`
syntax regardless of the actual `cst_db_type`.

Add a small helper dict (put it near the `_DB_TYPE_LABELS` dict that already exists
in the file — search for `_DB_TYPE_LABELS`):

```python
_DB_TYPE_SQL_HINTS = {
    "mssql":      "SELECT\n    FORMAT(order_date, 'yyyy-MM') AS month,\n    region,\n    SUM(amount) AS total_sales\nFROM dbo.sales s\nJOIN dbo.dim_customer c ON s.customer_id = c.id\nGROUP BY FORMAT(order_date, 'yyyy-MM'), region",
    "athena":     "SELECT\n    date_trunc('month', order_date) AS month,\n    region,\n    SUM(amount) AS total_sales\nFROM schema.sales s\nJOIN schema.dim_customer c ON s.customer_id = c.id\nGROUP BY 1, 2",
    "postgresql": "SELECT\n    DATE_TRUNC('month', order_date) AS month,\n    region,\n    SUM(amount)                    AS total_sales\nFROM sales s\nJOIN dim_customer c ON s.customer_id = c.id\nGROUP BY 1, 2",
}
_DEFAULT_SRC_HINT = _DB_TYPE_SQL_HINTS["postgresql"]
```

Then change line 2266's `placeholder=` argument from the hardcoded string to:
```python
placeholder=_DB_TYPE_SQL_HINTS.get(cst_db_type, _DEFAULT_SRC_HINT),
```

---

## Exact file locations to change

All changes are in `webapp/app.py`. No changes to `src/` are needed.

| What | Line(s) | Change type |
|---|---|---|
| Add "Generate Snowflake SQL" sub-section | After line 2172 | Add ~40 lines |
| Modify "Add as validation entry" handler | Lines 2176–2187 | Modify ~8 lines |
| Update flash message | Line 2186 | Modify 1 line |
| Add `_DB_TYPE_SQL_HINTS` dict | Near `_DB_TYPE_LABELS` (search the file) | Add ~8 lines |
| Use dialect-aware placeholder | Line 2266 | Modify 1 line |

---

## Detailed implementation for Part 1

Find the `if _ai_result:` block at line 2163. After the `st.code(_ai_result["sql"], language="sql")` line (2172), insert:

```python
                # ── Optional: generate matching Snowflake SQL ────────────────
                with st.expander("🏔️ Also generate matching Snowflake SQL (optional)", expanded=False):
                    st.caption(
                        "Uses the same plain-English prompt to generate the Snowflake-side query. "
                        "Column names are taken from the source schema — correct any renamed columns "
                        "in the entry editor below after adding."
                    )
                    _sf_tbl_default = ""
                    if cst_sf_database and cst_sf_schema:
                        _sf_tbl_default = f"{cst_sf_database}.{cst_sf_schema}."
                    _sf_fqn_input = st.text_input(
                        "Snowflake table FQN (e.g. MYDB.PUBLIC.SALES)",
                        value=_sf_tbl_default,
                        key="cst_ai_sf_table_fqn",
                        placeholder="DATABASE.SCHEMA.TABLE_NAME",
                    )
                    _sf_gen_btn = st.button(
                        "✨ Generate Snowflake SQL",
                        key="cst_ai_sf_generate",
                        disabled=not _sf_fqn_input.strip(),
                    )
                    if _sf_gen_btn and _sf_fqn_input.strip():
                        # Build column list from source schema context —
                        # use source column names as best guess for Snowflake names.
                        _sf_cols = [
                            {"column": col["column_name"], "type": col["data_type"]}
                            for tbl_cols in _schema_ctx.values()
                            for col in tbl_cols
                        ]
                        with st.spinner("Generating Snowflake SQL…"):
                            try:
                                _sf_gen = AISQLQueryGenerator(model=ai_model)
                                _sf_result = _sf_gen.generate_custom_query(
                                    user_instruction=_ai_result["prompt"],
                                    table_fqn=_sf_fqn_input.strip(),
                                    columns=_sf_cols,
                                    db_type="snowflake",
                                )
                                st.session_state["cst_ai_sf_result_sql"] = {
                                    "sql": _sf_result.query,
                                    "confidence": _sf_result.confidence,
                                    "fqn": _sf_fqn_input.strip(),
                                }
                            except AISQLGenerationError as _exc:
                                st.error(f"Snowflake SQL generation failed: {_exc}")
                                st.session_state.pop("cst_ai_sf_result_sql", None)

                    _sf_ai_result = st.session_state.get("cst_ai_sf_result_sql")
                    if _sf_ai_result:
                        st.markdown(
                            f"<div style='font-size:0.8rem;font-weight:600;color:#059669;margin-bottom:4px;'>"
                            f"Generated Snowflake SQL "
                            f"<span style='color:#64748B;margin-left:8px;'>confidence {int(_sf_ai_result['confidence']*100)}%</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                        st.code(_sf_ai_result["sql"], language="sql")
```

---

## Verification

1. Run: `streamlit run webapp/app.py`
2. Go to **✍️ Custom SQL Validation** tab.
3. Select a PostgreSQL (or MSSQL or Athena) connection.
4. In Step 4, select a table, type "find customers with duplicate email addresses".
5. Click **✨ Generate SQL** — confirm source SQL appears.
6. Expand **🏔️ Also generate matching Snowflake SQL**.
7. Enter a valid Snowflake FQN (e.g. `MYDB.PUBLIC.CUSTOMERS`).
8. Click **✨ Generate Snowflake SQL** — confirm Snowflake SQL appears using `TRIM`, `CAST(... AS STRING)`, etc.
9. Click **➕ Add as validation entry below** — confirm the new entry in Step 5 has BOTH `source_sql` and `target_sql` pre-filled.
10. In Step 6 (YAML preview), confirm both `sourcequery` and `targetquery` are present and non-empty.

**Also verify dial-specific hint (Part 3):**
11. Switch connection to MSSQL, revisit the Source SQL text area — confirm placeholder uses `FORMAT(order_date, 'yyyy-MM')` syntax.
12. Switch to Athena — confirm placeholder uses `date_trunc` (lowercase).

---

## Files in scope

- `webapp/app.py` — five locations listed in the table above.

## Files explicitly out of scope

- `src/generated_queries/ai_sql_generator.py` — `generate_custom_query` already handles `"snowflake"` dialect correctly; no changes needed.
- `src/generated_queries/yaml_config_writer.py` — not involved.
- `src/validation/config_schema.py` — not involved.
- Any other tab or pipeline file.

---

## Maintenance note

`_schema_ctx` (built at line 2112 from selected source tables) holds source column
names. Snowflake column names are often UPPER_CASE renames of the source names. The
generated Snowflake SQL will use source column names, which may be wrong after
rename-heavy migrations. The UI caption already tells the user to fix renames in the
entry editor. If you later add a column-mapping lookup (e.g. reading from the
approval store), you can replace the `_sf_cols` list with mapped target names — the
`generate_custom_query` call signature does not change.
