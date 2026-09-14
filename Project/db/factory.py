import os
from pathlib import Path

from dotenv import dotenv_values

from db.postgres import Postgres
from db.mssqlserver import Mssqlserver
from db.athena import Athena
from db.snowflake import Snowflake

PROJECT_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = PROJECT_DIR.parent

# One .env file per environment — same shape/keys as the root .env already
# used by webapp/src (SRC_N_*, SNOWFLAKE_*), just a different file per target.
# This replaces the old Project/creds/<env>.yaml scheme so there is a single
# credential format instead of two.
_ENV_FILE_BY_ENVIRONMENT = {
    "local": ".env",
    "dev": ".env.dev",
    "uat": ".env.uat",
    "prod": ".env.prod",
}

_TYPE_ALIASES = {
    "postgresql": {"postgresql", "postgres"},
    "mssql": {"mssql", "mssqlserver"},
    "athena": {"athena", "aws_athena"},
    "redshift": {"redshift", "aws_redshift"},
}


def _load_env(environment: str) -> dict:
    # Falls back to real process environment variables when no per-environment
    # .env file exists — e.g. Cloud Run, which injects SRC_N_*/SNOWFLAKE_* as
    # actual env vars/secrets rather than a checked-in .env file. The file, when
    # present (local dev), still takes precedence over the ambient environment.
    fname = _ENV_FILE_BY_ENVIRONMENT.get(environment, f".env.{environment}")
    path = ROOT_DIR / fname
    file_env = dotenv_values(path) if path.exists() else {}
    return {**os.environ, **file_env}


def _find_source(env: dict, db_type: str) -> dict:
    """First SRC_N_* connection in env whose TYPE matches db_type. Returns the
    per-connection fields with the 'SRC_N_' prefix stripped."""
    wanted = _TYPE_ALIASES.get(db_type, {db_type})
    for i in range(1, 11):
        prefix = f"SRC_{i}_"
        raw_type = (env.get(f"{prefix}TYPE") or "").lower().strip()
        if raw_type in wanted:
            return {k[len(prefix):]: v for k, v in env.items() if k.startswith(prefix) and v}
    raise ValueError(
        f"No SRC_N_TYPE={db_type} connection found for this environment's env file. "
        f"Add one (SRC_N_TYPE={db_type}, SRC_N_HOST, SRC_N_DATABASE, ...)."
    )


def get_database(db_type, BASE_DIR, environment,
                 override_database: str = "", override_schema: str = ""):
    """
    Build a DB connector from .env credentials + optional YAML-level overrides.

    override_database / override_schema come from the YAML config and take
    precedence over whatever DATABASE/SCHEMA is in .env.  Only credentials
    (host, port, user, password, driver) stay in .env — the database and schema
    are the user's choice at generation time and travel with the YAML.
    """
    env = _load_env(environment)

    if db_type == "postgresql":
        src = _find_source(env, "postgresql")
        return Postgres(
            dbname=override_database or src["DATABASE"],
            host=src["HOST"],
            user=src["USERNAME"],
            password=src.get("PASSWORD", ""),
            port=int(src.get("PORT") or 5432),
            schema=override_schema or src.get("SCHEMA", ""),
        )

    elif db_type == "redshift":
        # Redshift speaks the PostgreSQL wire protocol — reuse Postgres as-is,
        # just with a different default port.
        src = _find_source(env, "redshift")
        return Postgres(
            dbname=override_database or src["DATABASE"],
            host=src["HOST"],
            user=src["USERNAME"],
            password=src.get("PASSWORD", ""),
            port=int(src.get("PORT") or 5439),
            schema=override_schema or src.get("SCHEMA", ""),
        )

    elif db_type == "mssql":
        src = _find_source(env, "mssql")
        return Mssqlserver(
            DRIVER=src.get("DRIVER", "ODBC Driver 18 for SQL Server"),
            SERVER=src["HOST"],
            DATABASE=override_database or src["DATABASE"],
            UID=src.get("USERNAME", ""),
            PWD=src.get("PASSWORD", ""),
        )

    elif db_type == "athena":
        src = _find_source(env, "athena")
        region = src.get("REGION") or src.get("HOST", "us-east-1")
        s3_output = src.get("QUERY_RESULT_LOCATION") or env.get("ATHENA_S3_OUTPUT", "")
        return Athena(
            AWS_REGION=region,
            ATHENA_DB=override_database or src["DATABASE"],
            ATHENA_OUTPUT=s3_output,
            ACCESS_KEY=src.get("USERNAME", ""),
            SECRET_KEY=src.get("PASSWORD", ""),
        )

    elif db_type == "snowflake":
        return Snowflake(
            SNOWFLAKE_ACCOUNT=env.get("SNOWFLAKE_ACCOUNT", ""),
            SNOWFLAKE_USER=env.get("SNOWFLAKE_USERNAME", ""),
            SNOWFLAKE_PASSWORD=env.get("SNOWFLAKE_PASSWORD", ""),
            SNOWFLAKE_DATABASE=override_database or env.get("SNOWFLAKE_DATABASE", ""),
            SNOWFLAKE_SCHEMA=override_schema or env.get("SNOWFLAKE_SCHEMA", ""),
            SNOWFLAKE_WAREHOUSE=env.get("SNOWFLAKE_WAREHOUSE", ""),
        )

    else:
        raise ValueError(f"Unsupported database: {db_type}")
