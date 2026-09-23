# Security Architecture

> **Stale content notice (2026-09-23):** this document describes the security
> model of the removed chatbot/agent API (auth, authz, version store, audit
> trail — `src/connector/auth.py`, `authz.py`, `version_store.py`, `audit.py`,
> all deleted in the "removed chat bot" commit, 2026-09-22). None of this is
> live code today. Kept for historical reference only.

## Overview

Migration Validator implements a layered security model: authentication at the API boundary, role-based authorization per operation, optimistic concurrency control for write conflicts, and an append-only audit trail for compliance.

---

## Security Layers

```
Gemini / API Client
         │
         ▼
┌────────────────────────────────────────┐
│  Layer 1: Authentication               │
│  Bearer token validation               │
│  JWT / Static / Dev modes              │
└────────────────────┬───────────────────┘
                     │ AuthResult{user_id, email, roles, permissions}
                     ▼
┌────────────────────────────────────────┐
│  Layer 2: Authorization (RBAC)         │
│  require_permission(auth, permission)  │
│  Resource-level allowlists             │
│  Per-user table restrictions           │
└────────────────────┬───────────────────┘
                     │
                     ▼
┌────────────────────────────────────────┐
│  Layer 3: AI Self-Approval Guard       │
│  actor ∉ {"", "gemini_ai", "ai"}       │
└────────────────────┬───────────────────┘
                     │
                     ▼
┌────────────────────────────────────────┐
│  Layer 4: Optimistic Concurrency       │
│  check_and_bump(entity_key, expected)  │
│  → VersionConflictError (HTTP 409)     │
└────────────────────┬───────────────────┘
                     │
                     ▼
┌────────────────────────────────────────┐
│  Layer 5: Audit Logging                │
│  AuditRecord → output/audit_log.jsonl  │
│  Append-only, no secrets               │
└────────────────────────────────────────┘
```

---

## Authentication

**Location:** `src/gemini_connector/auth.py`

### Authentication Modes

| Mode | Env Var | Description | Use Case |
|------|---------|-------------|----------|
| `static` | `AUTH_MODE=static` | Single shared token | Default; CI/CD; hackathon demo |
| `jwt` | `AUTH_MODE=jwt` | HS256 JWT validation | Enterprise integration |
| `dev` | `AUTH_MODE=dev` | No validation; ADMIN | Local development only |

### Static Token Mode

```bash
AUTH_MODE=static
CONNECTOR_API_TOKEN=your-secret-token-here
CONNECTOR_ROLES=REVIEWER,RULE_ADMIN
```

Client sends: `Authorization: Bearer your-secret-token-here`

### JWT Mode

```bash
AUTH_MODE=jwt
JWT_SECRET=your-hmac-secret
JWT_ISSUER=https://your-idp.company.com
JWT_AUDIENCE=migration-validator
JWT_ALGORITHM=HS256
```

JWT claims extracted:
- `sub` → `user_id`
- `email` → `email`
- `roles` → `List[str]` (maps to Role enum)
- `allowed_tables` → per-user table restriction list (optional)
- `permissions` → explicit permission override list (optional)

### Authentication Error Codes

| Code | Meaning |
|------|---------|
| `MISSING_TOKEN` | No Authorization header |
| `MALFORMED_TOKEN` | Not `Bearer <token>` format |
| `TOKEN_EXPIRED` | JWT `exp` claim in the past |
| `INVALID_ISSUER` | JWT `iss` does not match configured issuer |
| `INVALID_AUDIENCE` | JWT `aud` does not match configured audience |
| `INVALID_SIGNATURE` | HMAC signature verification failed |

---

## Authorization (RBAC)

**Location:** `src/gemini_connector/authz.py`

### Permission Enum (15 permissions)

| Permission | Description |
|------------|-------------|
| `SCHEMA_READ` | View source and target schemas |
| `MAPPING_READ` | View column mappings |
| `MAPPING_APPROVE` | Approve column mappings |
| `MAPPING_MODIFY` | Modify column mappings |
| `MAPPING_REJECT` | Reject column mappings |
| `RULE_READ` | View rules |
| `RULE_CREATE` | Create draft rules |
| `RULE_APPROVE` | Approve rules (not yet active) |
| `RULE_ACTIVATE` | Activate approved rules |
| `EXCLUSION_READ` | View exclusion configuration |
| `EXCLUSION_MODIFY` | Add/remove exclusions |
| `PLAN_READ` | View validation plans |
| `PLAN_APPROVE` | Approve validation plans |
| `VALIDATION_READ` | View validation results |
| `VALIDATION_EXECUTE` | Execute validation queries |

### Role → Permission Matrix

