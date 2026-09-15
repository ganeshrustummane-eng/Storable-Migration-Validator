# Migration Validator

> AI-assisted database migration testing platform. Validates heterogeneous source migrations (PostgreSQL, MSSQL, and AWS Athena) into Snowflake with automated SQL generation, row-level comparison, human-governed approvals, and full audit trail.

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green)](#)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688)](#)
[![Streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B)](#)
[![Status: Production](https://img.shields.io/badge/Status-Production-brightgreen)](#)

---

## Table of Contents

1. [What It Does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Supported Systems](#3-supported-systems)
4. [Quickstart](#4-quickstart)
5. [Web UI](#5-web-ui)
6. [Validation Workflow](#6-validation-workflow)
7. [Automation — Full Pipeline](#7-automation--full-pipeline)
8. [AI SQL Generation](#8-ai-sql-generation)
9. [Rule Book](#9-rule-book)
10. [Human Review & Approvals](#10-human-review--approvals)
11. [Scheduled Runs & Notifications](#11-scheduled-runs--notifications)
12. [Authentication & Security](#12-authentication--security)
13. [Jira Integration](#13-jira-integration)
14. [Environment Variables](#14-environment-variables)
15. [Repository Structure](#15-repository-structure)
16. [Documentation Index](#16-documentation-index)

---

## 1. What It Does

Migration Validator automates the most labour-intensive parts of a database migration:

| Manual today | Automated by this tool |
|---|---|
| Write column-mapping SQL per table | AI generates mapping + SQL from schema discovery |
| Run source/target queries, compare in spreadsheets | Row-level deterministic comparison engine |
| Chase architects for ambiguous mappings | Confidence-scored queue → human review UI |
| Re-run validations after every ETL change | Scheduled background runs with Slack/email alerts |
| Track which tables passed / failed | History dashboard + SQLite results store |
| File Jira tickets for failures | Attach validation result to assigned Jira ticket |

**What it does NOT do:** It does not move data. It reads source and target, compares them, and tells you what's wrong.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    STREAMLIT WEB UI                              │
│                   webapp/app.py  :8501                          │
│  Generate YAML · Run Validation · Review · Jira · Usage & Cost  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
┌─────────────────────┐    ┌────────────────────────────┐
│  VALIDATION ENGINE  │    │   GEMINI CONNECTOR         │
│  Project/main.py    │    │   src/gemini_connector/    │
│                     │    │   FastAPI  :8001           │
│  get_database()     │    │   24 tools · RBAC · Audit  │
│  canonicalize_frames│    └────────────────────────────┘
│  row-level compare  │
│  mismatch threshold │
│  notify_failure()   │
└──────┬──────────────┘
       │
  ┌────┴─────────────────────────────────┐
  ▼                                      ▼
SOURCE SYSTEMS                      TARGET SYSTEM
PostgreSQL / MSSQL / Athena         Snowflake
```

---

## 3. Supported Systems

| System | Role | Notes |
|---|---|---|
| PostgreSQL | Source | All schemas; `search_path` aware |
| Microsoft SQL Server | Source | Windows + SQL auth |
| AWS Athena | Source | S3 staging dir required |
| AWS Redshift | Source | Speaks Postgres wire protocol (psycopg2), default port 5439 |
| Snowflake | Target | Bronze / Silver / Gold / Reporting layers |
| Google Gemini / Vertex AI | AI backend | SQL generation, column mapping |
| EPAM DIAL | AI proxy | GPT-4o, Claude, Gemini, Llama |
| Anthropic Claude | AI backend | Direct fallback |

---

## 4. Quickstart

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Fill in SRC_1_*, SNOWFLAKE_*, GEMINI_API_KEY or DIAL_API_KEY

# 3. Start the web UI
streamlit run webapp/app.py

# 4. (Optional) Start the Gemini connector API
python start_connector.py
```

Open `http://localhost:8501` in your browser.

---

## 5. Web UI

The Streamlit app (`webapp/app.py`) has 11 tabs:

| Tab | Purpose |
|---|---|
| ▶️ Generate Single YAML | Pick one source table → AI maps columns → generate validation YAML |
| 📋 Generate Batch YAML | Multi-table, multi-schema, JOIN, and Excel report YAML generation with progress |
| ✍️ Custom SQL Validation | Write or AI-generate your own source + Snowflake SQL, build YAML |
| 🚀 Run Validation | Execute count + data validation, set mismatch threshold |
| 📈 History & Trends | Pass/fail history, trend charts from SQLite results store |
| 📖 Rule Book | Manage normalization and exclusion rules |
| 🚫 Exclusions | Column-level exclusion config per source type |
| ✅ Review & Approve | Human approval queue for AI-proposed column mappings |
| 🎫 My Jira Tickets | View assigned tickets, transition status, attach validation results |
| 💰 Usage & Cost | AI token usage and estimated cost by model/call type |
| 📘 Guide | Quickstart and feature walkthrough |

Sidebar provides: connection test, AI usage summary, scheduled run controls.

---

## 6. Validation Workflow

### Count Validation

Runs `SELECT COUNT(*)` on source and target. Pass = counts match exactly.

### Data Validation

1. Execute `sourcequery` and `targetquery` from YAML config
2. Canonicalize both DataFrames (JSON key order, whitespace, type coercion)
3. Join on primary key column(s)
4. Emit `PASS` / `FAIL` / `SOURCE_ONLY` / `TARGET_ONLY` per row
5. Write row-level CSV to `output/<layer>/validation_<run_id>/`
6. Apply mismatch threshold: `actual_mismatch_pct <= threshold_pct` → PASS

The batch workflow also supports multi-schema validation prompts, including
required `LEFT JOIN` rules, and can generate one YAML configuration per prompt.

### YAML Config Format

```yaml
tables:
  customer:
    validations:
      data_validation:
        source_table_name: customer
        source: postgresql
        source_database: mydb
        source_schema: public
        sourcequery: |
          SELECT id, name, email FROM public.customer ORDER BY id
        target_table_name: customer
        target: snowflake
        target_database: DEV_DB
        target_schema: PUBLIC
        targetquery: |
          SELECT id, name, email FROM DEV_DB.PUBLIC.CUSTOMER ORDER BY id
        pksourcecolumn: id
        pktargetcolumn: id
        mismatch_threshold_pct: 0.5   # optional: tolerate up to 0.5% mismatch
```

### Running from CLI

```bash
cd Project
python main.py \
  --layer_type bronze \
  --tables customer orders \
  --count_validation yes \
  --data_validation yes \
  --environment local
```

---

## 7. Automation — Full Pipeline

The goal: zero manual steps between "ETL run finished" and "validation results in Jira".

**Current state of each automation piece:**

| Step | Status | How |
|---|---|---|
| Schema discovery | ✅ Done | `ExtractorFactory` → `cached_source_columns()` |
| AI column mapping | ✅ Done | `AISQLQueryGenerator.generate_schema_aware_query()` |
| YAML generation | ✅ Done | UI builder or `yaml_config_writer.py` |
| Validation execution | ✅ Done | `runner.py` → `main.py` subprocess |
| Results stored | ✅ Done | `results_store.py` SQLite |
| Scheduled runs | ✅ Done | APScheduler in sidebar |
| Failure notifications | ✅ Done | `notifier.py` → Slack webhook + SMTP |
| Jira status update | ✅ Done | `jira_client.py` transition + comment |
| Source data profiling | ✅ Done | Direct driver connections, single-query |
| Schema drift detection | ✅ Done | Compares live columns vs saved YAML |

**What to build next for full automation:**

1. **Trigger on ETL completion** — webhook receiver or S3 event listener that fires `run_validation()` when a Fivetran/dbt run finishes. Currently: manual button or schedule.
2. **CI/CD integration** — `python Project/main.py ...` returns exit code 1 on failure. Wire it into your pipeline (`github actions`, `Airflow`, `dbt tests`).
3. **Auto-generate YAML for new tables** — when schema discovery finds a table with no YAML, auto-generate and queue for review instead of requiring manual UI step.
4. **Multi-environment promotion** — run validation against dev → flag regressions before promoting to uat → prod.

---

## 8. AI SQL Generation

The Custom SQL tab generates dialect-specific SQL using schema context:

```python
AISQLQueryGenerator.generate_schema_aware_query(
    prompt="validate customer migration",
    schema_context={col: dtype, ...},
    db_type="postgresql",      # or "mssql", "athena", "snowflake"
    default_schema="public",
)
```

Generated SQL includes a `-- PK: col1, col2` comment so the UI auto-fills the primary key fields.

Supported dialects: `postgresql`, `mssql`, `athena`, `snowflake`.

---

## 9. Rule Book

Rules are the single governance layer that decides which columns are compared, how each type is normalized for cross-dialect comparison, and which AI-observed patterns get reused on future runs. Every rule ends up feeding the same `CanonicalValidationPlan` — there is no separate code path that hardcodes a one-off filter or transform.

| Rule type | Storage | Applies to |
|---|---|---|
| Fivetran exclusions | `config/exclusions.yaml` | All tables |
| DB-type exclusions | `config/postgresql_exclusions.yaml`, `mssql_exclusions.yaml`, `athena_exclusions.yaml`, `redshift_exclusions.yaml` | Per source |
| Pattern rules | UI Rule Book tab → `rule_book_learned.json` | Regex match |
| Transformation rules | `src/rules/` (base, immutable) + validation plan | Type coercion |
| Normalization rules | `Project/utils/semantic_normalize.py` | JSON/JSONB/HStore |

**Rule lifecycle:** AI observes a pattern during column mapping → proposed as a **draft** in `rule_book_learned.json` → a human with the `RULE_ADMIN` role approves or rejects → approved rules are **activated** (`RULE_ACTIVATE` permission) → every change is versioned in `VersionStore` → active rules are applied to all future pipeline runs.

**How rules flow into a validation run** (`src/rule_book.py`, `src/core/validation_plan.py`):

```
Schema extraction (source + target columns)
        ↓
ExclusionManager removes Fivetran + user-defined + pattern-excluded columns
        ↓
RuleBook.get_rule_for_type() resolves a type-normalization rule per remaining column
        ↓
Remaining ambiguous columns go to fuzzy match → AI (DIAL) for a proposed mapping
        ↓
CanonicalValidationPlan (immutable) — the ONE place SQL + YAML generators read from
        ↓
ai_sql_generator.py emits source/target SQL   +   yaml_config_writer.py persists the plan as YAML
```

**Guardrails baked into the Rule Book (not just convention):**

- Base rules in `src/rules/` and `rules_catalog.json` are immutable — never edited at runtime, only *learned* rules are mutable.
- Every learned rule's SQL template is validated before it can be saved (`_validate_sql_template` in `src/rule_book.py`): no `;`, no `--`/`/* */` comment markers, and a deny-list of DDL/DML keywords (`DROP`, `DELETE`, `ALTER`, `TRUNCATE`, `INSERT`, `UPDATE`, `GRANT`, `REVOKE`, `EXEC`, `EXECUTE`, `CREATE`, `MERGE`, `CALL`). This blocks SQL injection through the "AI-assisted paste" rule form even though rule text isn't wired into live SQL generation yet.
- `source_filter` / `target_filter` and any join/transform expressed in the plan must encode the *same* logical condition in both dialects — the SQL generator never lets one side diverge from the other.
- Rule text is only ever concatenated into AI **prompts** (`build_prompt_block()`); it never becomes a raw SQL fragment executed against a live connection without going through the plan → generator path above.

---

## 10. Human Review & Approvals

Column mappings below the confidence threshold go to the Review & Approve tab.

| Confidence | Action |
|---|---|
| ≥ 95% | Auto-accepted |
| 75–94% | Queued for human review |
| < 75% | Mandatory human review |

Approval actions: `approve_mapping`, `modify_mapping`, `reject_mapping`.  
Every action writes an `AuditRecord` with actor, timestamp, reason, and version.

---

## 11. Scheduled Runs & Notifications

**Scheduler** (sidebar → Scheduled Runs):
- Intervals: every 1h / 6h / 12h / daily
- Runs `run_validation(layer, env, ["all"], count=True, data=True)` in background thread
- Shows active config (layer, env, interval) while running

**Failure notifications** — configure in `.env`:

```bash
# Slack
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

# Email
NOTIFY_EMAIL_TO=team@company.com
SMTP_HOST=smtp.company.com
SMTP_PORT=587
SMTP_USERNAME=alerts@company.com
SMTP_PASSWORD=...
```

`notifier.notify_failure(subject, body)` is called automatically by `Project/main.py` on any validation failure.

---

## 12. Authentication & Security

The Gemini Connector API (`start_connector.py`) enforces:

| Mode | Set via | Use case |
|---|---|---|
| `static` | `CONNECTOR_API_TOKEN` | CI/CD, internal tooling |
| `jwt` | HS256 JWT with configurable issuer | Enterprise SSO |
| `dev` | `AUTH_MODE=dev` | Local development |

RBAC roles: `VIEWER`, `REVIEWER`, `RULE_ADMIN`, `VALIDATION_OPERATOR`, `ADMIN`.

Security guarantees:
- Credentials never sent to AI — stay server-side
- AI self-approval blocked (`gemini_ai` / `ai` actor strings rejected)
- Append-only audit log — no secrets logged
- Optimistic concurrency control — HTTP 409 on version conflict

---

## 13. Jira Integration

Set in `.env`:

```bash
JIRA_URL=https://your-company.atlassian.net
JIRA_EMAIL=your.email@company.com
JIRA_API_TOKEN=your-api-token
JIRA_PROJECT_KEY=MIG
```

The **My Jira Tickets** tab:
- Fetches tickets assigned to you via JQL (`/rest/api/3/search/jql`)
- Shows status badge, summary, priority
- Transition buttons: To Do → In Progress → Done
- Attach validation run result as a Jira comment (YAML file picker)

---

## 14. Environment Variables

See `.env.example` for the full list. Key variables:

```bash
# AI backend (choose one)
GEMINI_API_KEY=                    # Gemini Developer API
GEMINI_MODEL=gemini-2.5-flash
DIAL_API_KEY=                      # EPAM DIAL proxy
DIAL_API_BASE=

# Source connections (repeat for SRC_2, SRC_3, SRC_4, ...)
SRC_1_TYPE=postgresql              # postgresql | mssql | athena | redshift
SRC_1_HOST=
SRC_1_PORT=5432
SRC_1_DATABASE=
SRC_1_SCHEMA=public
SRC_1_USERNAME=
SRC_1_PASSWORD=

# Snowflake target
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_USERNAME=
SNOWFLAKE_PASSWORD=
SNOWFLAKE_DATABASE=
SNOWFLAKE_SCHEMA=

# Notifications
SLACK_WEBHOOK_URL=
NOTIFY_EMAIL_TO=
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=

# Jira
JIRA_URL=
JIRA_EMAIL=
JIRA_API_TOKEN=
JIRA_PROJECT_KEY=MIG
```

---

## 15. Repository Structure

```
Migration-validator/
├── webapp/app.py                   # Streamlit web UI (11 tabs + sidebar controls)
├── start_connector.py              # Gemini connector FastAPI server
├── requirements.txt
├── .env.example
│
├── Project/
│   ├── main.py                     # Validation runner (CLI entry point)
│   ├── runner.py                   # Subprocess wrapper + results recorder
│   ├── results_store.py            # SQLite history store
│   ├── db/
│   │   ├── factory.py              # Connector factory (reads SRC_N_* env vars)
│   │   ├── postgres.py
│   │   ├── mssqlserver.py
│   │   ├── athena.py
│   │   └── snowflake.py
│   ├── utils/
│   │   ├── utility.py              # run_id, summary CSV, logging
│   │   └── semantic_normalize.py   # JSON/JSONB canonicalization
│   └── output/                     # Project-level validation output
│
├── src/
│   ├── notifier.py                 # Slack + email failure notifications
│   ├── rule_book.py                # Rule management
│   ├── validation_pipeline.py      # AI column mapping pipeline
│   ├── sql_extractor/extractors.py # Schema-only extractors (list tables/columns)
│   ├── generated_queries/
│   │   ├── ai_sql_generator.py     # AI SQL generation (dialect-aware)
│   │   └── yaml_config_writer.py   # YAML config file writer
│   ├── gemini_connector/
│   │   ├── api.py                  # FastAPI app
│   │   ├── tools.py                # 24 tool implementations
│   │   ├── gemini_agent.py         # GeminiAgent + tool declarations
│   │   ├── jira_client.py          # Jira REST API client
│   │   ├── auth.py                 # JWT / static / dev auth
│   │   ├── approval_store.py       # Mapping approval persistence
│   │   └── audit.py                # Append-only audit logger
│   └── matching/                   # Fuzzy + exact column matching
│
├── config/
│   ├── exclusions.yaml             # Global column exclusions
│   ├── postgresql_exclusions.yaml
│   ├── mssql_exclusions.yaml
│   ├── athena_exclusions.yaml
│   └── redshift_exclusions.yaml
│
├── output/                         # Validation results, audit log, plans
├── token_usage_analysis/           # AI token logging + cost reporting
├── docs/                           # Architecture, API, deployment docs
└── _unused/                        # Archived files no longer in use
```

---

## 16. Documentation Index

| Topic | Document |
|---|---|
| System architecture | `docs/architecture/system-architecture.md` |
| Gemini connector integration | `docs/architecture/gemini-integration.md` |
| Security & auth | `docs/architecture/security-architecture.md` |
| All 24 connector tools | `docs/api/connector-tools.md` |
| RBAC roles & permissions | `docs/api/authorization.md` |
| Rule Book | `docs/rules/rule-book.md` |
| Approval workflow | `docs/human-in-the-loop/review-workflow.md` |
| Audit trail format | `docs/human-in-the-loop/audit-trail.md` |
| Validation strategies | `docs/validation/validation-strategies.md` |
| Supported databases | `docs/validation/supported-databases.md` |
| Local setup | `docs/deployment/local-setup.md` |
| All environment variables | `docs/deployment/environment.md` |
| GCP Cloud Run deployment | `docs/deployment/gcp-deployment.md` |
