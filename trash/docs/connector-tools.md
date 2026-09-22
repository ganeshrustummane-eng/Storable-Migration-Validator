# Connector Tools Reference

All 24 tools exposed to Gemini. Each tool call arrives at `POST /tools/{tool_name}` with body `{"arguments": {...}}`.

Write tools (marked ✏️) require authentication and a human actor. Read tools (marked 🔍) are public by default.

---

## Discovery Tools

### 🔍 `discover_connections`

Returns all configured source database connections and the Snowflake target.

**Input:** none

**Output:**
```json
{
  "connections": [
    {
      "name": "SRC_1",
      "db_type": "postgresql",
      "host": "postgres.company.com",
      "database": "fms",
      "schema": "public",
      "label": "PostgreSQL (fms)"
    },
    {
      "name": "SNOWFLAKE",
      "db_type": "snowflake",
      "database": "DEV_SITELINK_BRONZE",
      "schema": "AWSDEV_DEVT5000_DBO"
    }
  ]
}
```

**Permission:** None (public)  
**Side effects:** None  
**Security:** Passwords and credentials are never returned.

---

### 🔍 `list_databases`

Lists available databases for a given connection.

**Input:**
```json
{ "connection_name": "SRC_1" }
```

**Output:**
```json
{ "databases": ["fms", "fms_archive", "analytics"] }
```

**Permission:** `SCHEMA_READ`

---

### 🔍 `list_schemas`

Lists schemas within a database.

**Input:**
```json
{ "connection_name": "SRC_1", "database": "fms" }
```

**Output:**
```json
{ "schemas": ["public", "audit", "reporting"] }
```

**Permission:** `SCHEMA_READ`

---

### 🔍 `list_tables`

Lists tables within a schema.

**Input:**
```json
{ "connection_name": "SRC_1", "database": "fms", "schema": "public" }
```

**Output:**
```json
{ "tables": ["customer", "orders", "events", "payments"] }
```

**Permission:** `SCHEMA_READ`

---

### 🔍 `get_table_schema`

Returns column definitions for a specific table.

**Input:**
```json
{
  "connection_name": "SRC_1",
  "database": "fms",
  "schema": "public",
  "table": "customer"
}
```

**Output:**
```json
{
  "table": "customer",
  "columns": [
    { "name": "id", "type": "integer", "nullable": false, "is_primary_key": true },
    { "name": "email", "type": "character varying", "nullable": true },
    { "name": "created_at", "type": "timestamp without time zone", "nullable": true }
  ],
  "primary_keys": ["id"]
}
```

**Permission:** `SCHEMA_READ`

---

## Mapping Tools

### 🔍 `get_table_mapping`

Returns the current mapping status for a source table.

**Input:**
```json
{ "source_table": "customer", "layer": "bronze" }
```

**Output:**
```json
{
  "source_table": "customer",
  "target_table": "CUSTOMER",
  "layer": "bronze",
  "status": "complete",
  "total_mappings": 38,
  "active_mappings": 38,
  "skipped": 4,
  "pending_review": 2,
  "coverage_pct": 90.5
}
```

**Permission:** `MAPPING_READ`

---

### 🔍 `get_column_mappings`

Returns detailed column-level mapping entries for a table.

**Input:**
```json
{ "source_table": "customer", "layer": "bronze", "page": 1, "page_size": 50 }
```

**Output:**
```json
{
  "mappings": [
    {
      "source_column": "created_at",
      "target_column": "CREATED_AT",
      "match_method": "exact",
      "confidence": 1.0,
      "transformation_rule": "timestamp",
      "status": "auto_accepted"
    },
    {
      "source_column": "cust_id",
      "target_column": "CUSTOMER_ID",
      "match_method": "fuzzy",
      "confidence": 0.82,
      "transformation_rule": "text",
      "status": "pending"
    }
  ],
  "page": 1,
  "page_size": 50,
  "total": 38,
  "has_more": false
}
```

**Permission:** `MAPPING_READ`

---

### 🔍 `get_pending_reviews`

Returns all mappings awaiting human review.

**Input:**
```json
{ "table": "customer" }
```
*(table is optional — omit for all pending across all tables)*

**Output:**
```json
{
  "pending": [
    {
      "record_id": "abc-123",
      "table": "customer",
      "source_column": "cust_id",
      "target_column": "CUSTOMER_ID",
      "confidence": 0.82,
      "ai_recommendation": "High probability match — same business concept",
      "created_at": "2026-08-25T14:30:00Z"
    }
  ],
  "total": 1
}
```

**Permission:** `MAPPING_READ`

---

## Rules Tools

### 🔍 `get_rule`

Returns a specific rule by ID.

**Input:**
```json
{ "rule_id": "R001" }
```

**Output:**
```json
{
  "rule_id": "R001",
  "name": "Timestamp normalization",
  "type": "transformation",
  "pattern": "timestamp.*",
  "action": "Cast to text using dialect-appropriate function",
  "status": "active",
  "version": 2
}
```

**Permission:** `RULE_READ`

