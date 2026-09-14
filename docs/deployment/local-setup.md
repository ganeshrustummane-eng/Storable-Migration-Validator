# Local Setup Guide

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.10+ | Required |
| pip | Latest | Required |
| Git | Any | Required |
| PostgreSQL client | Any | Optional (for local testing) |
| MSSQL ODBC driver | 17 or 18 | Required for MSSQL sources |
| AWS credentials | Any | Required for Athena |

---

## Step 1: Clone and Install

```bash
git clone <repository-url>
cd Migration-validator
pip install -r requirements.txt
```

---

## Step 2: Configure Environment

Copy the example environment file and populate it:

```bash
cp .env.example .env
```

Edit `.env` with your values. See [`docs/deployment/environment.md`](environment.md) for all variables.

**Minimum required for Gemini connector demo:**

```bash
# --- Gemini ---
GOOGLE_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-default-model

# --- Connector authentication ---
AUTH_MODE=static
CONNECTOR_API_TOKEN=your-connector-token
CONNECTOR_ROLES=ADMIN

# --- Source: PostgreSQL ---
SRC_1_DB_TYPE=postgresql
SRC_1_HOST=localhost
SRC_1_PORT=5432
SRC_1_USERNAME=postgres
SRC_1_PASSWORD=your-password

# --- Target: Snowflake ---
SNOWFLAKE_ACCOUNT=your-account.snowflakecomputing.com
SNOWFLAKE_USERNAME=your-user
SNOWFLAKE_PASSWORD=your-password
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_ROLE=SYSADMIN
```

---

## Step 3: Configure database_registry.yaml

The registry maps connection names to database/schema (non-secret metadata):

```yaml
# config/database_registry.yaml
SRC_1:
  database: your_database
  schema: public

SNOWFLAKE:
  database: DEV_YOUR_DB_BRONZE
  schema: YOUR_SCHEMA
```

---

## Step 4: Start the Connector Server

```bash
python start_connector.py
```

Expected output:
```
Migration Validator Connector
================================
  URL:    http://0.0.0.0:8001
  Docs:   http://localhost:8001/docs
  Health: http://localhost:8001/health
  Tools:  http://localhost:8001/tools
  Auth:   static mode
================================
```

Verify:
```bash
curl http://localhost:8001/health
# → {"status": "ok", "tool_count": 24, "auth_mode": "static"}
```

---

## Step 5: Start the Web UI (Optional)

```bash
streamlit run webapp/app.py
```

Opens at `http://localhost:8501`

---

## Step 6: Run the CLI (Optional)

```bash
python -m src.validate_cli --help
```

Useful commands:
```
python -m src.validate_cli setup
python -m src.validate_cli generate --source-table events --sf-table EVENTS
python -m src.validate_cli multi --tables events,users,orders
python -m src.validate_cli batch --config tables.yaml
python -m src.validate_cli rules
python -m src.validate_cli list-models
python -m src.validate_cli add-rule
```

---

## Step 7: Run Available Checks

```bash
python -m py_compile webapp/app.py
python verify_jira_config.py
```

The Streamlit UI provides live connection selection, YAML generation, custom SQL
validation, history, rules, exclusions, review, Jira, and AI usage views.
Batch workflow supports multi-schema JOIN prompts and Excel report input.

---

## MSSQL ODBC Driver Installation

### Windows
```
Download "ODBC Driver 18 for SQL Server" from Microsoft
Run installer
```

### Ubuntu/Debian
```bash
curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add -
curl https://packages.microsoft.com/config/ubuntu/22.04/prod.list > /etc/apt/sources.list.d/mssql-release.list
apt-get update
ACCEPT_EULA=Y apt-get install -y msodbcsql18
```

---

## Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| `ModuleNotFoundError: pyodbc` | ODBC driver not installed | Install MSSQL ODBC driver 17/18 |
| `SnowflakeLoginError` | Wrong account/credentials | Verify SNOWFLAKE_ACCOUNT includes `.snowflakecomputing.com` |
| `AuthenticationError: MISSING_TOKEN` | No bearer token in request | Add `Authorization: Bearer <CONNECTOR_API_TOKEN>` header |
| `PORT 8001 already in use` | Another process on port | `python start_connector.py --port 8002` |
| Gemini offline mode | No GOOGLE_API_KEY | Set `GOOGLE_API_KEY` in `.env` for full functionality |
