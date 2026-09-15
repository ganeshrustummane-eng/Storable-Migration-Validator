# Rule Book

## Overview

The Rule Book is the governance layer that controls which columns are included in validation, how data types are normalized for cross-dialect comparison, and which AI-proposed patterns are approved for reuse.

---

## Rule Categories

### 1. Static Exclusions (Built-in)

Hardcoded in `src/validate_cli.py`. Always excluded, cannot be overridden.

```python
STATIC_EXCLUDE_COLUMNS = [
    "_fivetran_synced",
    "_fivetran_deleted",
    "_fivetran_id",
    "_fivetran_index",
    "_fivetran_start",
    "_fivetran_end",
    "_fivetran_active"
]
```

**Reason:** These are Fivetran audit/internal columns that exist only in the Snowflake target and have no source equivalent. Including them would cause false-positive mismatches on every validation.

---

### 2. Pattern Exclusions

Regex patterns applied to column names. Configured in per-DB-type YAML files.

```yaml
# config/postgresql_exclusions.yaml (same shape for mssql_exclusions.yaml,
# athena_exclusions.yaml, redshift_exclusions.yaml)
pattern_exclusions:
  patterns:
    - pattern: "^_FIVETRAN_.*"
      reason: "Fivetran internal metadata"
      applies_to: [source, target]
```

**Pattern syntax:** Python `re` module. Case-insensitive matching by default.

---

### 3. Global User-Defined Exclusions

Columns added by engineers via CLI or web UI. Persisted to per-DB-type YAML files.

```yaml
user_global_exclusions:
  - column_name: uuid
    reason: "UUID field not loaded to Snowflake"
    applies_to: [source, target]
    added_by: CLI
    date_added: "2026-08-20"
```

Apply to all tables of that DB type. Added via:
- CLI: `python -m src.validate_cli` → option 6 (Add Exclusion)
- Web UI: Exclusions tab
- API: `EXCLUSION_MODIFY` permission

---

### 4. Table-Specific Exclusions

Run-specific exclusions selected during a validation run. Ephemeral — not persisted.

When the CLI asks "Select columns to exclude," option `T` applies exclusions to the current table only for that run.

---

### 5. Transformation Rules

Applied to each column during SQL generation. Determined by the AI mapping pipeline.

| Rule | Applies to | SQL Transform |
|------|-----------|---------------|
| `text` | varchar, char, text | Direct cast to TEXT/STRING |
| `timestamp` | timestamp, datetime | Format to `YYYY-MM-DD HH24:MI:SS` |
| `boolean` | bool, bit | CASE to '0'/'1' string |
| `integer` | int, bigint, smallint | Cast to TEXT/STRING |
| `json` | json, jsonb, hstore, VARIANT | Cast to text |

---

### 6. Learned Rules

AI-proposed rules that have been reviewed and approved by a RULE_ADMIN.

```json
{
  "rule_id": "LEARNED-001",
  "name": "Timestamp with timezone normalization",
  "type": "transformation",
  "pattern": ".*_at$",
  "source_type_hint": "timestamp with time zone",
  "action": "Strip timezone, format as YYYY-MM-DD HH24:MI:SS",
  "status": "active",
  "proposed_by": "ai",
  "approved_by": "admin@company.com",
  "approved_at": "2026-08-20T10:00:00Z",
  "version": 1
}
```

Stored in: `rule_book_learned.json`

---

## Rule Lifecycle

```
AI observes a pattern during column mapping
         ↓
AI Proposal (in-memory, during pipeline run)
         ↓
Draft (saved to rule_book_learned.json with status: "draft")
         ↓
Human Review (RULE_ADMIN role required)
   ├── Approve → status: "approved"
   └── Reject  → status: "rejected"
         ↓
Activation (RULE_ACTIVATE permission required)
   → status: "active"
         ↓
Versioned (every change tracked in VersionStore)
         ↓
Applied to future pipeline runs
```

---

## Rule Priority

Rules are applied in this priority order (highest to lowest):

1. Table-specific configured mappings (from YAML config — always wins)
2. Static exclusions (`STATIC_EXCLUDE_COLUMNS`)
3. Pattern exclusions
4. Global user-defined exclusions
5. Learned active rules
6. AI-generated transformation rules

---

## Exclusion Scope Reference

| Scope | CLI Option | Persistence | Applies To |
|-------|-----------|-------------|-----------|
| Global + persist | `G` | YAML file | All future runs for this DB type |
| Table-only | `T` | None | This run only |
| None | `N` | — | No exclusions applied |

---

## Managing Rules via Gemini

```
User: "What rules apply to the events table?"
→ Gemini calls: get_applicable_rules(source_table="events")
→ Returns all active rules for that table

User: "Approve the timestamp normalization rule R042"
→ Gemini calls: approve_rule(rule_id="R042", actor="admin@corp.com", expected_version=0)
→ Rule activated, audit record created
```

---

## Viewing the Rule Book via CLI

```bash
python -m src.validate_cli
# Select option: r (Rules)
# Shows all base rules, learned rules, and their status
```
