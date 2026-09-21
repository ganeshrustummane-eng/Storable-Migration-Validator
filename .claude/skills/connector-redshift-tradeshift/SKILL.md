---
name: connector-redshift-tradeshift
description: "Use when working on the Redshift source connector (internally called 'TradeShift'). Redshift has NO dedicated Project/db/redshift.py -- it reuses Project/db/postgres.py's Postgres class as-is via Project/db/factory.py, and src/sql_extractor/extractors.py's RedshiftExtractor subclasses PostgresExtractor directly. Env vars: SRC_n_TYPE=redshift. Files: Project/db/factory.py, Project/db/postgres.py, src/sql_extractor/extractors.py."
---

# Redshift ("TradeShift") source connector

## The key fact: this is not a separate connector

CLAUDE.md and the code agree: Redshift speaks the PostgreSQL wire protocol, so
`Project/db/factory.py`'s redshift branch reuses the `Postgres` class from
`Project/db/postgres.py` (same psycopg2 connector) with a different default port:
**5439** instead of Postgres's 5432. There is no `Project/db/redshift.py` file --
if you're looking for Redshift-specific connection/query-execution logic, it isn't
there; it's the shared `Postgres` class, see **connector-postgresql**.

## Schema extraction -- `RedshiftExtractor(PostgresExtractor)`

Confirmed subclass of `PostgresExtractor` in `src/sql_extractor/extractors.py`.
Only overrides `_row_to_column` to apply a Redshift-only type map
(`_REDSHIFT_TYPE_MAP`) via `_normalise_redshift_type()`:

| Redshift-only type | Mapped to |
|---|---|
| `super` | json |
| `varbyte` | bytea |
| `hllsketch` | text |
| `geometry` | text |
| `geography` | text |

Code comment: "Redshift's information_schema reports standard types ... identically
to PostgreSQL -- only Redshift-only types need mapping." Everything else (PK/FK
discovery, standard type handling) is inherited unchanged from `PostgresExtractor`.

## Env config (`.env.example`, `SRC_n_*` block)

Same shape as the PostgreSQL block: `TYPE=redshift`, `HOST`, `PORT` (default 5439),
`DATABASE`, `SCHEMA` (default `public`), `USERNAME`, `PASSWORD`. Type aliases
accepted by the factory: `redshift`/`aws_redshift`.

## No known Redshift-specific SQL-function gotchas in code

Searched code comments for a documented "Redshift doesn't support a Postgres SQL
function" note -- none found. The only verified Redshift-specific quirk is the
type-mapping table above. Don't assume undocumented incompatibilities exist without
verifying against a real Redshift error first.

## Verification checklist

- [ ] A fix that looks Redshift-specific may actually belong in `Postgres`
  (`Project/db/postgres.py`) or `PostgresExtractor` -- check whether it's inherited
  behavior before writing Redshift-only code.
- [ ] Only add to `_REDSHIFT_TYPE_MAP` for types that are genuinely Redshift-only
  and not already handled by `PostgresExtractor`'s standard-type path.
- [ ] `py_compile` any touched `.py` file before calling the change done.
