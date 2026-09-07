---
plan: 004
title: Add AWS Redshift as a 4th source database type
status: TODO
effort: M
depends_on: none
commit: cc0c1fe
branch: version1.2
---

## Why

Today the tool supports exactly 3 source types (`postgresql`, `mssql`, `athena`) against
1 target (`snowflake`). Every one of those touch points is a **flat list/dict keyed by
db_type**, deliberately — see `src/sql_extractor/extractors.py:4-6`: *"Add a new source
DB by: (1) writing a new class below, (2) adding it to `_REGISTRY` at the bottom. No
other file needs changing."* Adding Redshift means adding a 4th entry to each of those
lists, not restructuring anything.

**Scope assumption — confirm before starting:** this plan adds Redshift as a **source**
(a 4th thing to validate FROM), joining postgresql/mssql/athena, not as a second
**target** (replacing/joining Snowflake). That match's the existing "3 sources → 1
target" shape and every extension point below is source-side. If the actual intent is
"validate migrations INTO Redshift instead of Snowflake," that is a materially bigger
change (every `elif db_type == "snowflake"` special-cased *target* branch in
`Project/db/factory.py`, `src/validation/config_schema.py`, and the YAML writers would
need a Redshift counterpart) — STOP and ask the user to confirm which one they mean
before writing code, don't guess.

## Key fact that shrinks this plan

Redshift speaks the PostgreSQL wire protocol — `psycopg2` connects to it exactly like
PostgreSQL, just on port 5439 by default instead of 5432, and its
`information_schema.columns` / `table_constraints` / `key_column_usage` catalogs work
the same way (Redshift doesn't *enforce* PK constraints at write time, but constraint
metadata still round-trips through `information_schema` the same as Postgres — PK
detection needs no special-casing). That means:

- **No new `Project/db/*.py` connector class is needed.** Reuse `Project/db/postgres.py`'s
  `Postgres` class as-is for `db_type == "redshift"`, just with a different default port.
  Do not write a `Project/db/redshift.py` that duplicates `Postgres` — that would be
  exactly the kind of copy-pasted-per-dialect file flagged in the over-engineering
  review. Rung 2 of the ladder: it already exists, reuse it.
