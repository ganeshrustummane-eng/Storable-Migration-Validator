---
name: connector-mssql-sitelink
description: "Use when working on the MSSQL source connector (SiteLink, Storable's self-storage product): connection/query execution (Project/db/mssqlserver.py) or schema extraction (src/sql_extractor/extractors.py's MSSQLExtractor). Env vars: SRC_n_TYPE=mssql. Files: Project/db/mssqlserver.py, src/sql_extractor/extractors.py."
---

# MSSQL (SiteLink) source connector

## Query execution -- `Project/db/mssqlserver.py`

Class `Mssqlserver(Database)`. Connector library: **pyodbc**. Constructor params:
`DRIVER, SERVER, DATABASE, UID, PWD`. `connect()` branches on whether `UID`/`PWD`
are both set: if so, SQL Server auth (`UID=...;PWD=...;TrustServerCertificate=yes;`);
otherwise falls back to Windows/Azure AD integrated auth
(`Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;`). This dual-auth
branching is the main thing distinctive about this connector -- don't assume
username/password is always required. `execute_query()` does a full `cur.fetchall()`,
no batching, builds a DataFrame from records + `cur.description`.

## Schema extraction -- `MSSQLExtractor`

Same dual-auth branching as the connector above (`self.auth` = `"windows"` vs.
`"sql"`). Has its own type-mapping table, `_normalise_mssql_type()`, translating
MSSQL native types to the PG-compatible vocabulary the rest of the pipeline (rule
book, base rules) expects:

| MSSQL type | Mapped to |
|---|---|
| nvarchar, varchar | character varying |
| datetime, datetime2, smalldatetime | timestamp without time zone |
| datetimeoffset | timestamp with time zone |
| bit | boolean |
| tinyint | smallint |
| uniqueidentifier | uuid |
| varbinary, binary | bytea |
| xml | json |
| money, smallmoney | numeric |

FK discovery uses a `REFERENTIAL_CONSTRAINTS` join -- more elaborate than
PostgreSQL's `constraint_column_usage` approach. If FK-related extraction breaks,
check this query specifically rather than assuming it mirrors the Postgres path.

## Env config (`.env.example`, `SRC_n_*` block)

`TYPE=mssql`, `HOST`, `PORT` (default 1433), `DATABASE`, `SCHEMA` (default `dbo`),
`USERNAME`, `PASSWORD`, `AUTH`, `DRIVER` (optional, defaults to `"ODBC Driver 18 for
SQL Server"`). Type aliases accepted by the factory: `mssql`/`sqlserver`/`sql_server`.

## No documented collation gotcha in code

Searched for an MSSQL collation-mismatch comment in code -- none found. The closest
real artifact is `rowversion` column exclusion (documented in
`docs/validation/supported-databases.md`, table-specific exclusion), which is
unrelated to collation. Don't assume a collation issue exists without reproducing
it first.

## Verification checklist

- [ ] Confirm which auth mode (`UID`/`PWD` vs. integrated) is actually configured
  before debugging a connection issue -- the two paths are genuinely different.
- [ ] If adding a new MSSQL type to handle, add it to `_normalise_mssql_type()`'s
  mapping table, not as a special case elsewhere in the pipeline.
- [ ] `py_compile` any touched `.py` file before calling the change done.