| Permission | VIEWER | REVIEWER | RULE_ADMIN | VAL_OPERATOR | ADMIN |
|------------|--------|----------|------------|--------------|-------|
| SCHEMA_READ | ✓ | ✓ | ✓ | ✓ | ✓ |
| MAPPING_READ | ✓ | ✓ | ✓ | ✓ | ✓ |
| RULE_READ | ✓ | ✓ | ✓ | ✓ | ✓ |
| PLAN_READ | ✓ | ✓ | ✓ | ✓ | ✓ |
| VALIDATION_READ | ✓ | ✓ | ✓ | ✓ | ✓ |
| EXCLUSION_READ | ✓ | ✓ | ✓ | ✓ | ✓ |
| MAPPING_APPROVE | | ✓ | ✓ | ✓ | ✓ |
| MAPPING_MODIFY | | ✓ | ✓ | ✓ | ✓ |
| MAPPING_REJECT | | ✓ | ✓ | ✓ | ✓ |
| PLAN_APPROVE | | ✓ | ✓ | ✓ | ✓ |
| EXCLUSION_MODIFY | | ✓ | ✓ | | ✓ |
| VALIDATION_EXECUTE | | | | ✓ | ✓ |
| RULE_CREATE | | | ✓ | | ✓ |
| RULE_APPROVE | | | ✓ | | ✓ |
| RULE_ACTIVATE | | | ✓ | | ✓ |

### Resource-Level Allowlists

Additional env-var restrictions applied after role check:

```bash
AUTHZ_SOURCE_ALLOWLIST=postgresql,athena    # comma-separated; empty = all allowed
AUTHZ_DB_ALLOWLIST=fms,DevT5000            # comma-separated; empty = all allowed
AUTHZ_SCHEMA_ALLOWLIST=public,dbo          # comma-separated; empty = all allowed
```

### Per-User Table Restriction

JWT `allowed_tables` claim restricts a user to specific tables:

```json
{
  "sub": "user-123",
  "email": "analyst@company.com",
  "roles": ["REVIEWER"],
  "allowed_tables": ["customer", "orders", "payments"]
}
```

`None` (claim absent) = all tables allowed.

---

## AI Self-Approval Guard

All write tools enforce a human actor requirement:

```python
# From tools.py — applied to all write tools
if not actor or actor.lower() in ("", "gemini_ai", "ai"):
    return _err("Action requires an authenticated human actor. "
                "Provide your email or user ID as the actor parameter.")
```

This ensures Gemini cannot approve its own recommendations. The actor must be a real human identifier (email, username) that is then recorded in the audit trail.

---

## Optimistic Concurrency Control

**Location:** `src/gemini_connector/version_store.py`

Prevents concurrent write conflicts on shared entities.

### Entity Version Keys

| Entity | Key Pattern |
|--------|-------------|
| Validation plan | `plan/<layer>/<table>` |
| Column mapping | `mapping/<table>/<source_column>` |
| Rule | `rule/<rule_id>` |
| Exclusion | `exclusion/<table>/<column>` |

### Write Protocol

```python
# Client must pass current expected version
approve_mapping(record_id="...", expected_version=3)

# Server-side check
version_store.check_and_bump("mapping/customer/email", expected=3)
# → if current == 3: bumps to 4, returns 4
# → if current != 3: raises VersionConflictError (HTTP 409)
```

### Concurrent Write Scenario

```
User A: reads mapping/customer/email → version=2
User B: reads mapping/customer/email → version=2

User A: approves with expected_version=2 → bumped to 3 ✓
User B: approves with expected_version=2 → VersionConflictError (409)
  → User B must re-read and re-submit with expected_version=3
```

---

## Audit Trail

**Location:** `src/gemini_connector/audit.py`

### AuditRecord Fields

| Field | Type | Description |
|-------|------|-------------|
| `audit_id` | UUID | Unique record identifier |
| `action` | str | e.g., `approve_mapping`, `reject_mapping` |
| `entity_type` | str | `mapping`, `rule`, `plan` |
| `entity_id` | str | Stable identifier for the entity |
| `actor` | str | Human email or username |
| `user_id` | str | From auth token `sub` claim |
| `timestamp` | ISO-8601 UTC | Exact time of action |
| `request_id` | UUID | Links to tool call log |
| `previous` | dict | State before the action |
| `new_state` | dict | State after the action |
| `reason` | str | Human-provided justification |
| `plan_version` | int | Version at time of action |
| `source_system` | str | Affected source system |
| `table` | str | Affected table name |
| `column` | str | Affected column name |
| `rule_id` | str | Affected rule ID |
| `metadata` | dict | Additional context |

### Security Properties

- **Append-only:** No record is ever modified or deleted
- **No secrets:** `raw_claims`, passwords, and API keys are never logged
- **No response payloads:** Full data results are not logged (only identifiers)
- **Linked to request:** `request_id` correlates with tool call observability log

---

## Credential Management

| Credential Type | Storage | Exposed to Gemini? |
|----------------|---------|-------------------|
| Database passwords | `.env` file (git-ignored) | Never |
| Snowflake password | `.env` file | Never |
| API keys (DIAL, Claude) | `.env` file | Never |
| Gemini API key | `.env` file | Never |
| Connector API token | `.env` file | Used to authenticate Gemini→connector; not forwarded to databases |
| JWT signing secret | `.env` file | Never |

The connector acts as a **secrets boundary**. Gemini only receives tool results — structured data with no connection strings, credentials, or raw configuration.

---

## Security Verification

Verify connector behavior through the FastAPI health endpoint, authenticated
tool requests, audit records, and the repository's available checks.

Keep credentials in `.env`; never include secrets in requests, logs, or documentation.

Security controls cover:
1. Governed approval workflow with audit trail
2. Authorization denial (VIEWER cannot activate rule)
3. Stale version / concurrent write conflict
4. Zero credential leakage in API responses
