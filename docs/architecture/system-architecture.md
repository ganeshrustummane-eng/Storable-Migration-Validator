# System Architecture

## Overview

Migration Validator is a three-tier system: a conversational AI layer (Gemini), a governed connector layer (FastAPI), and a deterministic validation engine (Python core).

---

## Component Map

```
┌──────────────────────────────────────────────────────────────────────────┐
│  TIER 1 — AI LAYER                                                       │
│                                                                          │
│  ┌─────────────────────┐      ┌────────────────────────────────────┐    │
│  │   Gemini Client      │      │   EPAM DIAL Proxy                  │    │
│  │   (google.com /      │      │   (ai-proxy.lab.epam.com)          │    │
│  │    Vertex AI)        │      │   GPT-4o · Claude · Gemini · Llama │    │
│  └──────────┬───────────┘      └──────────────────────┬─────────────┘    │
│             │ function calling                         │ AI mapping       │
└─────────────┼──────────────────────────────────────────┼──────────────────┘
              │                                          │
              ▼                                          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  TIER 2 — CONNECTOR LAYER (FastAPI · Port 8001)                         │
│                                                                          │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────────────────────────┐ │
│  │  auth.py  │  │ authz.py │  │               tools.py                │ │
│  │           │  │          │  │                                        │ │
│  │ JWT mode  │  │ VIEWER   │  │  Discovery: discover_connections       │ │
│  │ Static    │  │ REVIEWER │  │             list_databases             │ │
│  │ Dev mode  │  │ RULE_ADM │  │             list_schemas               │ │
│  └──────────┘  │ VAL_OPER │  │             list_tables                │ │
│                │ ADMIN    │  │             get_table_schema            │ │
│                └──────────┘  │                                        │ │
│                              │  Mapping:   get_table_mapping          │ │
│  ┌──────────────────────┐    │             get_column_mappings         │ │
│  │      api.py           │    │             get_pending_reviews         │ │
│  │  19 REST endpoints   │    │                                        │ │
│  │  /health             │    │  Rules:     get_rule                   │ │
│  │  /tools              │    │             get_applicable_rules        │ │
│  │  /chat               │    │                                        │ │
│  │  /pending            │    │  Plans:     get_validation_plan        │ │
│  │  /summary/{layer}    │    │             generate_validation_plan    │ │
│  │  /coverage           │    │             generate_validation_sql     │ │
│  │  /table/{table}      │    │                                        │ │
│  │  /metrics            │    │  Execution: execute_validation          │ │
│  │  /audit              │    │             get_validation_result       │ │
│  │  /approve/mapping    │    │             get_validation_failures     │ │
│  │  /reject/mapping     │    │                                        │ │
│  │  /modify/mapping     │    │  Summary:   get_migration_summary      │ │
│  │  /approve/rule       │    │             get_coverage               │ │
│  │  /approve/plan       │    │             get_business_metrics        │ │
│  │  /execute/validation │    │                                        │ │
│  └──────────────────────┘    │  Write:     approve_mapping            │ │
│                              │             reject_mapping              │ │
│  ┌──────────────────────┐    │             modify_mapping             │ │
│  │   Persistence Layer   │    │             approve_rule               │ │
│  │                       │    │             approve_plan               │ │
│  │ ApprovalStore (JSONL) │    └────────────────────────────────────────┘ │
│  │ AuditLogger   (JSONL) │                                              │
│  │ VersionStore  (JSON)  │                                              │
│  │ MetricsTracker(JSONL) │                                              │
│  └──────────────────────┘                                              │
└─────────────────────────────────────────┬────────────────────────────────┘
                                          │
                                          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  TIER 3 — VALIDATION ENGINE                                              │
│                                                                          │
│  ValidationPipeline                                                      │
│    ├── ExtractorFactory (connects to source + Snowflake)                 │
│    ├── CandidateMatcher (exact → normalized → fuzzy → AI)               │
│    ├── ConfidenceScorer (name + type + position + learned examples)      │
│    ├── AIRulePlanner (DIAL proxy — ambiguous columns only)               │
│    ├── ExclusionManager (Fivetran + user-defined + pattern rules)        │
│    └── CanonicalValidationPlan (immutable plan → SQL + YAML)            │
│                                                                          │
│  PlanStore      → output/plans/<layer>/<table>.plan.json                 │
│  YAMLWriter     → Project/config/<layer>/<type>/<table>.yaml             │
│  SQLGenerator   → deterministic SQL from plan                            │
│  RuleBook       → rule_book_learned.json                                 │
└────────────────┬──────────────────────────────┬─────────────────────────┘
                 │                              │
                 ▼                              ▼
    ┌────────────────────┐          ┌───────────────────────┐
    │   SOURCE SYSTEMS   │          │    TARGET SYSTEM      │
    │                    │          │                       │
    │  PostgreSQL        │          │  Snowflake            │
    │  (psycopg2)        │          │  Bronze Layer         │
    │                    │          │  Silver Layer         │
    │  MSSQL             │          │  Gold Layer           │
    │  (pyodbc)          │          │  Reporting Layer      │
    │                    │          └───────────────────────┘
    │  AWS Athena        │
    │  (boto3)           │
    │                    │
    │  AWS Redshift      │
    │  (psycopg2)        │

---

## Key Design Decisions

### 1. Canonical Validation Plan as the single source of truth

The `CanonicalValidationPlan` is an immutable, serializable data structure that captures all column mapping decisions. Both the SQL generator and YAML config generator consume the same plan — guaranteeing consistency.

```
Column matching (AI + fuzzy + exact)
          ↓
