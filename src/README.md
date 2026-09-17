# 🏗️ Migration Validator — Modular Architecture Guide

> **What this tool does:**  
> Connect to PostgreSQL (source) + Snowflake (target), automatically discover schemas,
> apply type-aware normalization rules, and generate **ready-to-run SQL + YAML files**
> for validating your migration data. Nothing executes against your data automatically —
> you review and run the generated queries yourself.

---

## 📁 Module Structure

```
src/
│
├── 📦 rules/                        ← TYPE-SPECIFIC NORMALIZATION RULES
│   ├── __init__.py                  ← Registry + get_rule_for_type()
│   ├── base_rule.py                 ← Abstract BaseValidationRule + RuleRegistry
│   ├── boolean_rule.py              ← BOOLEAN → '1'/'0'
│   ├── numeric_rule.py              ← NUMERIC → ROUND(2dp) → text
│   ├── timestamp_ntz_rule.py        ← TIMESTAMP → 'YYYY-MM-DD HH24:MI:SS'
│   ├── timestamp_tz_rule.py         ← TIMESTAMPTZ → UTC → 'YYYY-MM-DD HH24:MI:SS'
│   ├── date_rule.py                 ← DATE → 'YYYY-MM-DD'
│   ├── text_rule.py                 ← VARCHAR/TEXT → TRIM  (default fallback)
│   ├── uuid_rule.py                 ← UUID → UPPER(TRIM())
│   ├── integer_rule.py              ← INT/BIGINT → CAST to text
│   ├── json_rule.py                 ← JSON/JSONB → canonical text
│   ├── bytea_rule.py                ← BYTEA → hex text
│   └── null_rule.py                 ← NULL → '<<NULL>>' standalone rule
│
├── 📦 sql_extractor/                ← LIVE SCHEMA EXTRACTION
│   ├── __init__.py
│   ├── base_extractor.py            ← ColumnMetadata, TableMetadata, BaseExtractor
│   ├── postgres_extractor.py        ← PostgreSQL → information_schema.columns
│   └── snowflake_extractor.py       ← Snowflake → INFORMATION_SCHEMA.COLUMNS
│                                       + detects _FIVETRAN_ACTIVE column
│
├── 📦 ai_transformation/            ← COLUMN MAPPING + RULE ASSIGNMENT
│   ├── __init__.py                  ← Exports + AVAILABLE_MODELS list
│   ├── static_rule_mapper.py        ← Deterministic type-pair matching
│   ├── ai_rule_mapper.py            ← DIAL/GPT-4o AI-powered mapping
│   │                                   + user-selectable model
│   └── orchestrator.py              ← Tries AI first, falls back to static
│
├── 📦 generated_queries/            ← SQL + YAML OUTPUT GENERATION
│   ├── __init__.py
│   ├── sql_query_generator.py       ← Builds all 6 validation SQL queries
│   ├── yaml_config_writer.py        ← Writes YAML in project-standard format
│   └── query_output_manager.py      ← Orchestrates SQL + YAML file output
│
├── rule_book.py                     ← EVOLVING RULE CATALOG MANAGER
│   │                                   Loads base rules from rules_catalog.json
│   │                                   + learned rules from rule_book_learned.json
│   │                                   + builds AI prompt injection block
│   └── (generates rule_book_learned.json on first custom rule save)
│
├── validation_pipeline.py           ← END-TO-END PIPELINE ORCHESTRATOR
│   │                                   Extract → Map Rules → Generate SQL+YAML
│   └── CLI: python validation_pipeline.py --pg-table X --sf-table Y
│
├── validate_cli.py                  ← INTERACTIVE CLI (main entry point)
│   │                                   Interactive menu + all subcommands
│   └── Commands: generate | rules | add-rule | list-models | list-tables
│
├── rules_catalog.json               ← RULE DEFINITIONS (machine-readable)
│                                       Used by rule_book.py + AI prompt builder
│
└── (legacy — kept for backward compatibility)
    ├── models.py
    ├── transformation_rules.py
    ├── sql_generators.py
    ├── database_connectors.py
    ├── validator.py
    └── report_generator.py
```

---

## 🔄 How the Pipeline Works

