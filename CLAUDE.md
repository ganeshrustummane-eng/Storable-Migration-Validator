# Migration Validator — Project Context

Read this before working anywhere in the repo. It's the one place that describes
the real business problem, the real architecture, and the data-quality bar this
tool has to hit — so agents and skills don't each re-derive (or contradict) it.

## The problem

Storable is migrating data from several operational systems into Snowflake.
Migration itself (extraction + load) is done by another team using Fivetran and
similar CDC tools. **This repo's job is to validate that migration** — confirm it
completed correctly, not to perform it.

## Source systems → Snowflake

| Source | System | What lives there |
|---|---|---|
| PostgreSQL | `SRC_*_TYPE=postgresql` | Transaction data |
| Athena | `SRC_*_TYPE=athena` | Reporting layer |
| MSSQL | `SRC_*_TYPE=mssql` | SiteLink (Storable's self-storage product) |
| Redshift | `SRC_*_TYPE=redshift` | Referred to internally as "TradeShift" — same connector/wire protocol as PostgreSQL (psycopg2), different default port (5439) |

All four land in **Snowflake** (the single validation target) via **Fivetran**
(and similar tools) doing the actual migration. Every Snowflake table produced
this way carries Fivetran metadata columns (`_FIVETRAN_SYNCED`, `_FIVETRAN_DELETED`,
`_FIVETRAN_ID`, `_FIVETRAN_INDEX`, `_FIVETRAN_ACTIVE`, ...) — these are always
excluded from column-level comparison, and `_FIVETRAN_ACTIVE = TRUE` must always
be applied as a filter on the Snowflake side so only the latest active record per
key is compared (soft-deleted/superseded rows are not migration failures).

Connection config is environment-driven: `SRC_1_*`, `SRC_2_*`, ... in `.env` /
`.env.dev` / `.env.uat` / `.env.prod` (see `.env.example`), one block per source,
`TYPE` picks the connector. `SNOWFLAKE_*` is the single target.

## What "validated correctly" means — data quality dimensions

Every validation run should be assessed against these dimensions, not just
"did the row counts match":

- **Completeness** — every source row that should have migrated is present in
  Snowflake (no silent drops). Row-count validation + PK set-difference
  (`SOURCE_ONLY` / `TARGET_ONLY`) in `Project/main.py`.
- **Uniqueness** — no duplicate rows/keys introduced by the migration (Fivetran
  CDC replay, merge bugs). PK duplicate-detection in the SQL generators.
- **Accuracy** — migrated values match source values *after* correct
  transformation/type normalization (see semantic-normalization skill below) —
  not comparing a Postgres `hstore` string to a Snowflake `VARIANT` string
  byte-for-byte, but comparing their canonicalized meaning.
- **Timeliness** — Fivetran sync lag / `_FIVETRAN_SYNCED` freshness is
  reasonable for the validation window; validation shouldn't flag legitimately
  in-flight syncs as failures.
- **Validity** — migrated values conform to the expected type/format per the
  rule book (`src/rule_book.py`, `src/rules/rules_catalog.json`) for that source→target
  type pair.
- **Consistency** — the same logical filter/exclusion is applied identically on
  both sides (source query and Snowflake query) — an asymmetric filter produces
  false mismatches that look like data-quality bugs but are actually validator
  bugs.

Row-level pass/fail (not just table-level) matters: `Project/main.py` writes
every row's status (`PASS` / `FAIL` / `SOURCE_ONLY` / `TARGET_ONLY`), plus a
failed-only CSV. Don't build a path that only reports failures — passed rows
must be visible too, since "no failures reported" and "validation didn't run"
must never look the same.

Transformations and filtrations applied during migration (currency conversion,
renamed columns, exclusion lists, Fivetran-active filtering) are **in scope**
for validation, not something to work around — the validator's job is to know
what transformation/filter was applied and check *that*, not to compare raw
values and ignore the difference.

## Where things actually live (post-cleanup, current as of this session)

- **UI**: `webapp/app.py` — single-file Streamlit app. Thin wrapper only; no
  validation logic lives here.
- **Mapping pipeline (used by the UI)**: `src/validation_pipeline.py`'s
  `run_with_plan()` — exact/fuzzy column matching, AI only for ambiguous
  columns (`src/ai/rule_planner.py`). The older 100%-AI `run()` method and
  `ai_transformation/orchestrator.py` were removed — don't resurrect that
  pattern.
- **AI backend**: EPAM DIAL today (`DIAL_API_KEY`) — a proxy, used because a
  direct Claude key isn't issued yet. `CLAUDE_API_KEY` is supported as a
  fallback everywhere the AI backend is selected (`rule_planner.py`,
  `ai_transformation/ai_rule_mapper.py`, `connector/agent.py`) and becomes the
  primary path automatically once set, with no code change needed. Gemini
  support was removed entirely — don't add it back.
- **Actual validation execution (row-level compare)**: `Project/main.py` +
  `Project/runner.py` + `Project/db/*.py` — this is what the webapp's "Run
  Validation" button calls. `src/validation/*.py` (`data_validator.py`,
  `count_validator.py`, `validation_executor.py`) is a second, shallower,
  table-level-only engine still used by the chat-agent's `execute_validation`
  tool (`src/connector/tools.py`) — both are currently live for different
  entry points; this duplication is a known follow-up, not yet resolved.
- **Semantic normalization** (hstore→VARIANT, jsonb→VARIANT, etc.):
  `Project/utils/semantic_normalize.py`. Well-tested, don't casually rewrite.
- **Exclusions**: `config/exclusions.yaml` (global) + `config/*_exclusions.yaml`
  (per-source-DB) + table-specific exclusions set at runtime in the UI. The 5
  YAML files are ~90% duplicate content — a known cleanup candidate.
- **Agentic/chat layer**: `src/connector/` (renamed from `gemini_connector` —
  Gemini is gone, EPAM DIAL/Claude only). JIRA integration, approval store,
  audit log all live here and are real, in-use features.
- **Removed/quarantined code**: `trash/` — things moved out of the active tree
  but not deleted, in case something's needed later. Check there before
  assuming something no longer exists.

## Ground rules for any agent or skill working in this repo

1. **No redundant implementations.** If similar logic exists in two places
   (SQL generation, exclusion checks, Fivetran detection), point to the
   existing one — don't add a third.
2. **No speculative abstraction.** This is a production validation tool for
   one company's migration, not a generic framework — don't add config knobs,
   plugin systems, or new AI-backend integrations "just in case."
3. **Verify before asserting.** Before claiming a file/function is unused,
   grep for its actual callers across `src/`, `webapp/`, and `Project/` — this
   codebase has already had one incorrect "this is dead" claim corrected this
   session (`sql_query_generator.py` looked redundant but wraps a needed step).
4. **`py_compile` (or `ast.parse`) every touched `.py` file before calling a
   change done.**
