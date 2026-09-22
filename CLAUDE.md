# Migration Validator — Project Context

Read this before working anywhere in the repo. It's the one place that describes
the real business problem, the real architecture, and the data-quality bar this
tool has to hit — so agents and skills don't each re-derive (or contradict) it.

## The problem

Storable is migrating data from several operational systems into Snowflake.
Migration itself (extraction + load) is done by another team using Fivetran and
similar coalesce tools. **This repo's job is to validate that migration** — confirm it
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

Report/output formats are **CSV and YAML only**, verified — there is no XML
report generation anywhere in this codebase, and no `.xlsx` *writer* either
(`.xlsx` is read-only, used for uploaded mapping sheets via
`src/excel_batch_loader.py`; the webapp's Output Files tab lists `.xlsx` as a
display filter, but nothing produces one today). Don't assume either format
exists without checking first — see the `data-comparison-report` skill.

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
- **Base rule catalog (static/immutable)**: `src/rules/rules_catalog.json` (10
  rule entries, metadata only) + `src/rules/base_rules.py` (the Python classes
  that actually execute — the JSON file's embedded SQL templates for
  `json`/`hstore` are stale relative to these classes; don't trust them as the
  executing logic).
- **Learned rules (mutable, human-gated feedback loop)**: `src/rule_book.py`
  (gap-filler rules — `draft`/`active` status, `reuses_rule` anti-hallucination
  guard: an active learned rule only ever replays an existing base rule's
  template, never its own SQL) + `src/learning/feedback.py` (records human
  corrections to AI/fuzzy column-mapping decisions) + `src/learning/retrieval.py`
  (reads corrections back for future confidence scoring). Both writers persist
  into the same `rule_book_learned.json` under different top-level keys
  (`learned_rules` vs `learned_corrections`) — each does a read-merge-write to
  avoid clobbering the other's key. `docs/rules/rule-book.md`'s described
  lifecycle (approval roles, version store) does not match this code — treat
  that doc as stale/aspirational.
- **Webapp's own YAML-writing paths**: `webapp/app.py` contains three direct
  `yaml.dump()` call sites (the "prompt" single-table tab, the reference/
  filter/join "RPJ" tab, and the custom-YAML manual editor), and
  `src/excel_batch_loader.py` has its own `write_yaml()` for the Excel-upload
  batch flow. None of these four call into
  `src/generated_queries/yaml_config_writer.py` (the backend generator used by
  `src/validation_pipeline.py`) — they're independent, real, intentional-for-now
  duplication, same treatment as the 5 exclusion YAMLs above. Don't consolidate
  without the user asking. See the `webapp-yaml-generation` skill.
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
5. **Disambiguate before grepping.** This repo has several intentional
   look-alike pairs: the two validation engines (`Project/main.py` row-level
   vs `src/validation/*.py` table-level, see above), the two YAML-writing
   paths (`src/generated_queries/yaml_config_writer.py` vs webapp/
   `excel_batch_loader.py`'s direct `yaml.dump()` calls), and the 5
   near-duplicate exclusion YAMLs. If a request says "validation" or "YAML"
   without naming which one, ask the user to name the entry point (e.g. "Run
   Validation button" vs "chat bubble") instead of reading both sides to
   guess — cheaper for everyone.

## Skills index (`.claude/skills/`)

`.claude/` is the authoritative tree for agents/skills in this repo — `.github/`
has mirrored copies in Copilot's format that can drift out of sync (confirmed
divergence found once already); don't treat `.github/` as ground truth.

- `normalization-and-exclusions` — semantic type normalization + 3-tier exclusions.
- `reference-filter-joins` — NL condition → `CanonicalValidationPlan` → backend SQL/YAML.
- `base-rules-datatypes` — the static 10-rule catalog (`rules_catalog.json` + `base_rules.py`).
- `learned-rules` — the mutable gap-filler/correction feedback loop.
- `webapp-yaml-generation` — the webapp's own independent YAML-writing paths.
- `excel-batch-ai-review-planned` — **not yet built**; design notes only for a
  future AI-preview step on Excel-upload batch generation.
- `data-comparison-report` — the two comparison engines and CSV report format.
- `connector-postgresql`, `connector-mssql-sitelink`, `connector-athena`,
  `connector-redshift-tradeshift`, `connector-snowflake-target` — per-source/target
  connection, schema-extraction, and type-mapping specifics.