```
User runs: python validate_cli.py generate
                    │
                    ▼
         ┌──────────────────────┐
         │  1. sql_extractor    │  Connect to PostgreSQL + Snowflake
         │                      │  Extract column names + data types
         │  PostgresExtractor   │  via information_schema.columns
         │  SnowflakeExtractor  │  Detects _FIVETRAN_ACTIVE column
         └──────────┬───────────┘
                    │  List[ColumnMetadata]  (source + target)
                    ▼
         ┌──────────────────────┐
         │  2. ai_transformation│  Map source → target columns
         │                      │  Assign 1 rule per column pair
         │  AIRuleMapper        │  Uses DIAL/GPT-4o  (if API key set)
         │  StaticRuleMapper    │  Falls back to type-pair matching
         │  [model selectable]  │  User picks: gpt-4o / gpt-4o-mini / etc.
         └──────────┬───────────┘
                    │  List[ColumnRuleMapping]
                    ▼
         ┌──────────────────────┐
         │  3. generated_queries│  Apply rules → build SQL expressions
         │                      │
         │  SQLQueryGenerator   │  Generates 6 validation queries:
         │  YAMLConfigWriter    │    ① Row count (PostgreSQL)
         │  QueryOutputManager  │    ② Row count (Snowflake)
         │                      │    ③ Normalised SELECT (PostgreSQL)
         │                      │    ④ Normalised SELECT (Snowflake)
         │                      │    ⑤ NULL % per column (PostgreSQL)
         │                      │    ⑥ NULL % per column (Snowflake)
         └──────────┬───────────┘
                    │
          ┌─────────┴──────────┐
          ▼                    ▼
   📄 validation_sql/      📄 validation_sql/
   events_validation.sql   events_validation.yaml
```

---

## 🛡️ Normalization Rules — Quick Reference

| PostgreSQL Type          | Snowflake Type   | Rule          | Expression (PG side)                          |
|--------------------------|------------------|---------------|-----------------------------------------------|
| `BOOLEAN`                | `BOOLEAN`        | `boolean`     | `CASE WHEN col = true THEN '1' … END`         |
| `NUMERIC` / `DECIMAL`    | `NUMBER`         | `numeric`     | `ROUND(CAST(col AS NUMERIC), 2)`              |
| `TIMESTAMP`              | `TIMESTAMP_NTZ`  | `timestamp_ntz` | `TO_CHAR(col, 'YYYY-MM-DD HH24:MI:SS')`    |
| `TIMESTAMPTZ`            | `TIMESTAMP_TZ`   | `timestamp_tz`  | `TO_CHAR(col AT TIME ZONE 'UTC', …)`        |
| `DATE`                   | `DATE`           | `date`        | `TO_CHAR(col, 'YYYY-MM-DD')`                  |
| `VARCHAR` / `TEXT`       | `VARCHAR`        | `text`        | `TRIM(col)`                                   |
| `UUID`                   | `TEXT`           | `uuid`        | `UPPER(TRIM(CAST(col AS TEXT)))`              |
| `INTEGER` / `BIGINT`     | `NUMBER`         | `integer`     | `CAST(col AS TEXT)`                           |
| `JSON` / `JSONB`         | `VARIANT`        | `json`        | `col::jsonb::text`                            |
| `BYTEA`                  | `BINARY`         | `bytea`       | `encode(col, 'hex')`                          |
| **ALL**                  | **ALL**          | `null`        | `COALESCE(CAST(… AS TEXT), '<<NULL>>')`       |

**Snowflake only:** `WHERE _FIVETRAN_ACTIVE = TRUE` — auto-added when column detected.

---

## 🤖 AI Model Selection

```bash
# Interactive model selection
python validate_cli.py               # [2] Select AI model  →  numbered menu

# CLI flag per-run
python validate_cli.py generate --pg-table events --sf-table EVENTS --model gpt-4o-mini

# Environment variable (persistent default)
DIAL_MODEL=gpt-4o-mini  # in .env
```

| Model               | Notes                                  |
|---------------------|----------------------------------------|
| `gpt-4o`            | Default — best accuracy                |
| `gpt-4o-mini`       | Faster, lower cost                     |
| `gpt-4-turbo`       | High context window                    |
| `claude-3-5-sonnet` | Via DIAL bridge                        |
| `gemini-pro`        | Via DIAL bridge                        |

> If `DIAL_API_KEY` is not set, **static rule matching** is used automatically.  
> No error — it just skips AI and uses deterministic type-pair matching.

---

## 📋 YAML Output Format

Every generated SQL query is paired with a YAML config file:

```yaml
tables:
  events:
    validations:
      data_validation:
        source_table_name: events
        source: postgresql
        sourcecolumn: id                    ← first column (no PK dependency)
        sourcequery: |
          SELECT
              COALESCE(CAST(CASE WHEN needs_followup = true THEN '1' ...
          FROM public.events;
        target_table_name: EVENTS
        target: snowflake
        targetcolumn: ID
        targetquery: |
          SELECT
              COALESCE(CAST(CASE WHEN NEEDS_FOLLOWUP = TRUE THEN '1' ...
          FROM dev_edge_bronze.storedge_fms_public.EVENTS
          WHERE _FIVETRAN_ACTIVE = TRUE;
```

---

## 📚 Rule Book — Evolving Rule Catalog

