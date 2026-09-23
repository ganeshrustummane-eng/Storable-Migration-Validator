# Environment Variables Reference

All configuration is via environment variables, loaded from `.env` by `python-dotenv`.

> **Security:** `.env` is git-ignored. Never commit credentials.

---

## AI Backends

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DIAL_API_KEY` | For AI mapping | — | EPAM DIAL proxy API key |
| `DIAL_API_BASE` | No | `https://ai-proxy.lab.epam.com` | DIAL endpoint |
| `DIAL_API_VERSION` | No | `2025-04-01-preview` | DIAL API version |
| `DIAL_MODEL` | No | `gpt-4o` | DIAL model for mapping AI |
| `CLAUDE_API_KEY` | Fallback | — | Anthropic Claude direct key |
| `CLAUDE_MODEL` | No | `claude-3-5-sonnet` | Claude model name |

---

## Connector Authentication

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_MODE` | No | `static` | `static`, `jwt`, or `dev` |
| `CONNECTOR_API_TOKEN` | For static mode | — | Bearer token for API access |
| `CONNECTOR_ROLES` | No | `ADMIN` | Comma-separated roles for static token |
| `JWT_SECRET` | For jwt mode | — | HMAC signing secret |
| `JWT_ISSUER` | For jwt mode | — | Expected `iss` claim |
| `JWT_AUDIENCE` | For jwt mode | — | Expected `aud` claim |
| `JWT_ALGORITHM` | No | `HS256` | JWT signing algorithm |

---

## Authorization

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTHZ_SOURCE_ALLOWLIST` | No | (all) | Comma-sep source types allowed |
| `AUTHZ_DB_ALLOWLIST` | No | (all) | Comma-sep database names allowed |
| `AUTHZ_SCHEMA_ALLOWLIST` | No | (all) | Comma-sep schema names allowed |
| `CORS_ORIGINS` | No | `*` | Comma-sep allowed CORS origins |

---

## Source Connections (Multi-Connection Registry)

For each source connection N (1, 2, 3, ...):

| Variable | Required | Description |
|----------|----------|-------------|
| `SRC_N_DB_TYPE` | Yes | `postgresql`, `mssql`, or `athena` |
| `SRC_N_HOST` | Yes | Database host |
| `SRC_N_PORT` | No | Database port (default varies by type) |
| `SRC_N_USERNAME` | Yes | Username or AWS access key |
| `SRC_N_PASSWORD` | Yes | Password or AWS secret key |
| `SRC_N_AUTH` | No | Auth method (e.g., `ActiveDirectory`) |

**Non-secret metadata** (database, schema) goes in `config/database_registry.yaml`.

---

## Active Source Connection (Runtime)

Set by CLI/pipeline during validation run:

| Variable | Description |
|----------|-------------|
| `SOURCE_HOST` | Active source host |
| `SOURCE_PORT` | Active source port |
| `SOURCE_DATABASE` | Active source database |
| `SOURCE_SCHEMA` | Active source schema |
| `SOURCE_USERNAME` | Active source username |
| `SOURCE_PASSWORD` | Active source password |
| `SOURCE_TYPE` | Active source DB type |
| `SOURCE_AUTH` | Active source auth method |

---

## Snowflake Target

| Variable | Required | Description |
|----------|----------|-------------|
| `SNOWFLAKE_ACCOUNT` | Yes | Account URL (include `.snowflakecomputing.com`) |
| `SNOWFLAKE_USERNAME` | Yes | Snowflake username |
| `SNOWFLAKE_PASSWORD` | Yes | Snowflake password |
| `SNOWFLAKE_WAREHOUSE` | Yes | Compute warehouse name |
| `SNOWFLAKE_ROLE` | Yes | Role to use |
| `SNOWFLAKE_DATABASE` | Runtime | Set dynamically per table |
| `SNOWFLAKE_SCHEMA` | Runtime | Set dynamically per table |

---

## AWS Athena

| Variable | Required | Description |
|----------|----------|-------------|
| `ATHENA_S3_OUTPUT` | For Athena | S3 path for query results |
| `ATHENA_REGION` | For Athena | AWS region |

---

## Confidence Thresholds

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIDENCE_AUTO_ACCEPT` | `0.95` | Above this: auto-accept mapping |
| `CONFIDENCE_REVIEW` | `0.75` | Below this: mandatory human review |

---

## JIRA Integration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `JIRA_URL` | No | — | JIRA Cloud instance URL (e.g., `https://company.atlassian.net`) |
| `JIRA_EMAIL` | No | — | JIRA account email |
| `JIRA_API_TOKEN` | No | — | API token from [JIRA Account Settings](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `JIRA_PROJECT_KEY` | No | — | Project key for ticket creation (e.g., `MIG`, `DW`) |
| `JIRA_ISSUE_TYPE` | No | `Task` | Issue type to create (Task, Bug, Story, etc.) |

**When enabled:**
- Automatically creates JIRA tickets for rejected mappings
- Creates tickets for validation failures exceeding thresholds
- Tracks all issues with full traceability
- Links tickets to audit log entries

**Setup:**
1. Generate API token: https://id.atlassian.com/manage-profile/security/api-tokens
2. Add variables to `.env` file
3. Test: `python verify_jira_config.py --create-test-ticket`
4. Full docs: [jira-integration.md](jira-integration.md)
