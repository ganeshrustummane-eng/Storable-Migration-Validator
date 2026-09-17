"""
Enterprise Authorization Layer (RBAC + Fine-Grained Permissions)
=================================================================
Implements role-based and resource-level authorization for all
Migration Intelligence Connector operations.

RBAC hierarchy:
  VIEWER           — read-only access to schemas, mappings, rules, results
  REVIEWER         — VIEWER + approve/reject/modify mappings, add comments
  RULE_ADMIN       — REVIEWER + create/update/approve/activate rules
  VALIDATION_OPERATOR — REVIEWER + trigger validation, generate/execute SQL
  ADMIN            — all permissions

Fine-grained permissions (canonical names used in permission checks):
  schema.read
  mapping.read        mapping.approve  mapping.modify  mapping.reject
  rule.read           rule.create      rule.approve    rule.activate
  exclusion.read      exclusion.modify
  plan.read           plan.approve
  validation.execute  validation.read

Resource-level checks (performed on top of permission checks):
  - source_system: allowed source systems for this user
  - database:      allowed databases
  - schema:        allowed schemas (None = all allowed)
  - table:         allowed tables  (None = all allowed)
  - operation:     must match the permission being checked

Configuration (via env vars):
  AUTHZ_SOURCE_ALLOWLIST  — comma-separated; empty = all allowed
  AUTHZ_DB_ALLOWLIST      — comma-separated; empty = all allowed
  AUTHZ_SCHEMA_ALLOWLIST  — comma-separated; empty = all allowed
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Set

from connector.auth import AuthResult


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

class Permission(str, Enum):
    SCHEMA_READ         = "schema.read"
    MAPPING_READ        = "mapping.read"
    MAPPING_APPROVE     = "mapping.approve"
    MAPPING_MODIFY      = "mapping.modify"
    MAPPING_REJECT      = "mapping.reject"
    RULE_READ           = "rule.read"
    RULE_CREATE         = "rule.create"
    RULE_APPROVE        = "rule.approve"
    RULE_ACTIVATE       = "rule.activate"
    EXCLUSION_READ      = "exclusion.read"
    EXCLUSION_MODIFY    = "exclusion.modify"
    PLAN_READ           = "plan.read"
    PLAN_APPROVE        = "plan.approve"
    VALIDATION_READ     = "validation.read"
    VALIDATION_EXECUTE  = "validation.execute"


# ---------------------------------------------------------------------------
# Roles → Permission sets
# ---------------------------------------------------------------------------

class Role(str, Enum):
    VIEWER              = "VIEWER"
    REVIEWER            = "REVIEWER"
    RULE_ADMIN          = "RULE_ADMIN"
    VALIDATION_OPERATOR = "VALIDATION_OPERATOR"
    ADMIN               = "ADMIN"


_READ_PERMS: FrozenSet[Permission] = frozenset({
    Permission.SCHEMA_READ,
    Permission.MAPPING_READ,
    Permission.RULE_READ,
    Permission.EXCLUSION_READ,
    Permission.PLAN_READ,
    Permission.VALIDATION_READ,
})

ROLE_PERMISSIONS: Dict[str, FrozenSet[Permission]] = {
    Role.VIEWER.value: _READ_PERMS,

    Role.REVIEWER.value: _READ_PERMS | frozenset({
        Permission.MAPPING_APPROVE,
        Permission.MAPPING_MODIFY,
        Permission.MAPPING_REJECT,
        Permission.EXCLUSION_MODIFY,
        Permission.PLAN_APPROVE,
    }),

    Role.RULE_ADMIN.value: _READ_PERMS | frozenset({
        Permission.MAPPING_APPROVE,
        Permission.MAPPING_MODIFY,
        Permission.MAPPING_REJECT,
        Permission.RULE_CREATE,
        Permission.RULE_APPROVE,
        Permission.RULE_ACTIVATE,
        Permission.EXCLUSION_MODIFY,
        Permission.PLAN_APPROVE,
    }),

    Role.VALIDATION_OPERATOR.value: _READ_PERMS | frozenset({
        Permission.MAPPING_APPROVE,
        Permission.MAPPING_MODIFY,
        Permission.MAPPING_REJECT,
        Permission.PLAN_APPROVE,
        Permission.VALIDATION_EXECUTE,
    }),

    Role.ADMIN.value: frozenset(Permission),  # all permissions
}


# ---------------------------------------------------------------------------
# Authorization exception
# ---------------------------------------------------------------------------

class AuthorizationError(Exception):
    """Caller lacks permission for the requested operation."""
    def __init__(self, message: str, code: str = "AUTHORIZATION_ERROR"):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Resource-level policy (configurable via env)
# ---------------------------------------------------------------------------

def _allowed_set(env_key: str) -> Optional[Set[str]]:
    """Return a set of allowed values from env, or None (= all allowed)."""
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return None
    return {v.strip().lower() for v in raw.split(",") if v.strip()}


_SOURCE_ALLOWLIST = _allowed_set("AUTHZ_SOURCE_ALLOWLIST")
_DB_ALLOWLIST     = _allowed_set("AUTHZ_DB_ALLOWLIST")
_SCHEMA_ALLOWLIST = _allowed_set("AUTHZ_SCHEMA_ALLOWLIST")


def check_resource_access(
    auth: AuthResult,
    *,
    source_system: Optional[str] = None,
    database: Optional[str]      = None,
    schema: Optional[str]        = None,
    table: Optional[str]         = None,
) -> None:
    """
    Enforce resource-level access policy.

    System-wide allowlists (env vars) constrain what every user may touch,
    regardless of their role.  Per-user resource restrictions (future: from
    an identity provider's custom claims) can be layered on top.

    Raises AuthorizationError if any boundary is violated.
    """
    if source_system and _SOURCE_ALLOWLIST is not None:
        if source_system.lower() not in _SOURCE_ALLOWLIST:
            raise AuthorizationError(
                f"Access to source system '{source_system}' is not permitted.",
            )

    if database and _DB_ALLOWLIST is not None:
        if database.lower() not in _DB_ALLOWLIST:
            raise AuthorizationError(
                f"Access to database '{database}' is not permitted.",
            )

    if schema and _SCHEMA_ALLOWLIST is not None:
        if schema.lower() not in _SCHEMA_ALLOWLIST:
            raise AuthorizationError(
                f"Access to schema '{schema}' is not permitted.",
            )

    # Per-user resource restrictions from token claims
    allowed_tables: Optional[List[str]] = auth.raw_claims.get("allowed_tables")
    if allowed_tables is not None and table is not None:
        if table.lower() not in {t.lower() for t in allowed_tables}:
            raise AuthorizationError(
                f"Access to table '{table}' is not permitted for this account.",
            )


# ---------------------------------------------------------------------------
# Permission resolver
# ---------------------------------------------------------------------------

def _resolve_permissions(auth: AuthResult) -> Set[str]:
    """
    Derive the effective permission set for an authenticated identity.

    Explicit token permissions override role-derived permissions only if
    the token carries a non-empty list.  Otherwise roles are expanded.
    """
    if auth.permissions:
        # Token carries explicit permissions — use them directly
        return set(auth.permissions)

    effective: Set[str] = set()
    for role in auth.roles:
        perms = ROLE_PERMISSIONS.get(role, frozenset())
        effective.update(p.value for p in perms)
    return effective


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def require_permission(
    auth: AuthResult,
    permission: Permission,
    *,
    source_system: Optional[str] = None,
    database: Optional[str]      = None,
    schema: Optional[str]        = None,
    table: Optional[str]         = None,
) -> None:
    """
    Assert that the authenticated user holds `permission` and is allowed
    to access the specified resource.

    Raises AuthorizationError with a descriptive message on failure.
    """
    effective = _resolve_permissions(auth)

    if permission.value not in effective:
        roles_str = ", ".join(auth.roles) or "none"
        raise AuthorizationError(
            f"Authorization denied: '{permission.value}' is required. "
            f"User '{auth.actor}' has roles [{roles_str}]. "
            f"Assign the appropriate role (e.g. REVIEWER, RULE_ADMIN) to proceed.",
        )

    check_resource_access(
        auth,
        source_system=source_system,
        database=database,
        schema=schema,
        table=table,
    )


def has_permission(auth: AuthResult, permission: Permission) -> bool:
    """Non-raising check (use for conditional UI hints in API responses)."""
    return permission.value in _resolve_permissions(auth)


def effective_permissions(auth: AuthResult) -> List[str]:
    """Return sorted list of all permissions the identity holds."""
    return sorted(_resolve_permissions(auth))
