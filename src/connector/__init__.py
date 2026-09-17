"""
Migration Intelligence Connector
===================================
Turns Migration Validator into an agentic connector by exposing purpose-built
tools that a chat agent (or any external caller, via the REST API in api.py)
can use to perform governed migration-validation workflows.

Architecture:
    Chat agent / external caller
          ↓
    Migration Intelligence Connector  (this package)
          ↓
    Migration Validator APIs / tools
          ↓
    PostgreSQL / MSSQL / Athena / Redshift  →  Snowflake  →  Validation Engine
          ↓
    Results  →  AI explanation (agent.py — EPAM DIAL today, direct Claude
    once a key is issued)

Design principle:
    - AI recommends; humans approve high-risk decisions
    - Every write action is audited
    - Migration Validator = governed execution and data-validation layer
"""

from .audit import AuditLogger, AuditRecord
from .approval_store import ApprovalStore, ApprovalRecord, ApprovalStatus
from .metrics import MetricsTracker

__all__ = [
    "AuditLogger",
    "AuditRecord",
    "ApprovalStore",
    "ApprovalRecord",
    "ApprovalStatus",
    "MetricsTracker",
]
