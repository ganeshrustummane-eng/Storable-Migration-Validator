"""
Enterprise Audit Logger
========================
Every write action in the Migration Intelligence Connector is appended to an
immutable JSONL audit log.  The log is the source of truth for governance.

Audit schema (AuditRecord fields):
  audit_id       — UUID for this record
  timestamp      — ISO-8601 UTC
  user_id        — identity from token (sub claim)
  actor          — display name / email
  action         — upper-snake-case verb (APPROVE_MAPPING, REJECT_RULE, …)
  resource_type  — "mapping" | "rule" | "plan" | "exclusion" | "validation_run"
  resource_id    — entity primary key
  old_value      — serialized previous state (None for creation)
  new_value      — serialized new state
  reason         — human-provided justification
  plan_version   — entity version at time of write (for concurrency audit)
  request_id     — correlation ID from the HTTP request
  source_system  — source DB type (postgresql/mssql/athena)
  table          — table name (if applicable)
  column         — column name (if applicable)
  rule_id        — rule identifier (if applicable)

SECURITY INVARIANTS:
  - Secrets (passwords, API keys, connection strings) MUST NOT appear in any field.
  - raw_claims from JWT are never stored.
  - audit records are append-only; no record is ever modified or deleted.
  - Do not store response payloads that might contain sensitive data.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_AUDIT_PATH = Path(__file__).resolve().parents[2] / "output" / "audit_log.jsonl"


# ---------------------------------------------------------------------------
# AuditRecord — the canonical audit schema
# ---------------------------------------------------------------------------

@dataclass
class AuditRecord:
    """Immutable audit entry for one governed write action."""

    # Core identity fields
    action:        str            # e.g. "APPROVE_MAPPING", "REJECT_RULE"
    entity_type:   str            # "mapping" | "rule" | "plan" | "exclusion"
    entity_id:     str            # e.g. "events.active_flag"

    # Who
    actor:         str            # display name / email (from token)
    user_id:       str = ""       # subject from token

    # When / correlation
    audit_id:      str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:     str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    request_id:    str = ""       # HTTP request correlation ID

    # State diff — NEVER include credentials here
    previous:      Optional[Dict[str, Any]] = None
    new_state:     Optional[Dict[str, Any]] = None
    reason:        str = ""

    # Versioning
    plan_version:  Optional[int] = None   # entity version after this write

    # Resource context
    source_system: str = ""
    table:         str = ""
    column:        str = ""
    rule_id:       str = ""

    # Backward-compat aliases kept for existing callers
    run_id:        str = ""
    metadata:      Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AuditRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------

class AuditLogger:
    """
    Append-only JSONL audit log.

    Thread-safe for single-process use (each write is a single f.write call
    which the OS makes atomic at the syscall level on POSIX).
    For multi-process safety, use a database-backed logger in production.
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else _AUDIT_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, record: AuditRecord) -> None:
        """Append one audit record (never modifies existing records)."""
        try:
            line = json.dumps(record.to_dict(), ensure_ascii=False, default=str)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            # Never let audit failures silently swallow the application error.
            print(f"[AUDIT ERROR] Could not write audit record: {exc}")

    def recent(self, n: int = 50) -> List[AuditRecord]:
        """Return the N most recent audit records."""
        if not self._path.exists():
            return []
        records: List[AuditRecord] = []
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        records.append(AuditRecord.from_dict(d))
                    except Exception:
                        pass
        except Exception:
            pass
        return records[-n:]

    def for_entity(self, entity_type: str, entity_id: str) -> List[AuditRecord]:
        """All audit records for a specific entity (full history)."""
        return [
            r for r in self.recent(n=10_000)
            if r.entity_type == entity_type and r.entity_id == entity_id
        ]

    def for_actor(self, actor: str, n: int = 200) -> List[AuditRecord]:
        """Most recent N records by a specific actor."""
        all_r = self.recent(n=10_000)
        return [r for r in all_r if r.actor == actor][-n:]


# Module-level singleton
audit_logger = AuditLogger()
