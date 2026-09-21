---
name: connector-athena
description: "Use when working on the Athena source connector: connection/query execution (Project/db/athena.py, boto3-based async query polling) or schema extraction (src/sql_extractor/extractors.py's AthenaExtractor, pyathena + boto3 Glue). Athena has no primary-key concept and no HOST/PORT config -- it uses an S3 result location instead. Env vars: SRC_n_TYPE=athena. Files: Project/db/athena.py, src/sql_extractor/extractors.py."
---

# Athena source connector

## Query execution -- `Project/db/athena.py` -- async, not a normal DB cursor

Class `Athena(Database)`. Connector library: **boto3** (Athena API client, not a
DB-API driver -- there is no persistent connection or cursor in the usual sense).
Constructor params: `AWS_REGION, ATHENA_DB, ATHENA_OUTPUT, ACCESS_KEY="",
SECRET_KEY=""` (`ATHENA_OUTPUT` is the S3 staging/results location). `connect()`
builds a `boto3.Session`, using explicit keys if given, else falling back to the
default boto3 credential chain.

`execute_query()` is fundamentally different from the other three connectors --
it's poll-based:
1. `start_query_execution()` with `QueryExecutionContext={"Database": ...}` and
   `ResultConfiguration={"OutputLocation": ...}`.
2. Polling loop: `get_query_execution()` every 2 seconds until state is
   `SUCCEEDED`/`FAILED`/`CANCELLED`.
3. Raises on non-`SUCCEEDED` with the API's `StateChangeReason`.
4. Fetches via `get_query_results()`, skips the header row, and **every value comes
   back as a string** (`VarCharValue`) regardless of the column's native type --
   Athena's connector layer does not preserve numeric/boolean typing. If a
   comparison looks like a type mismatch when it shouldn't be, check whether this
   string-coercion is the actual cause before assuming a rule-book/normalization bug.

This poll-and-wait model is Athena's real architecture (query execution is
asynchronous by nature, not a bug or a workaround) -- don't try to make it
synchronous or remove the polling loop.

## Schema extraction -- `AthenaExtractor`

Uses **pyathena** for querying and **boto3's Glue client** for
`list_tables`/`list_schemas` specifically to avoid Athena workgroup permission
requirements (documented in a code comment). Requires an S3 output location
(`s3_output`/`ATHENA_S3_OUTPUT`) or raises `ExtractionError`.

Has its own type map, `_ATHENA_TYPE_MAP`:

| Athena type | Mapped to |
|---|---|
| varchar, string | character varying |
| tinyint | smallint |
| double | double precision |
| timestamp | timestamp without time zone |
| binary | bytea |
| map, struct | json |

Athena's `INFORMATION_SCHEMA.COLUMNS` only exposes a subset of standard columns --
no `character_maximum_length`, `numeric_precision`, `numeric_scale`, or
`column_default`. Don't write code that assumes those fields are populated for an
Athena source.

**Athena tables have no primary key concept at all.** `detect_primary_key()`
always returns `detected=False` for Athena, with the note "Athena (S3-backed)
tables have no primary key constraints." This is a hard architectural fact -- PK-set
comparison for an Athena source needs a different completeness strategy (e.g. a
row-hash or explicit business-key config), not a bug to work around by trying
harder to detect a PK that doesn't exist.

## Env config (`.env.example`, `SRC_n_*` block) -- different shape than the others

No `HOST`/`PORT` at all. Instead: `REGION` (default `us-west-2`), `DATABASE`,
`SCHEMA`, `USERNAME`, `PASSWORD`, `QUERY_RESULT_LOCATION` (an S3 URI). Type alias
accepted by the factory: `athena`/`aws_athena`.

**Known gap:** `AthenaExtractor.__init__` reads an `ATHENA_WORKGROUP` env var
(defaulting to `"primary"`) that is not present anywhere in `.env.example`. If a
non-default workgroup is ever needed, this env var has to be set manually --
`.env.example` doesn't document it yet.

## Verification checklist

- [ ] Don't assume Athena columns arrive typed -- they're always strings at the
  connector layer (`VarCharValue`); check where type coercion is expected to happen.
- [ ] Don't try to detect or enforce a PK for an Athena source -- design completeness
  checks around a row-hash or explicit key config instead.
- [ ] If `INFORMATION_SCHEMA` metadata (precision/scale/default) seems missing for
  an Athena table, that's expected -- Athena doesn't expose it, not a bug.
- [ ] `py_compile` any touched `.py` file before calling the change done.