---

### 🔍 `get_applicable_rules`

Returns all rules that apply to a given table.

**Input:**
```json
{ "source_table": "events", "source_type": "postgresql" }
```

**Output:**
```json
{
  "rules": [
    {
      "rule_id": "R001",
      "name": "Timestamp normalization",
      "applies_to": "timestamp columns",
      "status": "active"
    },
    {
      "rule_id": "EXCL-001",
      "name": "Fivetran column exclusion",
      "applies_to": "_fivetran_* columns",
      "status": "active"
    }
  ]
}
```

**Permission:** `RULE_READ`

---

## Plan Tools

### 🔍 `get_validation_plan`

Returns the current stored validation plan for a table.

**Input:**
```json
{ "source_table": "customer", "layer": "bronze" }
```

**Output:**
```json
{
  "source_table": "customer",
  "target_table": "CUSTOMER",
  "status": "complete",
  "generated_at": "2026-08-25T17:00:25Z",
  "model_used": "gpt-4o",
  "active_mappings": 38,
  "skipped": 4,
  "exact_matches": 35,
  "fuzzy_matches": 2,
  "ai_resolved": 1,
  "pk_mismatch": false
}
```

**Permission:** `PLAN_READ`

---

### ✏️ `generate_validation_plan`

Generates a new validation plan for a source table. Triggers schema extraction, column matching, and confidence scoring.

**Input:**
```json
{
  "source_table": "customer",
  "layer": "bronze",
  "force_regenerate": false
}
```

**Output:**
```json
{
  "source_table": "customer",
  "status": "complete",
  "active_mappings": 38,
  "pending_review": 2,
  "auto_accepted": 36,
  "warnings": ["PK mismatch: source has [id], target has [ID, _FIVETRAN_START]"]
}
```

**Permission:** `PLAN_READ` (generation) + `MAPPING_APPROVE` (auto-accept)  
**Side effects:** Writes plan to `output/plans/<layer>/<table>.plan.json`; creates `ApprovalRecord` entries for low-confidence mappings

---

### 🔍 `generate_validation_sql`

Generates the validation SQL queries from the current plan.

**Input:**
```json
{ "source_table": "customer", "layer": "bronze", "validation_type": "count_validation" }
```

**Output:**
```json
{
  "source_sql": "SELECT COUNT(*) AS source_row_count FROM public.customer",
  "target_sql": "SELECT COUNT(*) AS target_row_count FROM DEV_DB.PUBLIC.CUSTOMER WHERE _FIVETRAN_ACTIVE = TRUE",
  "validation_type": "count_validation"
}
```

**Permission:** `PLAN_READ`  
**Side effects:** None (read-only SQL generation)

---

## Validation Execution Tools

### ✏️ `execute_validation`

Executes the validation SQL against source and target databases.

**Input:**
```json
{
  "source_table": "customer",
  "layer": "bronze",
  "actor": "jane.doe@company.com",
  "expected_version": 3
}
```

**Output:**
```json
{
  "status": "WARNING",
  "coverage_pct": 99.1,
  "source_row_count": 50000,
  "target_row_count": 49955,
  "row_difference": 45,
  "failed_checks": 1,
  "execution_time_s": 4.2,
  "new_version": 4
}
```

**Permission:** `VALIDATION_EXECUTE`  
**Side effects:** Runs live SQL against source and target databases; writes audit record; bumps version

---

### 🔍 `get_validation_result`

Returns the most recent validation result for a table.

**Input:**
```json
{ "source_table": "customer", "layer": "bronze" }
```

**Output:**
```json
{
  "status": "WARNING",
  "coverage_pct": 99.1,
  "last_run": "2026-08-25T17:05:00Z",
  "failed_checks": 1,
  "pass_count": 37,
  "warning_count": 1
}
```

**Permission:** `VALIDATION_READ`

---

### 🔍 `get_validation_failures`

Returns details of failed validation checks.

**Input:**
```json
{ "source_table": "customer", "layer": "bronze", "page": 1, "page_size": 50 }
```

**Output:**
```json
{
  "failures": [
    {
      "column": "email",
      "check_type": "null_count",
      "source_value": "12",
      "target_value": "0",
      "difference": "12 NULLs in source not present in target"
    }
  ],
  "total": 1
}
```

**Permission:** `VALIDATION_READ`

---

## Summary and Metrics Tools

### 🔍 `get_migration_summary`

Returns an aggregated migration status for a layer.

**Input:**
```json
{ "layer": "bronze" }
```

**Output:**
```json
{
  "layer": "bronze",
  "total_tables": 12,
  "validated": 8,
  "pending_plan": 2,
  "pending_review": 2,
  "pass": 6,
  "warning": 2,
  "fail": 0,
  "overall_coverage_pct": 97.3,
  "tables_needing_attention": ["orders", "payments"]
}
```

**Permission:** `MAPPING_READ`

---

### 🔍 `get_coverage`

Returns per-table coverage report, filterable by threshold.

