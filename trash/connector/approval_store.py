"""
Approval Store
==============
Persists human approval decisions for:
  - Column mappings (approve / reject / modify)
  - Rules (approve / reject)
  - Validation plans (approve)
  - Exclusions (approve / remove)

Invariant: AI recommends. Human approves. Nothing is silently auto-approved
beyond the confidence thresholds declared in CONFIDENCE_POLICY.

CONFIDENCE_POLICY (configurable via env vars):
  >= AUTO_ACCEPT_THRESHOLD  → auto accepted (default 0.95)
  >= REVIEW_THRESHOLD       → AI-assisted / human review required (default 0.75)
  <  REVIEW_THRESHOLD       → unmatched / mandatory review
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

_STORE_PATH = Path(__file__).resolve().parents[2] / "output" / "approval_store.jsonl"

AUTO_ACCEPT_THRESHOLD = float(os.getenv("CONFIDENCE_AUTO_ACCEPT", "0.95"))
REVIEW_THRESHOLD      = float(os.getenv("CONFIDENCE_REVIEW", "0.75"))


class ApprovalStatus(str, Enum):
    PENDING        = "pending"
    APPROVED       = "approved"
    REJECTED       = "rejected"
    MODIFIED       = "modified"
    AUTO_ACCEPTED  = "auto_accepted"
    EXCLUDED       = "excluded"


@dataclass
class ApprovalRecord:
    """One pending or decided approval item."""
    id:           str           # unique key: "<table>.<source_col>" or "<rule_id>"
    entity_type:  str           # "mapping" | "rule" | "plan" | "exclusion"
    table:        str = ""
    source_column: str = ""
    target_column: str = ""
    confidence:   float = 0.0
    match_method: str = ""
    transformation_rule: str = ""
    ai_recommendation: str = ""
    reason:       str = ""
    status:       str = ApprovalStatus.PENDING.value
    decided_by:   str = ""
    decided_at:   str = ""
    modified_target: Optional[str] = None
    modified_rule:   Optional[str] = None
    rejection_reason: str = ""
    metadata:     Dict[str, Any] = field(default_factory=dict)
    created_at:   str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ApprovalRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class ApprovalStore:
    """
    Read/write store for approval decisions.

    Persistence: JSONL file — each line is one record (latest per ID wins).
    This means records can be updated by appending a new line with the same ID.
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else _STORE_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # -- read ----------------------------------------------------------------

    def _load_all(self) -> Dict[str, ApprovalRecord]:
        """Load all records. Latest line per ID wins (update-by-append)."""
        records: Dict[str, ApprovalRecord] = {}
        if not self._path.exists():
            return records
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            d = json.loads(line)
                            records[d["id"]] = ApprovalRecord.from_dict(d)
                        except Exception:
                            pass
        except Exception:
            pass
        return records

    def get(self, record_id: str) -> Optional[ApprovalRecord]:
        return self._load_all().get(record_id)

    def pending(self) -> List[ApprovalRecord]:
        """All records awaiting human decision."""
        return [
            r for r in self._load_all().values()
            if r.status == ApprovalStatus.PENDING.value
        ]

    def by_table(self, table: str) -> List[ApprovalRecord]:
        return [r for r in self._load_all().values() if r.table == table]

    def by_status(self, status: str) -> List[ApprovalRecord]:
        return [r for r in self._load_all().values() if r.status == status]

    # -- write ---------------------------------------------------------------

    def upsert(self, record: ApprovalRecord) -> None:
        """Append record (latest per ID wins)."""
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except Exception as exc:
            print(f"[APPROVAL STORE ERROR] {exc}")

    def approve(self, record_id: str, actor: str, reason: str = "") -> Optional[ApprovalRecord]:
        r = self.get(record_id)
        if r is None:
            return None
        r.status = ApprovalStatus.APPROVED.value
        r.decided_by = actor
        r.decided_at = datetime.now(timezone.utc).isoformat()
        r.reason = reason
        self.upsert(r)
        return r

    def reject(self, record_id: str, actor: str, reason: str = "") -> Optional[ApprovalRecord]:
        r = self.get(record_id)
        if r is None:
            return None
        r.status = ApprovalStatus.REJECTED.value
        r.decided_by = actor
        r.decided_at = datetime.now(timezone.utc).isoformat()
        r.rejection_reason = reason
        self.upsert(r)
        return r

    def modify(
        self, record_id: str, actor: str,
        new_target: Optional[str] = None,
        new_rule: Optional[str] = None,
        reason: str = "",
    ) -> Optional[ApprovalRecord]:
        r = self.get(record_id)
        if r is None:
            return None
        r.status = ApprovalStatus.MODIFIED.value
        r.decided_by = actor
        r.decided_at = datetime.now(timezone.utc).isoformat()
        if new_target:
            r.modified_target = new_target
        if new_rule:
            r.modified_rule = new_rule
        r.reason = reason
        self.upsert(r)
        return r

    def stats(self) -> Dict[str, int]:
        all_records = list(self._load_all().values())
        result = {s.value: 0 for s in ApprovalStatus}
        for r in all_records:
            result[r.status] = result.get(r.status, 0) + 1
        result["total"] = len(all_records)
        return result


# Module-level singleton
approval_store = ApprovalStore()
