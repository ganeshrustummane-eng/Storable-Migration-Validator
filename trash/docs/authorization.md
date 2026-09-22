# Authorization

## RBAC Model

The connector uses a five-role RBAC model with 15 fine-grained permissions.

## Roles

| Role | Description | Typical User |
|------|-------------|-------------|
| `VIEWER` | Read-only access to all data | Stakeholders, auditors |
| `REVIEWER` | Can approve, modify, and reject mappings | Data analysts, migration engineers |
| `RULE_ADMIN` | Can create and activate rules | Senior data architects |
| `VALIDATION_OPERATOR` | Can execute validation queries | Automated pipelines, operators |
| `ADMIN` | Full access to all operations | Migration lead, platform admin |

## Permissions by Operation

| Tool / Operation | VIEWER | REVIEWER | RULE_ADMIN | VAL_OP | ADMIN |
|-----------------|--------|----------|------------|--------|-------|
| discover_connections | ✓ | ✓ | ✓ | ✓ | ✓ |
| list_databases/schemas/tables | ✓ | ✓ | ✓ | ✓ | ✓ |
| get_table_schema | ✓ | ✓ | ✓ | ✓ | ✓ |
| get_table_mapping | ✓ | ✓ | ✓ | ✓ | ✓ |
| get_column_mappings | ✓ | ✓ | ✓ | ✓ | ✓ |
| get_pending_reviews | ✓ | ✓ | ✓ | ✓ | ✓ |
| get_rule / get_applicable_rules | ✓ | ✓ | ✓ | ✓ | ✓ |
| get_validation_plan | ✓ | ✓ | ✓ | ✓ | ✓ |
| get_validation_result | ✓ | ✓ | ✓ | ✓ | ✓ |
| get_migration_summary | ✓ | ✓ | ✓ | ✓ | ✓ |
| get_coverage | ✓ | ✓ | ✓ | ✓ | ✓ |
| get_business_metrics | ✓ | ✓ | ✓ | ✓ | ✓ |
| approve_mapping | | ✓ | ✓ | ✓ | ✓ |
| reject_mapping | | ✓ | ✓ | ✓ | ✓ |
| modify_mapping | | ✓ | ✓ | ✓ | ✓ |
| approve_plan | | ✓ | ✓ | ✓ | ✓ |
| add/remove exclusions | | ✓ | ✓ | | ✓ |
| execute_validation | | | | ✓ | ✓ |
| create rule (draft) | | | ✓ | | ✓ |
| approve_rule | | | ✓ | | ✓ |
| activate rule | | | ✓ | | ✓ |

## Resource-Level Restrictions

Environment variables limit access to specific systems regardless of role:

```bash
# Comma-separated; empty string = all allowed
AUTHZ_SOURCE_ALLOWLIST=postgresql,athena
AUTHZ_DB_ALLOWLIST=fms,DevT5000
AUTHZ_SCHEMA_ALLOWLIST=public,dbo
```

A `REVIEWER` who passes role check but tries to access a non-allowlisted source receives HTTP 403.

## Per-User Table Restrictions (JWT)

The `allowed_tables` claim in the JWT restricts a user to specific tables:

```json
{
  "roles": ["REVIEWER"],
  "allowed_tables": ["customer", "orders"]
}
```

Absent claim = all tables allowed. Empty list = no tables allowed.

## Explicit Permission Override (JWT)

The `permissions` claim overrides role-derived permissions entirely when non-empty:

```json
{
  "roles": ["VIEWER"],
  "permissions": ["MAPPING_READ", "MAPPING_APPROVE"]
}
```

This user can approve mappings despite the VIEWER role.

## Setting Roles in Static Mode

```bash
CONNECTOR_ROLES=REVIEWER,RULE_ADMIN
```

All requests using the static token receive both roles (union of all permissions).
