# Human-in-the-Loop Review Workflow

## Design Principle

AI recommendations are proposals. Humans decide. The Migration Validator enforces this by:

1. Scoring every column mapping with a confidence value (0.0–1.0)
2. Routing low-confidence mappings to a human review queue
3. Requiring a named human actor for every write-back action
4. Blocking AI self-approval at the tool level
5. Recording every decision in an immutable audit trail

---

## Confidence Thresholds

| Confidence Range | Action | Human Required? |
|-----------------|--------|----------------|
| ≥ 0.95 | `AUTO_ACCEPTED` | No |
| 0.75 – 0.94 | `PENDING` (standard review) | Yes |
| < 0.75 | `PENDING` (mandatory review) | Yes |

Thresholds are configurable:

```bash
CONFIDENCE_AUTO_ACCEPT=0.95   # default
CONFIDENCE_REVIEW=0.75        # default
```

---

## Review Interfaces

### 1. Streamlit Web UI

Tab: **Review & Approve** in `webapp/app.py`

Features:
- Metrics row: Total / Pending / Approved / Modified / Rejected
- Per-table confidence color coding (green ≥95%, orange ≥75%, red <75%)
- Radio selection: Approve / Modify / Reject per mapping
- Plan approval panel with exclusion summary
- Audit history table (last 30 actions)
- Actor identity input (required for all actions)

### 2. Gemini Chat Interface

```
User: "Show me all mappings needing review."
→ Gemini calls: get_pending_reviews()
→ Returns: list of pending ApprovalRecords

User: "Approve the cust_id mapping. I've confirmed it's CUSTOMER_ID."
→ Gemini calls: approve_mapping(record_id=..., actor="jane@corp.com", reason="Confirmed")
→ Returns: {status: "approved", new_version: 1}
```

### 3. REST API

```bash
# List pending reviews
GET /pending

# Approve a mapping
POST /approve/mapping/{record_id}
Authorization: Bearer <token>
{
  "reason": "Confirmed via source DDL",
  "expected_version": 0
}

# Reject a mapping
POST /reject/mapping/{record_id}
Authorization: Bearer <token>
{
  "reason": "Wrong target column — email should map to EMAIL_ADDRESS",
  "expected_version": 0
}

# Modify a mapping
POST /modify/mapping/{record_id}
Authorization: Bearer <token>
{
  "new_target_column": "EMAIL_ADDRESS",
  "new_transformation_rule": "text",
  "reason": "Corrected target column",
  "expected_version": 0
}
```

---

## Full Review Flow

```
ValidationPipeline generates CanonicalValidationPlan
         │
         ├── confidence ≥ 0.95
         │   └── AUTO_ACCEPTED → no human action needed
         │
         └── confidence < 0.95
             └── ApprovalRecord{status: PENDING} written to approval_store.jsonl
                      │
             ┌────────┴─────────┐
             │                  │
         Via Web UI         Via Gemini / API
             │                  │
             ▼                  ▼
     Human reviews mapping details:
       - source_column, target_column
       - confidence score
       - AI recommendation text
       - match_method used
             │
     ┌───────┼───────┐
     │       │       │
  Approve  Modify  Reject
     │       │       │
     ▼       ▼       ▼
ApprovalRecord updated with:
  - status: APPROVED / MODIFIED / REJECTED
  - decided_by: actor
  - decided_at: timestamp
  - reason / rejection_reason / modified_target
  - new version number (VersionStore bumped)
             │
  AuditRecord appended to audit_log.jsonl
             │
  CanonicalValidationPlan updated with approved mappings
             │
  SQL generation proceeds with approved plan
```

---

## Actor Requirement

Every write action requires an `actor` parameter — the human's email or username.

The system blocks these actor values:
- Empty string `""`
- `"gemini_ai"`
- `"ai"`

Any other string is accepted as a valid actor. The actor is recorded verbatim in the AuditRecord and ApprovalRecord.

---

## Expected Version (Concurrency)

Every write action requires `expected_version` — the version the client believes is current.

- If the server's version matches: action proceeds, version bumps
- If the server's version differs: HTTP 409 `VERSION_CONFLICT` returned
- Client must re-read and re-submit with the correct version

This prevents two reviewers from simultaneously approving the same mapping with conflicting decisions.

---

## Plan-Level Approval

After all column mappings in a plan are reviewed, the plan itself requires approval before validation can execute:

```bash
POST /approve/plan/customer
{ "actor": "jane@corp.com", "expected_version": 2 }
```

Plan approval requires `PLAN_APPROVE` permission. After approval, `execute_validation` can proceed.
