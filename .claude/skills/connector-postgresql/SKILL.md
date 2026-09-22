---
name: connector-postgresql
description: "Use when working on the PostgreSQL source connector: connection/query execution (Project/db/postgres.py) or schema extraction (src/sql_extractor/extractors.py's PostgresExtractor). Env vars: SRC_n_TYPE=postgresql. This is also the base class Redshift's connector reuses -- see connector-redshift-tradeshift for the differences. Files: Project/db/postgres.py, src/sql_extractor/extractors.py."
---

# PostgreSQL source connector

## Query execution -- `Project/db/postgres.py`

Class `Postgres(Database)`. Connector library: **psycopg2**. Constructor params:
`dbname, user, password, host, port, schema=""`. `connect()` sets Postgres's schema
search path and a statement timeout via the same `options` startup string (multiple
`-c` flags, space-separated): `-c statement_timeout=<ms>` always, plus
`-c search_path=<schema>` when a schema is given. It also passes
`connect_timeout=CONNECT_TIMEOUT_SECONDS` (libpq's own TCP/auth-handshake timeout).
Two class constants control both: `CONNECT_TIMEOUT_SECONDS` (10s) and
`STATEMENT_TIMEOUT_SECONDS` (1800s/30min, server-side query ceiling) — override on
an instance if one table's validation query genuinely needs longer (e.g. a
200M+-row full scan). Before this, connect and query could both hang forever; see
`Project/db/test_postgres.py` for the mocked timeout checks.
`execute_query()` opens a fresh connection per call, `cur.fetchall()`s the whole
result set (no chunking/pagination — psycopg2's `fetchall()` returns the true full
result set, unlike Athena's paginated REST API, so no truncation risk here), builds
a `pandas.DataFrame` from `cur.description` for column names, and always closes
cursor+connection in a `finally` block.

No explicit type coercion beyond what psycopg2/pandas do implicitly -- if a
type-mapping problem shows up, it's happening downstream in the rule/normalization
layer, not in this connector.

## Schema extraction -- `src/sql_extractor/extractors.py`'s `PostgresExtractor`

Queries `information_schema.columns/tables/table_constraints/key_column_usage/
constraint_column_usage` for columns, tables, PK and FK discovery. Handles
Postgres's `USER-DEFINED` `data_type` (the value `information_schema` reports for
enums, domains, and `hstore`) by substituting `udt_name` instead -- this is how PG
enum/domain/hstore columns surface with their real type name rather than the
generic `USER-DEFINED` placeholder.

## Env config (`.env.example`, `SRC_n_*` block)

`TYPE=postgresql`, `HOST`, `PORT` (default 5432), `DATABASE`, `SCHEMA` (default
`public`), `USERNAME`, `PASSWORD`. Type aliases accepted by the factory:
`postgresql`/`postgres`/`pg`.

## Relationship to Redshift

Redshift has no dedicated `Project/db/redshift.py` -- `Project/db/factory.py`
explicitly reuses this `Postgres` class as-is (same wire protocol, psycopg2), just
with a different default port (5439 vs 5432). `RedshiftExtractor` in
`extractors.py` subclasses `PostgresExtractor` directly and only overrides type
mapping for a handful of Redshift-only types. See **connector-redshift-tradeshift**
for exactly what differs -- don't duplicate PostgreSQL-specific fixes there; fix
them here instead, since Redshift inherits this code.

## Verification checklist

- [ ] Any fix here should be checked against Redshift too, since `RedshiftExtractor`
  subclasses `PostgresExtractor` and `factory.py` reuses `Postgres` wholesale —
  the connect/statement timeouts above apply to Redshift automatically since it's
  the same class, not a separate fix.
- [ ] Don't assume type coercion happens in this connector -- it doesn't; check
  `Project/utils/semantic_normalize.py` and `src/rules/base_rules.py` instead.
- [ ] `py_compile` any touched `.py` file before calling the change done.
