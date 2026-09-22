---
name: connector-snowflake-target
description: "Use when working on the Snowflake target connector (Project/db/snowflake.py, src/sql_extractor/extractors.py's SnowflakeExtractor), or on Fivetran-metadata handling and VARIANT-type comparison on the Snowflake side. Snowflake is the single validation target for all four source types. Files: Project/db/snowflake.py, src/sql_extractor/extractors.py, src/generated_queries/sql_query_generator.py, src/generated_queries/ai_sql_generator.py, Project/utils/semantic_normalize.py."
---

# Snowflake target connector

## Query execution -- `Project/db/snowflake.py`

Class `Snowflake(Database)`. Connector library: **snowflake.connector**.
Constructor params: `SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD,
SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA, SNOWFLAKE_WAREHOUSE=""`. `connect()` only
adds the `warehouse` kwarg if truthy -- Snowflake can run with no explicit
warehouse if the account has a default.

`connect()` also always passes `login_timeout` (30s, bounds authentication),
`network_timeout` (1800s/30min, bounds all post-login network I/O including query
execution), and `session_parameters={"STATEMENT_TIMEOUT_IN_SECONDS": ...}` (1800s,
Snowflake's own server-side query ceiling -- aborts the query on Snowflake's side,
not a client-side guess). All three are class constants
(`LOGIN_TIMEOUT_SECONDS`/`NETWORK_TIMEOUT_SECONDS`/`STATEMENT_TIMEOUT_SECONDS`) --
override on an instance if a table's validation query genuinely needs longer.
These were verified as real accepted kwargs against the installed
`snowflake-connector-python==4.7.1` (`DEFAULT_CONFIGURATION` in
`snowflake.connector.connection`) before implementing -- don't assume parameter
names from older docs without re-checking against whatever version is installed.
Before this, login and query execution could both hang forever; see
`Project/db/test_snowflake.py` for the mocked checks (note: that test file must
strip its own script directory from `sys.path` before importing, since
`Project/db/snowflake.py`'s filename shadows the real top-level `snowflake`
package it needs to import -- a `python Project/db/test_snowflake.py`-shaped
gotcha, not a production issue, since `Project/main.py` never runs with
`Project/db/` as its own script directory).

`execute_query()` uses
`with self.connect() as conn: with conn.cursor() as cs:` context managers (unlike
the other three connectors, which manually close in a `finally`), and explicitly
runs `USE WAREHOUSE {warehouse};` before the real query if a warehouse is
configured -- the only connector that needs an explicit `USE` statement.

## Schema extraction -- `SnowflakeExtractor`

`_COLUMNS_SQL`/`_TABLES_SQL` template in a `{{database}}` placeholder via
`.replace()`, since Snowflake's `INFORMATION_SCHEMA` is database-scoped. PK
discovery uses `SHOW PRIMARY KEYS IN TABLE {full}` sorted by `key_sequence` --
different mechanism than the other three sources, which query
`INFORMATION_SCHEMA.TABLE_CONSTRAINTS`.

Defines `FIVETRAN_ACTIVE_COLUMN = "_FIVETRAN_ACTIVE"` and a `has_fivetran_active()`
static method that checks (case-insensitively) whether any column in a table is
literally named `_FIVETRAN_ACTIVE`. This is the auto-detection mechanism that
drives whether the `_FIVETRAN_ACTIVE = TRUE` filter gets injected into generated
SQL (see below) -- it is not hardcoded per-table, it's detected from the actual
Snowflake schema.

## Fivetran metadata columns -- the full exclusion list is 7 columns, not 5

`src/validate_cli.py`'s `STATIC_EXCLUDE_COLUMNS` is the authoritative list:
`_fivetran_synced`, `_fivetran_deleted`, `_fivetran_id`, `_fivetran_index`,
`_fivetran_start`, `_fivetran_end`, `_fivetran_active`. CLAUDE.md's shorthand list
(`_FIVETRAN_SYNCED`, `_FIVETRAN_DELETED`, `_FIVETRAN_ID`, `_FIVETRAN_INDEX`,
`_FIVETRAN_ACTIVE`) omits `_fivetran_start`/`_fivetran_end` (SCD2 history columns)
-- when excluding Fivetran columns anywhere, use the 7-column list from
`validate_cli.py`, not the CLAUDE.md summary.

## Where `_FIVETRAN_ACTIVE = TRUE` actually gets injected (multiple sites, verified)

This filter is distinct from excluding the `_FIVETRAN_ACTIVE` *column* -- it's a
row-level filter applied on the Snowflake side so only the latest active record per
key is compared:

- `src/generated_queries/sql_query_generator.py`'s `_where()` helper and its
  CTE-builder path both append `"_FIVETRAN_ACTIVE = TRUE"` mechanically when
  `has_fivetran_active` is true -- the deterministic/non-AI SQL path.
- `src/generated_queries/ai_sql_generator.py` handles it two ways: for
  AI-generated queries, it builds a prompt instruction telling the AI the query
  "MUST include" the filter (the AI is told, not mechanically appended); for the
  CTE-based JSON/HStore path (`_build_snowflake_cte_query`) it's appended
  mechanically, same as the deterministic path.

If you're debugging a missing-filter issue, check which of these three sites
generated the query in question -- the AI-prompt-instruction path is weaker
(relies on the AI following the instruction) than the two mechanical-append paths.

## VARIANT-type comparison

Snowflake VARIANT (or VARCHAR holding JSON text) is bridged via a single SQL
expression, emitted by both `JSONRule` and `HStoreRule` in `src/rules/base_rules.py`:
`COALESCE(TO_JSON(TRY_PARSE_JSON(CAST({col} AS STRING))), CAST({col} AS STRING))`.
This deliberately avoids `TYPEOF()` because it rejects VARCHAR outright (comment
notes this was verified against live Snowflake). After this SQL-side bridge, the
real canonicalization happens in Python:
`Project/utils/semantic_normalize.py`'s `canonicalize_frames()`/`canonicalize_value()`
-- applied symmetrically to both source and target DataFrames using the union of
candidate semi-structured columns from both sides, "never one side only, as that
guarantees a false mismatch."

## Verification checklist

- [ ] Use `SnowflakeExtractor.has_fivetran_active()`'s detection result, don't
  hardcode which tables have `_FIVETRAN_ACTIVE` -- it varies by table.
- [ ] Use the 7-column `STATIC_EXCLUDE_COLUMNS` list from `validate_cli.py` for
  Fivetran column exclusion, not the CLAUDE.md 5-column shorthand.
- [ ] If a Fivetran-active filter seems to be missing from a generated query, check
  whether it went through the AI-prompt-instruction path (weaker) vs. a mechanical
  append path.
- [ ] Don't reintroduce SQL-side JSON/HStore flattening -- canonicalization is
  Python-side by design (see base-rules-datatypes skill for the documented bugs
  that caused this move).
- [ ] `py_compile` any touched `.py` file before calling the change done.