**Input:**
```json
{ "layer": "bronze", "threshold": 0.95, "page": 1, "page_size": 20 }
```

**Output:**
```json
{
  "items": [
    { "table": "orders", "coverage_pct": 87.5, "unmatched_columns": 6, "skipped_columns": 4 },
    { "table": "payments", "coverage_pct": 91.2, "unmatched_columns": 3, "skipped_columns": 2 }
  ],
  "page": 1,
  "total": 2,
  "has_more": false
}
```

**Permission:** `MAPPING_READ`  
**Note:** `page_size` capped at 200

---

### 🔍 `get_business_metrics`

Returns aggregate business ROI metrics.

**Input:** none

**Output:**
```json
{
  "tables_processed": 12,
  "columns_processed": 486,
  "mappings_automated": 441,
  "mappings_reviewed": 45,
  "mappings_rejected": 3,
  "automation_rate_pct": 90.7,
  "manual_sql_avoided": 48,
  "ai_calls_made": 23,
  "total_execution_time_s": 142.5
}
```

**Permission:** `VALIDATION_READ`

---

## Write-Back Tools

All write-back tools require:
- Authentication (valid bearer token)
- RBAC permission
- Human actor (email or username — not `gemini_ai` or `ai`)
- Expected version for optimistic concurrency

### ✏️ `approve_mapping`

Approves a pending column mapping.

**Input:**
```json
{
  "record_id": "abc-123",
  "actor": "jane.doe@company.com",
  "reason": "Confirmed via source system DDL review",
  "expected_version": 0
}
```

**Output:**
```json
{
  "status": "approved",
  "record_id": "abc-123",
  "actor": "jane.doe@company.com",
  "new_version": 1,
  "approved_at": "2026-08-25T17:10:00Z"
}
```

**Permission:** `MAPPING_APPROVE`  
**Side effects:** Updates ApprovalStore, writes AuditRecord, bumps VersionStore

---

### ✏️ `reject_mapping`

Rejects a pending column mapping.

**Input:**
```json
{
  "record_id": "abc-123",
  "actor": "jane.doe@company.com",
  "reason": "Wrong target — email should map to EMAIL_ADDRESS not CUSTOMER_EMAIL",
  "expected_version": 0
}
```

**Output:**
```json
{
  "status": "rejected",
  "record_id": "abc-123",
  "actor": "jane.doe@company.com",
  "new_version": 1
}
```

**Permission:** `MAPPING_REJECT`  
**Side effects:** Updates ApprovalStore, writes AuditRecord, bumps VersionStore

---

### ✏️ `modify_mapping`

Modifies a column mapping with a corrected target column or transformation rule.

**Input:**
```json
{
  "record_id": "abc-123",
  "actor": "jane.doe@company.com",
  "new_target_column": "EMAIL_ADDRESS",
  "new_transformation_rule": "text",
  "reason": "Corrected target column name",
  "expected_version": 0
}
```

**Output:**
```json
{
  "status": "modified",
  "record_id": "abc-123",
  "new_target_column": "EMAIL_ADDRESS",
  "new_version": 1
}
```

**Permission:** `MAPPING_MODIFY`

---

### ✏️ `approve_rule`

Activates a draft rule. Requires `RULE_ADMIN` role.

**Input:**
```json
{
  "rule_id": "R042",
  "actor": "admin@company.com",
  "expected_version": 0
}
```

**Output:**
```json
{
  "status": "active",
  "rule_id": "R042",
  "new_version": 1
}
```

**Permission:** `RULE_ACTIVATE` (RULE_ADMIN role only)

---

### ✏️ `approve_plan`

Approves a validation plan, enabling execution.

**Input:**
```json
{
  "source_table": "customer",
  "actor": "jane.doe@company.com",
  "expected_version": 1
}
```

**Output:**
```json
{
  "status": "approved",
  "source_table": "customer",
  "new_version": 2
}
```

**Permission:** `PLAN_APPROVE`

---

## Error Response Format

All tools return errors in a consistent format:

```json
{
  "error": true,
  "code": "OBJECT_NOT_FOUND",
  "message": "No validation plan found for table 'customer' in layer 'bronze'",
  "request_id": "uuid-here"
}
```

### Error Codes

| Code | HTTP Status | Description |
|------|------------|-------------|
| `AUTHENTICATION_ERROR` | 401 | Invalid or missing bearer token |
| `AUTHORIZATION_ERROR` | 403 | Role lacks required permission |
| `VERSION_CONFLICT` | 409 | Expected version does not match current version |
| `OBJECT_NOT_FOUND` | 404 | Requested entity does not exist |
| `INVALID_REQUEST` | 400 | Missing or invalid input parameters |
| `SOURCE_UNAVAILABLE` | 503 | Cannot connect to source database |
| `TARGET_UNAVAILABLE` | 503 | Cannot connect to Snowflake |
| `PLAN_INVALID` | 422 | Plan has structural errors |
| `VALIDATION_FAILED` | 422 | Validation execution error |
| `EXECUTION_TIMEOUT` | 504 | Validation query timed out |