CanonicalValidationPlan
     ├── SQL generator (validation queries)
     └── YAML writer (Project/config/<layer>/)
```

### 2. Confidence-based routing

Every column mapping carries a confidence score (0.0–1.0). This score routes mappings through the human approval workflow:

- ≥ 0.95 → auto-accepted, no human review needed
- 0.75–0.94 → queued in ApprovalStore for human reviewer
- < 0.75 → mandatory human review, never auto-accepted

Thresholds are configurable via `CONFIDENCE_AUTO_ACCEPT` and `CONFIDENCE_REVIEW` env vars.

### 3. AI is advisory; humans decide

Gemini and the DIAL proxy propose column mappings and generate recommendations. Write-back tools (`approve_mapping`, `approve_plan`, `execute_validation`) require a human actor string. The `gemini_ai` and `ai` actor strings are blocked at tool level.

### 4. Append-only persistence for auditability

`output/audit_log.jsonl` is written once per action and never modified. Approval decisions in `output/approval_store.jsonl` use a latest-line-per-ID pattern — new lines overwrite logically, but history is preserved.

### 5. DIAL proxy for AI backend flexibility

The DIAL proxy (`ai-proxy.lab.epam.com`) provides a single API key that routes to GPT-4o, Claude, Gemini, Llama, or Mistral. The validator calls DIAL for column mapping AI. The connector calls Gemini directly for conversational agent functionality.

---

## Deployment Topology

```
┌─────────────────────────────────────────────────────────┐
│  Developer Machine / Server                             │
│                                                         │
│  ┌────────────────────────────┐                        │
│  │  start_connector.py        │  Port 8001             │
│  │  uvicorn FastAPI server    │  ← Gemini calls here   │
│  └────────────────────────────┘                        │
│                                                         │
│  ┌────────────────────────────┐                        │
│  │  streamlit run webapp/app.py│  Port 8501             │
│  │  Streamlit web UI          │  ← Browser access      │
│  └────────────────────────────┘                        │
│                                                         │
│  ┌────────────────────────────┐                        │
│  │  python -m src.validate_cli│  Terminal              │
│  │  CLI interface             │  ← Engineer access     │
│  └────────────────────────────┘                        │
│                                                         │
│  .env  (all secrets, git-ignored)                       │
│  output/  (plans, audit, approvals — git-ignored)       │
└─────────────────────────────────────────────────────────┘
           │                        │
           ▼                        ▼
    Source databases          Snowflake account
    (PostgreSQL / MSSQL /     (external)
     Athena / Redshift)
```

---

## Data Flow Summary

1. **Schema extraction** — `ExtractorFactory` connects to source DB and Snowflake, reads column names, types, primary keys
2. **Exclusion filtering** — `ExclusionManager` removes Fivetran audit columns and user-defined exclusions
3. **Column matching** — exact → normalized → fuzzy (RapidFuzz) → AI (DIAL) for ambiguous columns
4. **Plan generation** — `CanonicalValidationPlan` built from all match results; confidence scores assigned
5. **Plan persistence** — `PlanStore` writes `output/plans/<layer>/<table>.plan.json`
6. **Human review** — low-confidence mappings queued in `ApprovalStore`; humans approve/modify/reject via web UI or Gemini
7. **SQL generation** — deterministic SQL generated from approved plan
8. **Validation execution** — SQL runs against source and target; results compared
9. **Audit** — every action logged to `output/audit_log.jsonl`

---

## Technology Stack

| Component | Technology | Version |
|-----------|-----------|---------|
| Connector API | FastAPI + Uvicorn | 0.110+ |
| Web UI | Streamlit | Latest |
| CLI | Python argparse | 3.10+ |
| Gemini integration | google-generativeai | 0.5.0+ |
| Authentication | PyJWT | 2.8.0+ |
| PostgreSQL | psycopg2-binary | Latest |
| MSSQL | pyodbc | Latest |
| Snowflake | snowflake-connector-python | Latest |
| AWS Athena | boto3 | Latest |
| AWS Redshift | psycopg2-binary | Latest |
| Fuzzy matching | RapidFuzz | Latest |
| AI proxy | openai SDK (DIAL) | Latest |
| Testing | pytest + pytest-cov | Latest |
