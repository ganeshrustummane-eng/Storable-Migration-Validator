# Data Flow

## End-to-End Pipeline

```
1. SCHEMA EXTRACTION
   ├── Source: ExtractorFactory → psycopg2 / pyodbc / boto3
   │   SELECT column_name, data_type FROM information_schema.columns
   │   WHERE table_name = '<table>'
   │
   └── Target: SnowflakeExtractor
       SELECT column_name, data_type FROM information_schema.columns
       WHERE table_name = '<TABLE>' AND table_schema = '<SCHEMA>'

2. EXCLUSION FILTERING
   └── ExclusionManager applies in priority order:
       a. STATIC_EXCLUDE_COLUMNS (7 Fivetran columns — always excluded)
       b. Pattern rules (e.g. ^_FIVETRAN_.*)
       c. Table-specific rules from config YAML
       d. User-defined global exclusions from per-DB-type YAML
       e. Run-specific exclusions (CLI/UI selection)

3. CANDIDATE MATCHING
   └── CandidateMatcher runs four passes in order:
       a. Exact match (case-insensitive: source.lower() == target.lower())
       b. Normalized match (snake_case → stripped → canonical form)
       c. Fuzzy match (RapidFuzz token_sort_ratio ≥ configured threshold)
       d. AI match (DIAL proxy — only for unmatched columns)

4. CONFIDENCE SCORING
   └── Multi-factor score per mapping:
       - Name similarity (0.0–1.0)
       - Type compatibility (compatible types score higher)
       - Positional proximity (bonus for same position in schema)
       - Learned example bonus (if a prior match is in rule_book_learned.json)
       → Final confidence: 0.0–1.0

5. PLAN GENERATION
   └── CanonicalValidationPlan built from all match results:
       {
         source_table, target_table,
         mappings: [ColumnMappingEntry × N],
         status: complete|partial|ambiguous|invalid,
         pk_mismatch, warnings, ambiguities
       }

6. APPROVAL ROUTING
   ├── confidence ≥ 0.95 → AUTO_ACCEPTED (no human review)
   ├── 0.75 ≤ confidence < 0.95 → ApprovalStore{status: PENDING}
   └── confidence < 0.75 → ApprovalStore{status: PENDING, mandatory review}

7. HUMAN REVIEW (for PENDING items)
   ├── Approve → ApprovalRecord{status: APPROVED, actor, reason, version}
   ├── Modify  → ApprovalRecord{status: MODIFIED, new_target_column, new_rule}
   └── Reject  → ApprovalRecord{status: REJECTED, rejection_reason}

8. SQL GENERATION (from approved plan)
   └── SQLQueryGenerator produces:
       a. Count validation SQL:
          Source: SELECT COUNT(*) AS source_row_count FROM <schema>.<table>
          Target: SELECT COUNT(*) AS target_row_count
                  FROM <DB>.<SCHEMA>.<TABLE>
                  WHERE _FIVETRAN_ACTIVE = TRUE
       b. Data validation SQL:
          Source: SELECT COALESCE(CAST(col AS TEXT), '<<NULL>>') AS col_normalized...
          Target: SELECT COALESCE(CAST(col AS STRING), '<<NULL>>') AS col_normalized...
          WHERE _FIVETRAN_ACTIVE = TRUE

9. YAML GENERATION (config files for batch runs)
   └── YAMLConfigWriter produces:
       Project/config/<layer>/count_validation/<layer>.yaml
       Project/config/<layer>/data_validation/<table>.yaml

10. VALIDATION EXECUTION
    ├── Source query runs against source DB
    ├── Target query runs against Snowflake
    ├── Results compared row-by-row using PK join
    └── Mismatches recorded with source_value / target_value

11. AUDIT LOGGING
    └── Every approve/reject/modify/execute → AuditRecord appended to
        output/audit_log.jsonl
```

---

## Normalization Rules Applied During SQL Generation

The data validation SQL applies cross-dialect normalization to enable apples-to-apples comparison:

| Source Type | PostgreSQL SQL | Snowflake SQL | Notes |
|-------------|---------------|---------------|-------|
| boolean | `CASE WHEN col = true THEN '1' WHEN col = false THEN '0' ELSE NULL END` | `CASE WHEN col = TRUE THEN '1' ...` | Normalized to 0/1 string |
| timestamp | `TO_CHAR(col, 'YYYY-MM-DD HH24:MI:SS')` | `TO_VARCHAR(col, 'YYYY-MM-DD HH24:MI:SS')` | Timezone stripped |
| integer/numeric | `CAST(col AS TEXT)` | `CAST(col AS STRING)` | Cross-dialect text cast |
| hstore/json | `col::text` | `col::STRING` | JSON stringified for comparison |
| uuid | `CAST(col AS TEXT)` | `CAST(col AS STRING)` | UUID as string |
| All types | `COALESCE(..., '<<NULL>>')` | `COALESCE(..., '<<NULL>>')` | NULL sentinel for comparison |

---

## Multi-Source Data Flow

```
Connection Registry (.env)
  SRC_1: PostgreSQL (fms.public)
  SRC_2: MSSQL (DevT5000.dbo)
  SRC_3: Athena (migration_validator_test)

Snowflake Target:
  DEV_SITELINK_BRONZE.AWSDEV_DEVT5000_DBO
  DEV_SITELINK_SILVER.*
  DEV_SITELINK_GOLD.*

Migration Validator discovers all connections via discover_connections()
and validates tables across any source → Snowflake pair.
```

---

## Persistence File Map

> **Stale rows (2026-09-23):** the `tools.py`/`ApprovalStore`/`VersionStore`/
> `MetricsTracker` readers/writers below were deleted with the chatbot/agent
> layer ("removed chat bot" commit, 2026-09-22). `output/plans/*.plan.json`
> is still written by `ValidationPipeline` and read by the webapp directly.

| File | Format | Written by | Read by |
|------|--------|-----------|---------|
| `output/plans/<layer>/<table>.plan.json` | JSON (schema v1) | ValidationPipeline | webapp |
| `output/audit_log.jsonl` | Append-only JSONL | *(removed — was AuditLogger)* | *(removed — was audit tool, webapp)* |
| `output/approval_store.jsonl` | Latest-line-per-ID JSONL | *(removed — was ApprovalStore)* | *(removed — was tools.py, webapp)* |
| `output/entity_versions.json` | Flat JSON dict | *(removed — was VersionStore)* | *(removed — was tools.py)* |
| `output/connector_metrics.jsonl` | JSONL | *(removed — was MetricsTracker)* | *(removed — was metrics tool, webapp)* |
| `Project/config/<layer>/count_validation/<layer>.yaml` | YAML | YAMLConfigWriter | batch runner |
| `Project/config/<layer>/data_validation/<table>.yaml` | YAML | YAMLConfigWriter | batch runner |
| `config/postgresql_exclusions.yaml` | YAML | CLI (cmd_add_exclusion) | ExclusionManager |
| `config/database_registry.yaml` | YAML | Manual | validate_cli.py |
| `rule_book_learned.json` | JSON | cmd_add_rule | RuleBook |
| `token_usage_analysis/logs/token_usage.jsonl` | JSONL | AI pipeline | webapp usage tab |
| `.dial_model_cache.json` | JSON | _get_display_models | validate_cli.py |
