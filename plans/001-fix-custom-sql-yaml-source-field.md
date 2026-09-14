# Plan 001 — Fix YAML `source:` field in Custom SQL Validation tab

**Written against commit:** `cb3c859`  
**Status:** TODO  
**Effort:** XS (~10 min, 2-line change)  
**Risk:** Low — isolated to one tab's YAML serialization block  

---

## Why this matters

The Custom SQL Validation tab builds YAML that is later executed by `Project/main.py`
via the validation executor. The executor loads the YAML and passes it through
`src/validation/config_schema.py`, which validates every `source:` field against an
allowlist:

```python
# src/validation/config_schema.py:40
SUPPORTED_SOURCES = {"postgresql", "postgres", "mssql", "sqlserver", "athena", "snowflake"}
```

But the tab currently writes `source: src_0` (or `src_1`, etc.) — a connection-index
key, not a dialect name. This will fail the `_known_dialect` Pydantic validator at
runtime with a confusing error every time anyone tries to execute a Custom SQL YAML.

---

## Root cause (read this before touching anything)

`webapp/app.py` lines 2329 and 2334:

```python
# webapp/app.py:2329
src_key = f"src_{cst_rec['index']}"   # e.g. "src_0" — NOT a dialect name

# webapp/app.py:2334
block = {
    "source": src_key,          # BUG: writes "src_0" instead of "postgresql" etc.
    "sourcequery": ...,
    "target": "snowflake",
    "targetquery": ...,
}
```

`cst_db_type` (defined at line 1958 from `cst_rec["db_type"]`) already holds the
correct dialect string (`"postgresql"`, `"mssql"`, `"athena"`, etc.).

---

## The fix

**File:** `webapp/app.py`

**Step 1 — Remove the unused `src_key` variable** (line 2329):

Delete this line entirely:
```python
src_key = f"src_{cst_rec['index']}"
```

**Step 2 — Use `cst_db_type` as the `source:` value** (line 2334):

Change:
```python
            block = {
                "source": src_key,
```
To:
```python
            block = {
                "source": cst_db_type,
```

That is the entire change. `cst_db_type` is already in scope at line 2334 — it was
assigned at line 1958 and is never reassigned between there and the YAML block.

---

## Verification

1. Run the app: `streamlit run webapp/app.py`
2. Go to the **✍️ Custom SQL Validation** tab.
3. Select any connection (PostgreSQL, MSSQL, or Athena).
4. Add one validation entry with any non-empty `name`, `source_sql`, `target_sql`.
5. In the **📄 YAML preview** expander, confirm the `source:` field reads
   `postgresql` (or `mssql` / `athena`) — NOT `src_0`.
6. Save the YAML and run it through the validator:
   ```bash
   python -c "
   from validation.config_schema import ConfigSchema
   import yaml, pathlib
   raw = pathlib.Path('config/bronze/data_validation/<your_file>.yaml').read_text()
   ConfigSchema.validate(yaml.safe_load(raw))
   print('OK')
   "
   ```
   Expect `OK` — no `ValidationError` about unknown source dialect.

---

## Files in scope

- `webapp/app.py` — lines 2329 and 2334 only.

## Files explicitly out of scope

- `src/validation/config_schema.py` — do not change the allowlist.
- `src/generated_queries/yaml_config_writer.py` — not involved in the Custom SQL tab.
- Any other tab or function.

---

## Escape hatch

If `cst_db_type` is ever an empty string at the point of YAML generation (e.g. the
user somehow reached Step 6 without a connection), the YAML would write `source: ""`
which also fails validation. Guard with:
```python
"source": cst_db_type or "postgresql",
```
But first check: can `cst_rec` be `None` when `valid_entries` is non-empty? The
condition at line 2328 is `if valid_entries and cst_rec:` — so `cst_rec` is always
truthy here, and `cst_db_type` was set from `cst_rec["db_type"]`. No guard needed,
but add it anyway as a defensive default if you prefer.
