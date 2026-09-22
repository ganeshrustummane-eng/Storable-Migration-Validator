# Audit Trail

## Design

Every write action in Migration Validator produces an immutable `AuditRecord` appended to `output/audit_log.jsonl`.

**Security properties:**
- Append-only: records are never modified or deleted
- No secrets: passwords, API keys, and raw connection strings are never logged
- No response payloads: full data results are not logged — only entity identifiers
- Linked to tool call: `request_id` links to observability log

---

## What Gets Logged

| Action | Logged |
|--------|--------|
| `approve_mapping` | Yes |
| `reject_mapping` | Yes |
| `modify_mapping` | Yes |
| `approve_rule` | Yes |
| `approve_plan` | Yes |
| `execute_validation` | Yes |
| Read operations (get_*, list_*) | No |

---

## AuditRecord Format

```jsonl
{
  "audit_id": "f3a2b1c4-...",
  "action": "approve_mapping",
  "entity_type": "mapping",
  "entity_id": "mapping/customer/cust_id",
  "actor": "jane.doe@company.com",
  "user_id": "user-456",
  "timestamp": "2026-08-25T17:10:00.000000Z",
  "request_id": "req-uuid",
  "previous": { "status": "pending" },
  "new_state": { "status": "approved", "decided_by": "jane.doe@company.com" },
  "reason": "Confirmed via source system DDL review",
  "plan_version": 1,
  "source_system": "postgresql",
  "table": "customer",
  "column": "cust_id",
  "rule_id": null,
  "run_id": null,
  "metadata": {}
}
```

---

## Querying the Audit Trail

### Via Gemini

```
User: "Show me all recent approval actions."
→ Gemini calls: GET /audit?n=30
→ Returns last 30 audit records
```

### Via REST API

```bash
# Last 50 actions (default)
GET /audit

# Last 100 actions
GET /audit?n=100
```

### Via Web UI

Tab: **Review & Approve** → Audit History section

Displays last 30 actions as a sortable DataFrame.

### Via Python

```python
from src.gemini_connector.audit import audit_logger

# Last 50 records
records = audit_logger.recent(50)

# Records for a specific entity
records = audit_logger.for_entity("mapping", "mapping/customer/cust_id")

# Records for a specific actor
records = audit_logger.for_actor("jane.doe@company.com", n=200)
```

---

## Compliance Considerations

The audit trail supports:

- **Who:** `actor` (human email) and `user_id` (token subject)
- **What:** `action`, `entity_type`, `entity_id`, `previous` state, `new_state`
- **When:** ISO-8601 UTC timestamp with microseconds
- **Why:** `reason` (human-provided justification)
- **Which version:** `plan_version` (from VersionStore at time of action)
- **Request correlation:** `request_id` (UUID linking to tool call log)