- **`src/sql_extractor/extractors.py` does need a small `RedshiftExtractor`**, but as a
  thin subclass of `PostgresExtractor` overriding only type normalization (Redshift has
  a few types Postgres doesn't: `super`, `varbyte`, `geometry`, `hllsketch`) — same shape
  as `_normalise_mssql_type`/`_normalise_athena_type` (`src/sql_extractor/extractors.py:283,572`).
- **`src/rules/redshift_rules.py` is a re-export shim, same as the other three** —
  keep that pattern. This is *not* the yagni finding from the over-engineering review;
  that finding was reconsidered specifically because this Redshift addition was already
  planned — the per-dialect module names are the extension point new sources hook into,
  not dead duplication. Do not delete `src/rules/{athena,mssql,snowflake}_rules.py` as
  part of this plan or any other.

## Touch points (in dependency order)

### 1. `config/database_registry.yaml` — register a connection

Add a `SRC_4` block following the existing pattern (`config/database_registry.yaml:45-52`):

```yaml
  # ── SRC_4 — AWS Redshift ────────────────────────────────────────────────
  SRC_4:
    label: "Redshift — <cluster/db name>"
    db_type: redshift
    database: <fill in>
    schema: public
```

Also add matching `SRC_4_TYPE=redshift`, `SRC_4_HOST`, `SRC_4_PORT` (5439),
`SRC_4_DATABASE`, `SRC_4_SCHEMA`, `SRC_4_USERNAME`, `SRC_4_PASSWORD` to `.env` /
`.env.example` — follow the exact key shape already used for `SRC_1`/`SRC_2`/`SRC_3`.

**Verification:** `python -c "import yaml; yaml.safe_load(open('config/database_registry.yaml'))"`
parses without error.

### 2. `Project/db/factory.py` — new branch, reusing `Postgres`

File: `Project/db/factory.py`. Add `"redshift"` to `_TYPE_ALIASES` (line 25-29):

```python
_TYPE_ALIASES = {
    "postgresql": {"postgresql", "postgres"},
    "mssql": {"mssql", "mssqlserver"},
    "athena": {"athena", "aws_athena"},
    "redshift": {"redshift", "aws_redshift"},
}
```

Add a branch in `get_database()` (after the `postgresql` branch, ~line 79), reusing
`Postgres` — **do not import a new class**:

```python
    elif db_type == "redshift":
        src = _find_source(env, "redshift")
        return Postgres(
            dbname=override_database or src["DATABASE"],
            host=src["HOST"],
            user=src["USERNAME"],
            password=src.get("PASSWORD", ""),
            port=int(src.get("PORT") or 5439),
            schema=override_schema or src.get("SCHEMA", ""),
        )
```

**Verification:** `python -c "import ast; ast.parse(open('Project/db/factory.py').read())"`.
Manual: with a real Redshift connection in `.env`'s `SRC_4_*` keys, run
`python Project/main.py --layer_type bronze --tables <a_redshift_table> --count_validation yes --data_validation no --environment local`
and confirm it connects and returns a row count (no YAML/config beyond a hand-written
test entry needed yet — that comes from step 5/6's generator).

### 3. `src/sql_extractor/extractors.py` — `RedshiftExtractor`

Add near the other normalizers (after `_normalise_athena_type`, ~line 572), plus a class
just before the `_REGISTRY` dict (~line 725):

```python
def _normalise_redshift_type(raw: str) -> str:
    """Redshift-specific types that don't already match a PostgreSQL-compatible
    name. Everything else passes through unchanged — Redshift's information_schema
    reports standard types (varchar, int4, int8, numeric, timestamp, boolean, ...)
    identically to PostgreSQL."""
    raw_lower = raw.lower().strip()
    return {
        "super": "json",        # Redshift's semi-structured type ~= json for rule purposes
        "varbyte": "bytea",
        "hllsketch": "text",
        "geometry": "text",
        "geography": "text",
    }.get(raw_lower, raw)


class RedshiftExtractor(PostgresExtractor):
    """Redshift speaks the PostgreSQL wire protocol and shares its
    information_schema shape — only type normalization differs."""

    @staticmethod
    def _row_to_column(row: dict) -> ColumnMetadata:
        col = PostgresExtractor._row_to_column(row)
        col.data_type = _normalise_redshift_type(col.data_type)
        return col
```

Register it in `_REGISTRY` (~line 725-733):

```python
    "redshift":     ("sql_extractor.extractors", "RedshiftExtractor"),
    "aws_redshift": ("sql_extractor.extractors", "RedshiftExtractor"),
```

And in the default-port table (~line 737-739):

```python
    "redshift": 5439, "aws_redshift": 5439,
```

**Escape hatch:** if `PostgresExtractor._row_to_column` turns out to not be a
`@staticmethod` by the time this is implemented (re-check — code drifts), adjust the
override accordingly; don't assume the signature shown above without reading the
current file first.

**Verification:** `python -c "from sql_extractor.extractors import ExtractorFactory; ExtractorFactory.get('redshift')"`
(check the actual factory accessor name in the file — `ExtractorFactory` class is at
`src/sql_extractor/extractors.py:743`, read its methods before assuming `.get()` is
right) resolves to `RedshiftExtractor` without error.

### 4. `src/rules/redshift_rules.py` — re-export shim

Copy the shape of `src/rules/athena_rules.py` (21 lines) verbatim, changing only the
docstring to describe Redshift instead of Athena. Do not add Redshift-specific rule
overrides speculatively — if a real type-comparison mismatch shows up in testing (e.g.
`SUPER` columns comparing differently than `JSON`), add a targeted rule then, not now.

**Verification:** `python -c "import rules.redshift_rules"` (adjust for actual import
root — check whether `src/rules/__init__.py` is imported as `rules` or `src.rules`
elsewhere before assuming) imports cleanly.

### 5. `config/redshift_exclusions.yaml` — new exclusions file

File: `src/validate_cli.py:182-184` maps db_type → exclusions file path. Add:

```python
    "redshift":   _EXCLUSIONS_DIR / "redshift_exclusions.yaml",
```

Create `config/redshift_exclusions.yaml` following `config/athena_exclusions.yaml`'s
shape (version, description, `global_exclusions` with an empty or minimal `columns:
[]` list — don't invent exclusion entries that don't correspond to a real Redshift
metadata column; Athena's file excludes Fivetran-internal columns because that source
actually has them, Redshift may have none to start).

**Verification:** `python -c "import yaml; yaml.safe_load(open('config/redshift_exclusions.yaml'))"`.

### 6. `webapp/app.py` — surface it in the UI

- `SOURCE_TYPES = ("postgresql", "mssql", "athena")` at `webapp/app.py:418` → add
  `"redshift"`.
- Find `_DB_TYPE_LABELS` (referenced at `webapp/app.py:3405,3412` — read its definition,
  likely near `SOURCE_TYPES`) and add a `"redshift": "Redshift"`-shaped entry matching
  the existing label style.
- Check every other place `SOURCE_TYPES` or a hardcoded `("postgresql", "mssql",
  "athena")`-shaped tuple appears (`grep -n 'postgresql.*mssql.*athena' webapp/app.py`)
  before assuming the two call sites at line 3376/3405/3412 are the only ones — the
  audit for this plan only checked `SOURCE_TYPES` by name, a literal tuple written out
  again elsewhere would be missed by that grep.

**Verification:** run the Streamlit app, open the tab(s) that render a source-type
picker, confirm "Redshift" appears as a choice and selecting it doesn't throw before
credentials are even attempted.

### 7. `src/setup_wizard.py` — onboarding

`DB_TYPES` dict at `src/setup_wizard.py:151-156` (already re-read after this session's
click-based `_prompt`/`_yn` fix — line numbers may have shifted by a line or two from
the diff in that fix). Add:

```python
    "redshift":   ("AWS Redshift",             5439),
```

Add `"5"` (or next free number) to `_DB_ALIASES` (~line 158-160) pointing at
`"redshift"`.

**Verification:** `cd src && python setup_wizard.py`, walk through adding a connection,
confirm "AWS Redshift" appears as a numbered choice and pre-fills port 5439.

## What NOT to do

- No new `Project/db/redshift.py` (see "Key fact" above).
- No changes to `src/validation/{count_validator,data_validator}.py` or
  `Project/main.py`'s comparison logic — those are already source-agnostic (they take a
  `db_factory` and a `db_type` string, no per-dialect branching lives there).
- No Redshift-specific rules beyond the re-export shim unless a real test run surfaces
  a genuine type-comparison mismatch.
- No target-side changes (see "Scope assumption" above) unless the user confirms this
  is actually about a second target, not a fourth source.

## Test

No existing test file exercises `Project/db/factory.py` or `src/sql_extractor/extractors.py`
against a live database (confirm by checking for a `tests/` directory before assuming).
This plan's verification is the manual steps embedded above — each touch point has one.
If a live Redshift cluster is available for testing, the end-to-end check is: run a
real `count_validation` against one small Redshift table through `Project/main.py`
exactly as shown in step 2's verification, then a `data_validation` against the same
table, and confirm both produce a `PASS`/`FAIL` summary row with `source_type=redshift`
in `data_validation_summary.csv` — same shape as an existing postgresql/mssql/athena
run, just a new value in that column.

## Maintenance note

Every future 5th/6th source (and the Redshift work here) follows the same shape:
one connector reuse-or-class in `Project/db/`, one extractor (often a thin subclass) in
`src/sql_extractor/extractors.py`, one rules re-export in `src/rules/`, one exclusions
YAML, one `database_registry.yaml` entry, and one UI tuple/dict update in
`webapp/app.py` + `src/setup_wizard.py`. If a future source needs a genuinely different
comparison rule set (not just a different metadata normalizer), that's the first
legitimate reason to break the "rules are just a Postgres re-export" pattern — until
then, keep it a re-export.