```bash
# View all rules
python validate_cli.py rules

# Add a custom rule (saved permanently)
python validate_cli.py add-rule

# Rules are stored in:
#   src/rules/rules_catalog.json          ← base rules (do not edit manually)
#   src/rule_book_learned.json      ← your custom rules (auto-created)
```

Custom rules added via `add-rule` are **automatically injected** into every AI prompt.

---

## 🚀 Quick Start

```bash
# 1. Configure credentials
cp .env.example .env
# Edit .env → fill SOURCE_* and SNOWFLAKE_* credentials

# 2. Run interactive CLI (from project root)
python src/validate_cli.py

# 3. OR run directly with flags
python src/validate_cli.py generate \
    --pg-table events \
    --sf-table EVENTS \
    --model gpt-4o

# 4. View generated files
ls validation_sql/
#   events_validation.sql    ← 6 SQL queries ready to run
#   events_validation.yaml   ← YAML config for automated runner
```

---

## 📂 Output Files Location

All generated files go to `validation_sql/` (project root):

```
validation_sql/
├── events_validation.sql                   ← 6 SQL validation queries
├── events_validation.yaml                  ← YAML config
├── general_ledger_line_items_validation.sql
├── general_ledger_line_items_validation.yaml
└── <table>_validation.{sql,yaml}           ← one pair per table run
```

---

## ⚙️ Environment Variables

| Variable           | Required | Description                                      |
|--------------------|----------|--------------------------------------------------|
| `SOURCE_HOST`      | ✅       | PostgreSQL host                                  |
| `SOURCE_PORT`      | ✅       | PostgreSQL port (default: 5432)                  |
| `SOURCE_DATABASE`  | ✅       | PostgreSQL database name                         |
| `SOURCE_SCHEMA`    | ✅       | PostgreSQL schema (default: public)              |
| `SOURCE_USERNAME`  | ✅       | PostgreSQL username                              |
| `SOURCE_PASSWORD`  | ✅       | PostgreSQL password                              |
| `SNOWFLAKE_ACCOUNT`| ✅       | Snowflake account (ORG-ACCOUNT format)           |
| `SNOWFLAKE_DATABASE`| ✅      | Snowflake database                               |
| `SNOWFLAKE_SCHEMA` | ✅       | Snowflake schema                                 |
| `SNOWFLAKE_USERNAME`| ✅      | Snowflake username                               |
| `SNOWFLAKE_PASSWORD`| ✅      | Snowflake password                               |
| `DIAL_API_KEY`     | ⚡ Optional | EPAM DIAL API key (enables AI mode)           |
| `DIAL_API_BASE`    | ⚡ Optional | DIAL endpoint URL                             |
| `DIAL_MODEL`       | ⚡ Optional | AI model name (default: gpt-4o)               |

---

## 🧩 Adding a New Rule (Developer Guide)

1. **Add the class** to `src/rules/base_rules.py` (this is the one
   canonical file for all rule logic — see the note at the top of that file
   for why `mssql_rules.py`/`athena_rules.py`/`snowflake_rules.py` are only
   re-export shims, not separate rule implementations).
2. **Register** it in `src/rules/__init__.py` — import the class, then
   `_registry.register(MyRule())` **before** `_registry.register(TextRule())`
   (the wildcard `("*", "*")` fallback must always be registered last).
3. **Optionally** add it to `src/rules/rules_catalog.json` so it shows up in
   `validate_cli.py rules` and in AI prompt context.
4. **Done** — `get_rule_for_type()` picks it up automatically for every
   future `generate`/`batch` run.

No database source is optional: `BaseValidationRule` requires **all four**
dialect methods to be implemented (`_pg_expression`, `_ms_expression`,
`_athena_expression`, `_sf_expression`) — Python enforces this at
class-definition time, so a rule missing one of them fails to instantiate
immediately rather than silently reusing PostgreSQL syntax somewhere it
isn't valid (e.g. MSSQL/Athena don't support `TO_CHAR`/`::jsonb`/`AS TEXT`).

```python
# in src/rules/base_rules.py
class MyRule(BaseValidationRule):
    @property
    def rule_name(self) -> str: return "my_rule"

    @property
    def description(self) -> str: return "My custom transformation"

    @property
    def trigger_pairs(self):
        return [("MY_SOURCE_TYPE", "MY_SNOWFLAKE_TYPE")]

    def _pg_expression(self, col: str) -> str:
        return f"MY_PG_TRANSFORM({col})"

    def _ms_expression(self, col: str) -> str:
        return f"MY_MSSQL_TRANSFORM({col})"

    def _athena_expression(self, col: str) -> str:
        return f"my_athena_transform({col})"

    def _sf_expression(self, col: str) -> str:
        return f"MY_SF_TRANSFORM({col})"
```

---

*Generated by Migration Validator v2.0 — PostgreSQL → Snowflake*
