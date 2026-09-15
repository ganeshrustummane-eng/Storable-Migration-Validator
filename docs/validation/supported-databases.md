# Supported Databases

## Source Systems

### PostgreSQL

**Driver:** `psycopg2-binary`  
**Status:** Implemented

| Feature | Support |
|---------|---------|
| Schema extraction | ✓ |
| Primary key discovery | ✓ |
| Column type mapping | ✓ |
| hstore columns | ✓ (cast to text) |
| uuid columns | ✓ (cast to text) |
| Array columns | Prototype |
| Count validation SQL | ✓ |
| Data validation SQL | ✓ |

**Connection config:**
```bash
SRC_1_DB_TYPE=postgresql
SRC_1_HOST=your-host
SRC_1_PORT=5432
SRC_1_USERNAME=your-user
SRC_1_PASSWORD=your-password
# In database_registry.yaml:
# SRC_1: {database: fms, schema: public}
```

---

### Microsoft SQL Server (MSSQL)

**Driver:** `pyodbc`  
**Status:** Implemented

| Feature | Support |
|---------|---------|
| Schema extraction | ✓ |
| Primary key discovery | ✓ |
| Column type mapping | ✓ |
| rowversion (UTS) columns | ✓ (excluded by rule) |
| Count validation SQL | ✓ |
| Data validation SQL | ✓ |

**Connection config:**
```bash
SRC_2_DB_TYPE=mssql
SRC_2_HOST=your-host
SRC_2_PORT=1433
SRC_2_USERNAME=your-user
SRC_2_PASSWORD=your-password
# In database_registry.yaml:
# SRC_2: {database: DevT5000, schema: dbo}
```

---

### AWS Athena

**Driver:** `boto3`  
**Status:** Implemented

| Feature | Support |
|---------|---------|
| Schema extraction | ✓ |
| Primary key discovery | Limited (Athena has no PK constraint) |
| S3 query output | ✓ |
| Count validation SQL | ✓ |
| Data validation SQL | ✓ |

**Connection config:**
```bash
SRC_3_DB_TYPE=athena
SRC_3_HOST=athena.us-east-1.amazonaws.com
SRC_3_USERNAME=your-aws-access-key
SRC_3_PASSWORD=your-aws-secret-key
ATHENA_S3_OUTPUT=s3://your-bucket/athena-results/
ATHENA_REGION=us-east-1
# In database_registry.yaml:
# SRC_3: {database: migration_validator_test, schema: migration_validator_test}
```

---

### AWS Redshift

**Driver:** `psycopg2-binary` (Redshift speaks the PostgreSQL wire protocol)
**Status:** Implemented

| Feature | Support |
|---------|---------|
| Schema extraction | ✓ |
| Primary key discovery | ✓ |
| Column type mapping | ✓ |
| Count validation SQL | ✓ |
| Data validation SQL | ✓ |

**Connection config:**
```bash
SRC_4_DB_TYPE=redshift
SRC_4_HOST=your-cluster.redshift.amazonaws.com
SRC_4_PORT=5439
SRC_4_USERNAME=your-user
SRC_4_PASSWORD=your-password
# In database_registry.yaml:
# SRC_4: {database: dev, schema: public}
```

---

## Target System

### Snowflake

**Driver:** `snowflake-connector-python`  
**Status:** Implemented

| Feature | Support |
|---------|---------|
| Schema extraction | ✓ |
| Primary key discovery | ✓ |
| _FIVETRAN_ACTIVE filter | ✓ (automatic) |
| SCD2 history handling | ✓ (via _FIVETRAN_ACTIVE) |
| Medallion layer routing | ✓ (bronze/silver/gold) |
| Count validation SQL | ✓ |
| Data validation SQL | ✓ |

**Connection config:**
```bash
SNOWFLAKE_ACCOUNT=your-account.snowflakecomputing.com
SNOWFLAKE_USERNAME=your-user
SNOWFLAKE_PASSWORD=your-password
SNOWFLAKE_WAREHOUSE=your-warehouse
SNOWFLAKE_ROLE=your-role
# Database/schema set dynamically per table
```

**Known limitation:** `list_databases` / `list_schemas` / `list_tables` only accept a source
slot (`SRC_1`, `SRC_2`, `SRC_3`) — there is currently no equivalent tool to browse Snowflake's
own schema directly through Gemini. Snowflake-side schema is instead discovered implicitly as
part of `generate_validation_plan` for a specific source→target table pair. Planned: a
`list_snowflake_tables` tool for direct target-side discovery.

---

## Multi-Connection Registry

Up to N source connections can be registered:

```bash
# .env
SRC_1_DB_TYPE=postgresql
SRC_1_HOST=postgres.prod.company.com
SRC_1_USERNAME=...
SRC_1_PASSWORD=...

SRC_2_DB_TYPE=mssql
SRC_2_HOST=mssql.prod.company.com
SRC_2_USERNAME=...
SRC_2_PASSWORD=...

SRC_3_DB_TYPE=athena
SRC_3_HOST=athena.us-east-1.amazonaws.com
SRC_3_USERNAME=...   # AWS access key
SRC_3_PASSWORD=...   # AWS secret key
```

The `discover_connections` Gemini tool returns all registered connections. The validator can target any source → Snowflake pair.
