# Approval Model

## ApprovalStore

The `ApprovalStore` (`src/gemini_connector/approval_store.py`) is the persistence layer for all human review decisions.

**Storage format:** `output/approval_store.jsonl` — append-only JSONL where the latest line for each `id` is the canonical state.

---

## ApprovalRecord States

```
PENDING
  │
  ├──► AUTO_ACCEPTED  (confidence ≥ CONFIDENCE_AUTO_ACCEPT threshold)
  │
  ├──► APPROVED       (human reviewed and confirmed)
  │
  ├──► MODIFIED       (human changed target column or transformation rule)
  │
  ├──► REJECTED       (human rejected — column will be excluded from validation)
  │
  └──► EXCLUDED       (column excluded via exclusion rule — not mapped)
```

---

## ApprovalRecord Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID string | Stable record identifier |
| `entity_type` | str | Always `"mapping"` currently |
| `table` | str | Source table name |
| `source_column` | str | Source column name |
| `target_column` | str | AI-proposed target column |
| `confidence` | float | 0.0–1.0 confidence score |
| `match_method` | str | How match was made |
| `transformation_rule` | str | Proposed transformation |
| `ai_recommendation` | str | AI explanation text |
| `reason` | str | Human justification (on approve) |
| `status` | ApprovalStatus | Current state |
| `decided_by` | str | Actor email/username |
| `decided_at` | ISO-8601 | Decision timestamp |
| `modified_target` | str | New target column (if MODIFIED) |
| `modified_rule` | str | New transformation rule (if MODIFIED) |
| `rejection_reason` | str | Why rejected (if REJECTED) |
| `metadata` | dict | Additional context |
| `created_at` | ISO-8601 | When record was created |

---

## Aggregate Stats

```python
from src.gemini_connector.approval_store import approval_store

stats = approval_store.stats()
# {
#   "total": 42,
#   "pending": 5,
#   "approved": 30,
#   "rejected": 2,
#   "modified": 3,
#   "auto_accepted": 36,
#   "excluded": 2
# }
```

---

## Automation Rate

The automation rate measures what fraction of mappings required no human intervention:

```
automation_rate = (auto_accepted) / (total - excluded) × 100
```

High automation rates (>90%) indicate strong schema naming consistency between source and target.

---

## Configuration

```bash
# Confidence thresholds (0.0–1.0)
CONFIDENCE_AUTO_ACCEPT=0.95   # above this: auto-accept
CONFIDENCE_REVIEW=0.75        # below this: mandatory review
```

Mappings between 0.75 and 0.95 are queued for standard review — the reviewer may choose to approve without detailed investigation for straightforward name differences.
