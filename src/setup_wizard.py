"""
╔══════════════════════════════════════════════════════════════════════════╗
║   Migration Validator — Enterprise Setup Wizard                         ║
║   AI-guided .env configuration for up to 10 source database connections ║
╚══════════════════════════════════════════════════════════════════════════╝

Key design decisions
────────────────────
  ■  SERVER  ≠  DATABASE
     One PostgreSQL host can run dozens of databases.
     Each (host, port, db_type, database, schema) combo is one CONNECTION
     and gets its own SRC_N_* slot in .env — independent of how many
     physical machines are involved.

  ■  SAME-SERVER REUSE
     When the user says "same host as connection 1", the wizard
     re-uses host / port / username / password and only asks for
     the database and schema.

  ■  LIVE TABLE COUNT
     After credentials are entered and the connection test passes,
     the wizard queries the database and prints a live table count
     so the user knows immediately if the schema name is right.

  ■  ENV FORMAT  (SRC_N_* — new canonical form)
     SRC_1_TYPE, SRC_1_HOST, SRC_1_PORT, SRC_1_DATABASE,
     SRC_1_SCHEMA, SRC_1_USERNAME, SRC_1_PASSWORD
     …
     SRC_10_TYPE, SRC_10_HOST, …

     Legacy unprefixed SOURCE_* keys are also written (pointing at SRC_1)
     so the existing single-table pipeline keeps working unchanged.

  ■  .env is APPENDED not overwritten on re-runs
     If .env already exists the wizard asks before touching it.

Run directly:
    cd src && python setup_wizard.py

Or from the main CLI:
    python validate_cli.py setup
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import click

_SRC_DIR  = Path(__file__).parent
_ROOT_DIR = _SRC_DIR.parent

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT_DIR / ".env")
except ImportError:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Colour palette
# ─────────────────────────────────────────────────────────────────────────────

class _C:
    BOLD    = "\033[1m"
    GREEN   = "\033[92m"
    CYAN    = "\033[96m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    MAGENTA = "\033[95m"
    BLUE    = "\033[94m"
    DIM     = "\033[2m"
    RESET   = "\033[0m"


def _ok(m):      print(f"{_C.GREEN}  ✓  {m}{_C.RESET}")
def _warn(m):    print(f"{_C.YELLOW}  ⚠  {m}{_C.RESET}")
def _err(m):     print(f"{_C.RED}  ✗  {m}{_C.RESET}")
def _info(m):    print(f"{_C.CYAN}  ℹ  {m}{_C.RESET}")
def _dim(m):     print(f"{_C.DIM}     {m}{_C.RESET}")
def _blank():    print()
def _sep(c="─", w=68): print(f"  {c * w}")

def _head(m):
    print(f"\n{_C.BOLD}{_C.CYAN}  {m}{_C.RESET}")
    _sep()

def _box(title: str, items: List[str]) -> None:
    """Print a framed box with a title and bullet items."""
    w = max(len(title), max((len(i) for i in items), default=0)) + 4
    print(f"\n  ┌{'─'*w}┐")
    print(f"  │ {_C.BOLD}{title:<{w-2}}{_C.RESET} │")
    print(f"  ├{'─'*w}┤")
    for item in items:
        print(f"  │  {item:<{w-3}}│")
    print(f"  └{'─'*w}┘")


# ─────────────────────────────────────────────────────────────────────────────
# Input helpers
# ─────────────────────────────────────────────────────────────────────────────

def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    text = f"\n  {click.style(label, bold=True)}"
    value = click.prompt(text, default=default, show_default=not secret, hide_input=secret)
    return value.strip() if value and value.strip() else default


def _pick(options: List[str], prompt: str = "Choose", allow_new: bool = False) -> int:
    """
    Print a numbered menu and return the zero-based index chosen.
    If allow_new=True, option 0 (index -1) means 'add new'.
    """
    _blank()
    if allow_new:
        print(f"    {_C.MAGENTA}[0]{_C.RESET}  ← Add new / different connection")
    for i, opt in enumerate(options, 1):
        print(f"    {_C.CYAN}[{i}]{_C.RESET}  {opt}")
    _blank()
    while True:
        raw = input(f"  {prompt}: ").strip()
        try:
            n = int(raw)
            if allow_new and n == 0:
                return -1
            if 1 <= n <= len(options):
                return n - 1
        except ValueError:
            pass
        _warn(f"Please enter a number{' 0–' if allow_new else ' 1–'}{len(options)}.")


def _yn(question: str, default: bool = True) -> bool:
    return click.confirm(f"\n  {question}", default=default)


# ─────────────────────────────────────────────────────────────────────────────
# DB type catalogue
# ─────────────────────────────────────────────────────────────────────────────

DB_TYPES: Dict[str, Tuple[str, int]] = {
    "postgresql": ("PostgreSQL",              5432),
    "mssql":      ("Microsoft SQL Server",    1433),
    "snowflake":  ("Snowflake (as source)",   443),
    "athena":     ("AWS Athena",              443),
    "redshift":   ("AWS Redshift",            5439),
}

_DB_ALIASES = {
    "1": "postgresql",
    "2": "mssql",
    "3": "snowflake",
    "4": "athena",
    "5": "redshift",
    "pg": "postgresql",
    "postgres": "postgresql",
    "sql server": "mssql",
    "sqlserver": "mssql",
    "aws": "athena",
    "aws athena": "athena",
    "amazon athena": "athena",
    "aws redshift": "redshift",
    "amazon redshift": "redshift",
}


def _pick_db_type() -> str:
    """Interactive DB type picker. Returns canonical type string."""
    _blank()
    print(f"  {_C.BOLD}Database type:{_C.RESET}")
    for k, (display, port) in DB_TYPES.items():
        idx = list(DB_TYPES.keys()).index(k) + 1
        note = "(no port — S3 staging)" if k == "athena" else f"(default port {port})"
        print(f"    {_C.CYAN}[{idx}]{_C.RESET}  {display:<32} {_C.DIM}{note}{_C.RESET}")
    _blank()
    while True:
        raw = input("  Enter number or name: ").strip().lower()
        if raw in _DB_ALIASES:
            return _DB_ALIASES[raw]
        if raw in DB_TYPES:
            return raw
        _warn(f"Please enter a number 1–{len(DB_TYPES)}.")


# ─────────────────────────────────────────────────────────────────────────────
# Connection dataclass
# ─────────────────────────────────────────────────────────────────────────────

class SourceConnection:
    """
    Represents one validated source database connection.

    Conceptually: (server_host, server_port, db_type, database, schema)
    Two connections on the same host but different databases are distinct.
    """
    __slots__ = (
        "index", "db_type", "host", "port",
        "database", "schema", "username", "password", "auth",
        "display_name", "table_count", "extra_env",
    )

    def __init__(self, index: int, db_type: str, host: str, port: int,
                 database: str, schema: str, username: str, password: str,
                 extra_env: Optional[Dict[str, str]] = None, auth: str = ""):
        self.index        = index
        self.db_type      = db_type
        self.host         = host
        self.port         = port
        self.database     = database
        self.schema       = schema
        self.username     = username
        self.password     = password
        self.auth         = auth      # "windows" | "sql" | ""
        self.table_count  = -1    # -1 = not yet fetched
        self.extra_env    = extra_env or {}  # Athena-specific or other extra vars

        db_label, _ = DB_TYPES.get(db_type, (db_type, 0))
        if db_type == "athena":
            self.display_name = f"{db_label}  region={host}/{database}"
        else:
            self.display_name = f"{db_label}  {host}:{port}/{database}.{schema}"

    def short_label(self) -> str:
        cnt = f"  ({self.table_count} tables)" if self.table_count >= 0 else ""
        return f"[SRC_{self.index}]  {self.display_name}{cnt}"

    def same_server_as(self, other: "SourceConnection") -> bool:
        return (
            self.db_type  == other.db_type  and
            self.host     == other.host     and
            self.port     == other.port     and
            self.username == other.username
        )

    def env_vars(self, prefix: str) -> Dict[str, str]:
        d = {
            f"{prefix}TYPE":     self.db_type,
            f"{prefix}HOST":     self.host,
            f"{prefix}PORT":     str(self.port),
            f"{prefix}DATABASE": self.database,
            f"{prefix}SCHEMA":   self.schema,
            f"{prefix}USERNAME": self.username,
            f"{prefix}PASSWORD": self.password,
        }
        if self.auth:
            d[f"{prefix}AUTH"] = self.auth
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Live discovery helpers  (databases / schemas / tables via live connection)
# ─────────────────────────────────────────────────────────────────────────────

def _discover_postgres_databases(host: str, port: int, username: str, password: str) -> List[str]:
    """Return list of database names on a live PostgreSQL server (excludes system DBs)."""
    try:
        import psycopg2
        c = psycopg2.connect(
            host=host, port=port, database="postgres",
            user=username, password=password, connect_timeout=10,
        )
        cur = c.cursor()
        cur.execute(
            "SELECT datname FROM pg_database "
            "WHERE datistemplate = false AND datname NOT IN ('postgres','rdsadmin') "
            "ORDER BY datname;"
        )
        dbs = [row[0] for row in cur.fetchall()]
        c.close()
        return dbs
    except Exception:
        return []


def _discover_postgres_schemas(host: str, port: int, database: str,
                                username: str, password: str) -> List[str]:
    """Return user-visible schemas for a PostgreSQL database."""
    try:
        import psycopg2
        c = psycopg2.connect(
            host=host, port=port, database=database,
            user=username, password=password, connect_timeout=10,
        )
        cur = c.cursor()
        cur.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('information_schema','pg_catalog','pg_toast') "
            "  AND schema_name NOT LIKE 'pg_%' "
            "ORDER BY schema_name;"
        )
        schemas = [row[0] for row in cur.fetchall()]
        c.close()
        return schemas
    except Exception:
        return []


def _discover_postgres_table_count(host: str, port: int, database: str,
                                    schema: str, username: str, password: str) -> int:
    """Return table count for a PostgreSQL schema (-1 on error)."""
    try:
        import psycopg2
        c = psycopg2.connect(
            host=host, port=port, database=database,
            user=username, password=password, connect_timeout=10,
        )
        cur = c.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE';",
            (schema,)
        )
        count = cur.fetchone()[0]
        c.close()
        return int(count)
    except Exception:
        return -1


def _discover_mssql_databases(host: str, port: int, username: str, password: str,
                               auth: str = "") -> List[str]:
    """Return list of user databases on a live MSSQL server."""
    try:
        import pyodbc
        driver = os.getenv("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
        base = (
            f"DRIVER={{{driver}}};SERVER={host},{port};"
            f"TrustServerCertificate=yes;Encrypt=optional;Connection Timeout=10;"
        )
        cs = (base + "Trusted_Connection=yes;") if auth.lower() in ("windows", "win") \
             else (base + f"UID={username};PWD={password};")
        c = pyodbc.connect(cs)
        cur = c.cursor()
        cur.execute(
            "SELECT name FROM sys.databases "
            "WHERE database_id > 4 ORDER BY name;"  # skip master/model/msdb/tempdb
        )
        dbs = [row[0] for row in cur.fetchall()]
        c.close()
        return dbs
    except Exception:
        return []


def _discover_mssql_schemas(host: str, port: int, database: str,
                             username: str, password: str, auth: str = "") -> List[str]:
    """Return user schemas for a MSSQL database."""
    try:
        import pyodbc
        driver = os.getenv("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
        base = (
            f"DRIVER={{{driver}}};SERVER={host},{port};DATABASE={database};"
            f"TrustServerCertificate=yes;Encrypt=optional;Connection Timeout=10;"
        )
        cs = (base + "Trusted_Connection=yes;") if auth.lower() in ("windows", "win") \
             else (base + f"UID={username};PWD={password};")
        c = pyodbc.connect(cs)
        cur = c.cursor()
        cur.execute(
            "SELECT name FROM sys.schemas "
            "WHERE name NOT IN ('sys','INFORMATION_SCHEMA','guest','db_owner',"
            "'db_accessadmin','db_securityadmin','db_ddladmin','db_backupoperator',"
            "'db_datareader','db_datawriter','db_denydatareader','db_denydatawriter') "
            "ORDER BY name;"
        )
        schemas = [row[0] for row in cur.fetchall()]
        c.close()
        return schemas
    except Exception:
        return []


def _discover_mssql_table_count(host: str, port: int, database: str,
                                 schema: str, username: str, password: str,
                                 auth: str = "") -> int:
    """Return table count for a MSSQL schema (-1 on error)."""
    try:
        import pyodbc
        driver = os.getenv("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
        base = (
            f"DRIVER={{{driver}}};SERVER={host},{port};DATABASE={database};"
            f"TrustServerCertificate=yes;Encrypt=optional;Connection Timeout=10;"
        )
        cs = (base + "Trusted_Connection=yes;") if auth.lower() in ("windows", "win") \
             else (base + f"UID={username};PWD={password};")
        c = pyodbc.connect(cs)
        cur = c.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND table_type = 'BASE TABLE';",
            schema,
        )
        count = cur.fetchone()[0]
        c.close()
        return int(count)
    except Exception:
        return -1


def _discover_snowflake_databases(account: str, username: str, password: str,
                                   warehouse: str = "", role: str = "") -> List[str]:
    """Return list of Snowflake databases the user can access."""
    try:
        import snowflake.connector
        params = dict(account=account, user=username, password=password, login_timeout=15)
        if warehouse:
            params["warehouse"] = warehouse
        if role:
            params["role"] = role
        c = snowflake.connector.connect(**params)
        cur = c.cursor()
        cur.execute("SHOW DATABASES;")
        dbs = [row[1] for row in cur.fetchall()]  # col 1 = name
        c.close()
        return dbs
    except Exception:
        return []


def _discover_snowflake_schemas(account: str, database: str, username: str,
                                 password: str, warehouse: str = "", role: str = "") -> List[Tuple[str, int]]:
    """
    Return list of (schema_name, table_count) for a Snowflake database.
    Excludes INFORMATION_SCHEMA.
    """
    try:
        import snowflake.connector
        params = dict(account=account, database=database,
                      user=username, password=password, login_timeout=15)
        if warehouse:
            params["warehouse"] = warehouse
        if role:
            params["role"] = role
        c = snowflake.connector.connect(**params)
        cur = c.cursor()
        cur.execute(f"SHOW SCHEMAS IN DATABASE {database};")
        all_schemas = [row[1] for row in cur.fetchall() if row[1] != "INFORMATION_SCHEMA"]
        results = []
        for schema in all_schemas:
            try:
                cur.execute(
                    f"SELECT COUNT(*) FROM {database}.INFORMATION_SCHEMA.TABLES "
                    f"WHERE TABLE_SCHEMA = '{schema}' AND TABLE_TYPE = 'BASE TABLE';"
                )
                count = cur.fetchone()[0]
            except Exception:
                count = -1
            results.append((schema, int(count)))
        c.close()
        return results
    except Exception:
        return []


def _pick_from_live_list(items: List[str], prompt_text: str,
                          allow_manual: bool = True,
                          default: str = "") -> str:
    """
    Show a numbered list and return the chosen item.
    If allow_manual=True, option 0 = 'enter manually'.
    Falls back to _prompt() on empty list.
    """
    if not items:
        return _prompt(prompt_text, default).strip() or default

    _blank()
    if allow_manual:
        print(f"    {_C.DIM}[0]{_C.RESET}  (enter manually)")
    for i, item in enumerate(items, 1):
        marker = f"  {_C.DIM}← default{_C.RESET}" if item == default else ""
        print(f"    {_C.CYAN}[{i}]{_C.RESET}  {item}{marker}")
    _blank()

    while True:
        raw = input(
            f"  {prompt_text}" +
            (f" [{_C.DIM}{default}{_C.RESET}]" if default else "") +
            ": "
        ).strip()
        if not raw and default:
            return default
        if allow_manual and raw == "0":
            return _prompt(prompt_text, default).strip() or default
        try:
            n = int(raw)
            if 1 <= n <= len(items):
                return items[n - 1]
        except ValueError:
            if raw:
                return raw  # typed a name directly
        _warn(f"Please enter 0–{len(items)}.")


# ─────────────────────────────────────────────────────────────────────────────
# Connection testers
# ─────────────────────────────────────────────────────────────────────────────

def _test_connection(conn: SourceConnection) -> Tuple[bool, str, int]:
    """
    Test a source connection.
    Returns (success, message, table_count).
    table_count = -1 if we could not fetch it.
    """
    try:
        if conn.db_type in ("postgresql", "postgres", "pg"):
            return _test_postgres(conn)
        elif conn.db_type in ("mssql", "sqlserver", "sql_server"):
            return _test_mssql(conn)
        elif conn.db_type == "snowflake":
            return _test_snowflake_source(conn)
        elif conn.db_type in ("athena", "aws_athena"):
            return _test_athena(conn)
        else:
            return False, f"Unknown db_type '{conn.db_type}'", -1
    except Exception as exc:
        return False, f"Unexpected error: {exc}", -1


def _test_postgres(conn: SourceConnection) -> Tuple[bool, str, int]:
    try:
        import psycopg2
        c = psycopg2.connect(
            host=conn.host, port=conn.port, database=conn.database,
            user=conn.username, password=conn.password, connect_timeout=12,
        )
        cur = c.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE';",
            (conn.schema,)
        )
        count = cur.fetchone()[0]
        cur.execute("SELECT version();")
        ver = cur.fetchone()[0].split(",")[0]
        c.close()
        return True, ver, int(count)
    except ImportError:
        return False, "psycopg2-binary not installed — run: pip install psycopg2-binary", -1
    except Exception as exc:
        return False, str(exc), -1


def _test_mssql(conn: SourceConnection) -> Tuple[bool, str, int]:
    try:
        import pyodbc
        driver = os.getenv("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
        base = (
            f"DRIVER={{{driver}}};SERVER={conn.host},{conn.port};"
            f"DATABASE={conn.database};"
            f"TrustServerCertificate=yes;Encrypt=optional;Connection Timeout=12;"
        )
        raw_auth = getattr(conn, "auth", "").lower().strip()
        use_windows = raw_auth in ("windows", "win", "ntlm", "trusted")
        if use_windows:
            cs = base + "Trusted_Connection=yes;"
        else:
            cs = base + f"UID={conn.username};PWD={conn.password};"
        c = pyodbc.connect(cs)
        cur = c.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND table_type = 'BASE TABLE';",
            conn.schema,
        )
        count = cur.fetchone()[0]
        cur.execute(
            "SELECT @@SERVERNAME AS server_name, DB_NAME() AS database_name, "
            "SUSER_SNAME() AS login_name;"
        )
        row = cur.fetchone()
        ver = f"server={row[0]}, db={row[1]}, login={row[2]}"
        c.close()
        return True, ver, int(count)
    except ImportError:
        return False, "pyodbc not installed — run: pip install pyodbc", -1
    except Exception as exc:
        return False, str(exc), -1


def _test_snowflake_source(conn: SourceConnection) -> Tuple[bool, str, int]:
    try:
        import snowflake.connector
        c = snowflake.connector.connect(
            account=conn.host, database=conn.database,
            schema=conn.schema, user=conn.username, password=conn.password,
            login_timeout=20,
        )
        cur = c.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM {conn.database}.INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_SCHEMA = '{conn.schema.upper()}' AND TABLE_TYPE = 'BASE TABLE';"
        )
        count = cur.fetchone()[0]
        c.close()
        return True, "Snowflake connection OK", int(count)
    except ImportError:
        return False, "snowflake-connector-python not installed", -1
    except Exception as exc:
        return False, str(exc), -1


def _test_athena(conn: SourceConnection, s3_output: str = "", region: str = "") -> Tuple[bool, str, int]:
    """
    Test AWS Athena connectivity via the Glue Data Catalog (boto3 get_tables).

    Uses Glue rather than running an Athena SQL query so the ping works even when
    the Athena workgroup has IAM access control restrictions.  Falls back to an
    Athena SQL query only if boto3 is unavailable.

    Callers may pass s3_output and region directly (e.g. from SRC_N_QUERY_RESULT_LOCATION
    and SRC_N_REGION) to avoid relying solely on global env vars.
    """
    region   = (region or conn.host or os.getenv("ATHENA_REGION", "us-east-1")).strip()
    database = conn.database

    boto3_kwargs = dict(region_name=region)
    if conn.username and conn.password:
        boto3_kwargs["aws_access_key_id"]     = conn.username
        boto3_kwargs["aws_secret_access_key"] = conn.password

    try:
        import boto3
        glue = boto3.client("glue", **boto3_kwargs)
        paginator = glue.get_paginator("get_tables")
        count = 0
        for page in paginator.paginate(DatabaseName=database):
            count += len(page.get("TableList", []))
        return True, f"Athena/Glue region={region}", count
    except ImportError:
        pass  # boto3 not installed — fall back to pyathena SQL below
    except Exception as exc:
        err = str(exc)
        if "NoCredentialsError" in type(exc).__name__ or "NoCredentialsError" in err:
            return False, "AWS credentials not found — set SRC_N_USERNAME/PASSWORD or configure ~/.aws/credentials", -1
        return False, err, -1

    # ── Fallback: pyathena SQL query ──────────────────────────────────────────
    s3_output = (s3_output or os.getenv("ATHENA_S3_OUTPUT", "")).strip()
    if not s3_output:
        return False, "ATHENA_S3_OUTPUT is not set in .env — required for Athena", -1

    try:
        import pyathena
    except ImportError:
        return False, "boto3 and pyathena are both unavailable — run: pip install boto3", -1

    catalog   = os.getenv("ATHENA_CATALOG",   "AwsDataCatalog")
    workgroup = os.getenv("ATHENA_WORKGROUP",  "primary")
    kwargs = dict(
        region_name=region, s3_staging_dir=s3_output,
        catalog_name=catalog, work_group=workgroup, schema_name=database,
    )
    if conn.username and conn.password:
        kwargs["aws_access_key_id"]     = conn.username
        kwargs["aws_secret_access_key"] = conn.password

    import io, sys
    try:
        c   = pyathena.connect(**kwargs)
        cur = c.cursor()
        _save, sys.stderr = sys.stderr, io.StringIO()
        try:
            cur.execute(
                f"SELECT COUNT(*) FROM information_schema.tables "
                f"WHERE table_schema = '{database.lower()}' "
                f"AND table_type IN ('BASE TABLE', 'EXTERNAL TABLE')"
            )
        finally:
            sys.stderr = _save
        count = cur.fetchone()[0]
        c.close()
        return True, f"Athena region={region} catalog={catalog}", int(count)
    except Exception as exc:
        err = str(exc)
        if "NoCredentialsError" in type(exc).__name__ or "NoCredentialsError" in err:
            return False, "AWS credentials not found — set SRC_N_USERNAME/PASSWORD or configure ~/.aws/credentials", -1
        return False, err, -1


def _test_snowflake_target(account, database, schema, username, password,
                           warehouse=None, role=None) -> Tuple[bool, str, int]:
    try:
        import snowflake.connector
        params = dict(
            account=account, database=database, schema=schema,
            user=username, password=password, login_timeout=20,
        )
        if warehouse:
            params["warehouse"] = warehouse
        if role:
            params["role"] = role
        c = snowflake.connector.connect(**params)
        cur = c.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM {database}.INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_SCHEMA = '{schema.upper()}' AND TABLE_TYPE = 'BASE TABLE';"
        )
        count = cur.fetchone()[0]
        cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE();")
        row = cur.fetchone()
        c.close()
        return True, f"user={row[0]}, role={row[1]}", int(count)
    except ImportError:
        return False, "snowflake-connector-python not installed", -1
    except Exception as exc:
        return False, str(exc), -1


def _test_dial(api_key, api_base, api_version, model) -> Tuple[bool, str]:
    try:
        from openai import AzureOpenAI
        client = AzureOpenAI(
            api_key=api_key, azure_endpoint=api_base,
            api_version=api_version, timeout=15,
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with one word: READY"}],
            max_tokens=5, temperature=0,
        )
        return True, f"model={model}, reply='{resp.choices[0].message.content.strip()}'"
    except ImportError:
        return False, "openai package not installed — run: pip install openai"
    except Exception as exc:
        return False, str(exc)


# ─────────────────────────────────────────────────────────────────────────────
# Wizard steps
# ─────────────────────────────────────────────────────────────────────────────

def _wizard_banner():
    print(f"""
{_C.CYAN}{_C.BOLD}
╔══════════════════════════════════════════════════════════════════════╗
║   Migration Validator  —  Enterprise Setup Wizard                   ║
║                                                                      ║
║   Configure up to 10 source database connections (any mix of        ║
║   PostgreSQL / MSSQL / Snowflake) → one Snowflake target.           ║
║                                                                      ║
║   Key concept:  SERVER  ≠  DATABASE                                  ║
║   One PostgreSQL host with 3 databases = 3 separate connections.    ║
║   The wizard handles same-server reuse automatically.               ║
╚══════════════════════════════════════════════════════════════════════╝{_C.RESET}
    """)


def _step_how_many() -> int:
    _head("STEP 1 — How many source database connections?")
    _blank()
    print(f"  {_C.BOLD}A connection = one (host + database + schema) combination.{_C.RESET}")
    _blank()
    print(f"  {_C.DIM}Examples:{_C.RESET}")
    _dim("  1 connection  →  postgres-prod:5432/fms.public")
    _dim("  2 connections →  postgres-prod:5432/fms.public")
    _dim("                    postgres-prod:5432/billing.public   ← same server, different DB")
    _dim("  3 connections →  above 2 + mssql-crm:1433/CRM.dbo")
    _blank()
    _info("You can add up to 10 connections (any mix of PostgreSQL / MSSQL / Snowflake).")
    _blank()

    while True:
        raw = _prompt("Number of source connections (1 – 10)", "1")
        try:
            n = int(raw)
            if 1 <= n <= 10:
                return n
            _warn("Please enter a number from 1 to 10.")
        except ValueError:
            _warn("Please enter a number.")


def _collect_one_source(idx: int, total: int,
                         existing: List[SourceConnection]) -> SourceConnection:
    """
    Collect credentials for one source connection.

    Smart features:
      - If a previous connection shares the same server, offer to reuse creds
      - After credentials, test connection live and show table count
      - Allow retry on failure without restarting
    """
    _blank()
    _sep("═")
    print(f"  {_C.BOLD}{_C.MAGENTA}CONNECTION {idx} of {total}{_C.RESET}")
    _sep("═")

    # ── DB type ───────────────────────────────────────────────────────────────
    db_type = _pick_db_type()
    db_display, default_port = DB_TYPES[db_type]

    # ── Same-server reuse check ───────────────────────────────────────────────
    same_server_opts = [
        c for c in existing
        if c.db_type == db_type
    ]
    reuse_host = ""
    reuse_port = default_port
    reuse_user = ""
    reuse_pass = ""

    if same_server_opts and idx > 1:
        _blank()
        print(f"  {_C.BOLD}Is this connection on a server you already configured?{_C.RESET}")
        _dim("(Same host means we can reuse host/port/username/password — just ask for the DB)")
        labels = [f"{c.host}:{c.port}  (SRC_{c.index})" for c in same_server_opts]
        labels.append("Different server")
        choice = _pick(labels, prompt="Choose or enter the server number")
        if choice < len(same_server_opts):
            ref = same_server_opts[choice]
            reuse_host = ref.host
            reuse_port = ref.port
            reuse_user = ref.username
            reuse_pass = ref.password
            _ok(f"Reusing server credentials from SRC_{ref.index}  ({ref.host}:{ref.port})")

    # ── Athena: special setup flow (region instead of host/port) ────────────
    athena_extra: Dict[str, str] = {}
    if db_type == "athena":
        _blank()
        print(f"  {_C.BOLD}{_C.YELLOW}AWS Athena Setup{_C.RESET}")
        _dim("Athena uses S3 for query staging and the Glue Data Catalog as schema.")
        _blank()

        region = _prompt("AWS region  (e.g. us-east-1)", reuse_host or os.getenv("ATHENA_REGION", "us-east-1"))
        host   = region   # repurpose host slot as region
        port   = 443      # placeholder — not used for Athena

        s3_out = _prompt(
            "S3 staging URI  (e.g. s3://my-bucket/athena-results/)",
            os.getenv("ATHENA_S3_OUTPUT", ""),
        ).strip()
        while not s3_out.startswith("s3://"):
            _warn("S3 staging URI must start with s3://")
            s3_out = _prompt("S3 staging URI", "").strip()

        catalog   = _prompt("Glue Data Catalog name", os.getenv("ATHENA_CATALOG",   "AwsDataCatalog")).strip()
        workgroup = _prompt("Athena workgroup",        os.getenv("ATHENA_WORKGROUP", "primary")).strip()

        athena_extra = {
            "ATHENA_REGION":    region,
            "ATHENA_S3_OUTPUT": s3_out,
            "ATHENA_CATALOG":   catalog,
            "ATHENA_WORKGROUP": workgroup,
        }

        _blank()
        database = _prompt("Glue database / Athena database name", "").strip()
        while not database:
            _warn("Database name is required.")
            database = _prompt("Glue database / Athena database name", "").strip()

        schema = database  # Athena has no separate schema — schema == database

        _blank()
        _dim("Authentication: leave blank to use IAM role / instance profile.")
        _dim("Or enter AWS access key + secret for explicit credentials.")
        username = _prompt("AWS Access Key ID  (blank = IAM role)", "").strip()
        password = _prompt("AWS Secret Access Key", "" if not username else "", secret=bool(username)).strip()

    else:
        # ── Host ─────────────────────────────────────────────────────────────
        _blank()
        host = _prompt(f"{db_display} host", reuse_host or "localhost")

        # ── Port ─────────────────────────────────────────────────────────────
        port_str = _prompt(f"Port", str(reuse_port or default_port))
        try:
            port = int(port_str)
        except ValueError:
            port = default_port

        # ── Username / Password (needed before discovery) ─────────────────────
        username = _prompt("Username", reuse_user)
        while not username.strip():
            _warn("Username is required.")
            username = _prompt("Username", reuse_user)
        username = username.strip()
        password = _prompt("Password", reuse_pass, secret=True)

        # ── Database — live discovery ─────────────────────────────────────────
        _blank()
        print(f"  {_C.DIM}Discovering databases on {host}:{port} ...{_C.RESET}")
        if db_type in ("postgresql", "postgres", "pg"):
            live_dbs = _discover_postgres_databases(host, port, username, password)
        elif db_type in ("mssql", "sqlserver"):
            live_dbs = _discover_mssql_databases(host, port, username, password)
        else:
            live_dbs = []

        if live_dbs:
            _ok(f"Found {len(live_dbs)} database(s) on {host}:{port}")
            _blank()
            print(f"  {_C.BOLD}Select a database:{_C.RESET}")
            database = _pick_from_live_list(live_dbs, "Database", allow_manual=True)
        else:
            _blank()
            print(f"  {_C.DIM}Tip: one server can have many databases. This is the specific one to validate.{_C.RESET}")
            database = _prompt("Database name", "")
            while not database.strip():
                _warn("Database name is required.")
                database = _prompt("Database name", "")
        database = database.strip()

        # ── Schema — live discovery ───────────────────────────────────────────
        _blank()
        schema_default = "public" if db_type in ("postgresql", "redshift") else "dbo"
        print(f"  {_C.DIM}Discovering schemas in {database} ...{_C.RESET}")
        if db_type in ("postgresql", "postgres", "pg", "redshift"):
            live_schemas = _discover_postgres_schemas(host, port, database, username, password)
        elif db_type in ("mssql", "sqlserver"):
            live_schemas = _discover_mssql_schemas(host, port, database, username, password)
        else:
            live_schemas = []

        if live_schemas:
            _ok(f"Found {len(live_schemas)} schema(s) in {database}")
            # Annotate each schema with its table count
            annotated = []
            for s in live_schemas:
                if db_type in ("postgresql", "postgres", "pg"):
                    cnt = _discover_postgres_table_count(host, port, database, s, username, password)
                elif db_type in ("mssql", "sqlserver"):
                    cnt = _discover_mssql_table_count(host, port, database, s, username, password)
                else:
                    cnt = -1
                label = f"{s}  ({cnt} tables)" if cnt >= 0 else s
                annotated.append(label)
            _blank()
            print(f"  {_C.BOLD}Select a schema in {_C.GREEN}{database}{_C.RESET}{_C.BOLD}:{_C.RESET}")
            chosen_label = _pick_from_live_list(annotated, "Schema", allow_manual=True, default=schema_default)
            # Strip the "(N tables)" annotation if present
            schema = chosen_label.split("  (")[0].strip() if "  (" in chosen_label else chosen_label.strip()
            if not schema:
                schema = schema_default
        else:
            schema = _prompt(f"Schema  (default: {schema_default})", schema_default).strip()
            if not schema:
                schema = schema_default

    conn = SourceConnection(
        index=idx, db_type=db_type, host=host, port=port,
        database=database, schema=schema, username=username, password=password,
        extra_env=athena_extra if db_type == "athena" else {},
    )

    # ── Connection test (with retry) ─────────────────────────────────────────
    _blank()
    while True:
        conn_desc = (
            f"{host}/{database}" if db_type == "athena"
            else f"{host}:{port}/{database}.{schema}"
        )
        if not _yn(f"Test connection to {db_display} ({conn_desc})?"):
            _warn("Skipped — you can fix credentials manually in .env later.")
            break

        _info(f"Connecting to {db_display} ...")
        t0 = time.time()
        ok, msg, table_count = _test_connection(conn)
        elapsed = int((time.time() - t0) * 1000)

        if ok:
            conn.table_count = table_count
            _ok(f"{msg}  ({elapsed}ms)")
            if table_count == 0:
                _warn(f"Schema '{schema}' has 0 tables — double-check the schema name.")
            else:
                _ok(f"Found {table_count} tables in {database}.{schema}")
            break
        else:
            _err(f"Connection failed ({elapsed}ms):  {msg}")
            _blank()
            print(f"  {_C.BOLD}What would you like to do?{_C.RESET}")
            print(f"    {_C.CYAN}[1]{_C.RESET}  Edit credentials and retry")
            print(f"    {_C.YELLOW}[2]{_C.RESET}  Continue anyway  (fix .env manually later)")
            print(f"    {_C.RED}[3]{_C.RESET}  Abort setup")
            _blank()
            action = input("  Choice: ").strip()
            if action == "1":
                # Allow editing any field before retry
                _blank()
                print(f"  {_C.DIM}Leave blank to keep current value.{_C.RESET}")
                new_host = _prompt("Host", conn.host)
                new_port = _prompt("Port", str(conn.port))
                new_db   = _prompt("Database", conn.database)
                new_sch  = _prompt("Schema", conn.schema)
                new_usr  = _prompt("Username", conn.username)
                new_pwd  = _prompt("Password", conn.password, secret=True)
                conn.host     = new_host or conn.host
                conn.port     = int(new_port) if new_port else conn.port
                conn.database = new_db  or conn.database
                conn.schema   = new_sch or conn.schema
                conn.username = new_usr or conn.username
                conn.password = new_pwd or conn.password
                conn.display_name = (
                    f"{DB_TYPES[conn.db_type][0]}  "
                    f"{conn.host}:{conn.port}/{conn.database}.{conn.schema}"
                )
                continue
            elif action == "3":
                _warn("Setup aborted. Nothing was written.")
                sys.exit(0)
            else:
                _warn("Continuing without a successful connection test.")
                break

    return conn


def _collect_snowflake_target() -> Dict[str, str]:
    """Collect and test Snowflake target credentials."""
    _blank()
    _sep("═")
    print(f"  {_C.BOLD}{_C.BLUE}SNOWFLAKE TARGET  (destination){_C.RESET}")
    _sep("═")
    _blank()
    print(f"  {_C.DIM}All source databases are compared against this single Snowflake instance.{_C.RESET}")
    _blank()

    account = _prompt(
        "Snowflake account identifier\n"
        "  (format: ORG_NAME-ACCOUNT_NAME, e.g. myorg-prod1234)\n  ", ""
    ).strip()
    while not account:
        _warn("Account identifier is required.")
        account = _prompt("Snowflake account identifier", "").strip()

    username  = _prompt("Snowflake username", "").strip()
    while not username:
        _warn("Username is required.")
        username = _prompt("Snowflake username", "").strip()
    password  = _prompt("Snowflake password", "", secret=True)

    _blank()
    print(f"  {_C.DIM}Optional — press Enter to skip:{_C.RESET}")
    warehouse = _prompt("Warehouse", "").strip()
    role      = _prompt("Role", "").strip()

    # ── Live Snowflake database discovery ─────────────────────────────────────
    _blank()
    _info(f"Discovering Snowflake databases on {account} ...")
    live_sf_dbs = _discover_snowflake_databases(account, username, password, warehouse, role)
    if live_sf_dbs:
        _ok(f"Found {len(live_sf_dbs)} database(s)")
        _blank()
        print(f"  {_C.BOLD}Select a Snowflake database:{_C.RESET}")
        database = _pick_from_live_list(live_sf_dbs, "Snowflake database", allow_manual=True)
    else:
        _warn("Could not list Snowflake databases — enter the name manually.")
        database = _prompt("Snowflake database", "").strip()
        while not database:
            _warn("Database is required.")
            database = _prompt("Snowflake database", "").strip()

    # ── Live Snowflake schema discovery ──────────────────────────────────────
    _blank()
    _info(f"Discovering schemas in {database} ...")
    live_sf_schemas = _discover_snowflake_schemas(account, database, username, password, warehouse, role)
    if live_sf_schemas:
        _ok(f"Found {len(live_sf_schemas)} schema(s)")
        _blank()
        print(f"  {_C.BOLD}Select a Snowflake schema in {_C.GREEN}{database}{_C.RESET}{_C.BOLD}:{_C.RESET}")
        schema_labels = [
            f"{s}  ({cnt} tables)" if cnt >= 0 else s
            for s, cnt in live_sf_schemas
        ]
        schema_names = [s for s, _ in live_sf_schemas]
        chosen_label = _pick_from_live_list(schema_labels, "Snowflake schema",
                                             allow_manual=True, default="PUBLIC")
        # Strip annotation to get plain schema name
        idx = schema_labels.index(chosen_label) if chosen_label in schema_labels else -1
        if idx >= 0:
            schema = schema_names[idx]
        else:
            schema = chosen_label.split("  (")[0].strip() if "  (" in chosen_label else chosen_label.strip()
        schema = schema or "PUBLIC"
    else:
        _warn("Could not list schemas — enter the schema name manually.")
        schema = _prompt("Snowflake schema", "PUBLIC").strip() or "PUBLIC"

    # ── Test ─────────────────────────────────────────────────────────────────
    _blank()
    while True:
        if not _yn(f"Test Snowflake connection ({account}/{database}.{schema})?"):
            _warn("Skipped. Fix manually in .env if needed.")
            break

        _info("Connecting to Snowflake ...")
        t0 = time.time()
        ok, msg, count = _test_snowflake_target(
            account, database, schema, username, password,
            warehouse or None, role or None,
        )
        elapsed = int((time.time() - t0) * 1000)
        if ok:
            _ok(f"{msg}  ({elapsed}ms)")
            if count >= 0:
                _ok(f"Found {count} tables in {database}.{schema}")
            break
        else:
            _err(f"Connection failed ({elapsed}ms):  {msg}")
            if not _yn("Continue anyway?", default=False):
                _warn("Setup aborted.")
                sys.exit(0)
            break

    env: Dict[str, str] = {
        "SNOWFLAKE_ACCOUNT":  account,
        "SNOWFLAKE_DATABASE": database,
        "SNOWFLAKE_SCHEMA":   schema,
        "SNOWFLAKE_USERNAME": username,
        "SNOWFLAKE_PASSWORD": password,
    }
    if warehouse:
        env["SNOWFLAKE_WAREHOUSE"] = warehouse
    if role:
        env["SNOWFLAKE_ROLE"] = role
    return env


def _collect_ai_settings() -> Dict[str, str]:
    """Optional DIAL / AI settings."""
    _blank()
    _sep("═")
    print(f"  {_C.BOLD}{_C.MAGENTA}AI / DIAL API  (optional){_C.RESET}")
    _sep("═")
    _blank()
    print(
        f"  {_C.DIM}Used for intelligent column rule assignment. Without it,\n"
        f"  static rule matching is used — still covers all standard type mappings.\n"
        f"  Requires EPAM VPN.  Key at: https://ai-proxy.lab.epam.com{_C.RESET}"
    )
    _blank()

    api_key = _prompt(
        "DIAL API key  (leave blank to skip AI setup)", "", secret=True
    ).strip()

    if not api_key:
        _warn("AI setup skipped — static rule matching will be used.")
        return {
            "DIAL_API_KEY":     "",
            "DIAL_API_BASE":    "https://ai-proxy.lab.epam.com",
            "DIAL_API_VERSION": "2025-04-01-preview",
            "DIAL_MODEL":       "gpt-4o",
        }

    api_base    = _prompt("DIAL API base URL", "https://ai-proxy.lab.epam.com").strip()
    api_version = _prompt("API version", "2025-04-01-preview").strip()
    dial_model  = _prompt("Model name", "gpt-4o").strip()

    _blank()
    if _yn("Test DIAL API connection now?"):
        _info(f"Testing DIAL API (model={dial_model}) ...")
        t0 = time.time()
        ok, msg = _test_dial(api_key, api_base, api_version, dial_model)
        elapsed = int((time.time() - t0) * 1000)
        if ok:
            _ok(f"{msg}  ({elapsed}ms)")
        else:
            _err(f"API test failed: {msg}")
            _warn("AI features will fall back to static matching. Fix DIAL_API_KEY in .env when on VPN.")

    return {
        "DIAL_API_KEY":     api_key,
        "DIAL_API_BASE":    api_base,
        "DIAL_API_VERSION": api_version,
        "DIAL_MODEL":       dial_model,
    }


def _show_connection_registry(connections: List[SourceConnection]) -> None:
    """Print a summary table of all configured connections."""
    _blank()
    _sep("═")
    print(f"  {_C.BOLD}  SOURCE CONNECTION REGISTRY{_C.RESET}")
    _sep()
    print(
        f"  {'#':<6}{'Type':<14}{'Host':<24}{'Port':<6}"
        f"{'Database':<20}{'Schema':<14}{'Tables':<8}"
    )
    _sep("─")
    for c in connections:
        tc = str(c.table_count) if c.table_count >= 0 else "?"
        db_label, _ = DB_TYPES.get(c.db_type, (c.db_type, 0))
        print(
            f"  {_C.CYAN}SRC_{c.index:<3}{_C.RESET}"
            f"{db_label:<14}"
            f"{c.host:<24}"
            f"{c.port:<6}"
            f"{_C.GREEN}{c.database:<20}{_C.RESET}"
            f"{c.schema:<14}"
            f"{_C.DIM}{tc:<8}{_C.RESET}"
        )
    _sep("═")


# ─────────────────────────────────────────────────────────────────────────────
# .env writer
# ─────────────────────────────────────────────────────────────────────────────

def _build_env_content(
    connections: List[SourceConnection],
    sf_env: Dict[str, str],
    ai_env: Dict[str, str],
) -> str:
    """
    Build the full .env content string from wizard results.

    Env-var naming:
      SRC_N_*       → canonical new format (SRC_1_TYPE, SRC_1_HOST, …)
      SOURCE_*      → legacy unprefixed keys pointing at SRC_1 (backward compat)
    """
    lines = [
        "# ════════════════════════════════════════════════════════════════",
        "# Migration Validator — Environment Configuration",
        f"# Generated by setup wizard — {len(connections)} source connection(s)",
        "# NEVER commit this file — it is git-ignored.",
        "# ════════════════════════════════════════════════════════════════",
        "",
        "# ── AI / DIAL Settings ───────────────────────────────────────────",
        "# Required for AI-powered column rule assignment.",
        "# Without DIAL_API_KEY the tool uses static rule matching.",
        "# Get key: https://ai-proxy.lab.epam.com (EPAM VPN required)",
        "# ─────────────────────────────────────────────────────────────────",
    ]
    for k, v in ai_env.items():
        lines.append(f"{k}={v}")
    lines.append("")

    lines += [
        "# ── Snowflake Target ─────────────────────────────────────────────",
        "# All source databases are validated against this Snowflake instance.",
        "# ─────────────────────────────────────────────────────────────────",
    ]
    for k, v in sf_env.items():
        lines.append(f"{k}={v}")
    lines.append("")

    for c in connections:
        db_label, _ = DB_TYPES.get(c.db_type, (c.db_type, 0))
        if c.db_type == "athena":
            header = f"# ── Source Connection {c.index}: {db_label}  region={c.host}/{c.database} ──"
        else:
            header = f"# ── Source Connection {c.index}: {db_label}  {c.host}:{c.port}/{c.database}.{c.schema} ──"
        lines.append(header)
        for k, v in c.env_vars(f"SRC_{c.index}_").items():
            lines.append(f"{k}={v}")
        # Athena-specific extra env vars (ATHENA_REGION, ATHENA_S3_OUTPUT, etc.)
        for k, v in (c.extra_env or {}).items():
            lines.append(f"{k}={v}")
        lines.append("")

    # Legacy unprefixed SOURCE_* keys (point at SRC_1 for backward compat)
    first = connections[0]
    lines += [
        "# ── Legacy single-source keys (backward-compat — point at SRC_1) ─",
        "# Used by the single-table 'generate' command and validation pipeline.",
        "# ─────────────────────────────────────────────────────────────────",
    ]
    for k, v in first.env_vars("SOURCE_").items():
        lines.append(f"{k}={v}")
    lines.append("")

    return "\n".join(lines)


def _write_env_file(env_path: Path, content: str) -> None:
    env_path.write_text(content, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Review & save step
# ─────────────────────────────────────────────────────────────────────────────

def _review_and_save(
    connections: List[SourceConnection],
    sf_env: Dict[str, str],
    ai_env: Dict[str, str],
    env_path: Path,
) -> None:
    _show_connection_registry(connections)
    _blank()

    # Snowflake summary
    print(f"  {_C.BOLD}Snowflake Target:{_C.RESET}")
    print(
        f"    account={_C.GREEN}{sf_env.get('SNOWFLAKE_ACCOUNT','')}{_C.RESET}  "
        f"db={sf_env.get('SNOWFLAKE_DATABASE','')}  "
        f"schema={sf_env.get('SNOWFLAKE_SCHEMA','')}"
    )

    # AI summary
    ai_key = ai_env.get("DIAL_API_KEY", "")
    ai_icon = f"{_C.GREEN}✓ ACTIVE{_C.RESET}" if ai_key else f"{_C.YELLOW}⚠ static fallback{_C.RESET}"
    print(f"\n  {_C.BOLD}AI / DIAL:{_C.RESET}  {ai_icon}  model={ai_env.get('DIAL_MODEL','gpt-4o')}")

    _blank()
    print(f"  Will write: {_C.GREEN}{env_path}{_C.RESET}")
    if env_path.exists():
        _warn("An existing .env file will be overwritten.")

    if not _yn("Save this configuration?"):
        _warn("Nothing was written.")
        return

    content = _build_env_content(connections, sf_env, ai_env)
    _write_env_file(env_path, content)
    _ok(f".env written ({len(connections)} source connection(s))")

    _blank()
    _sep("═")
    print(f"\n  {_C.BOLD}{_C.GREEN}Setup complete!{_C.RESET}  Next steps:\n")
    print(f"    {_C.GREEN}[1]{_C.RESET}  Single table   → cd src && python validate_cli.py generate")
    print(f"    {_C.CYAN}[2]{_C.RESET}  Multi-table    → cd src && python validate_cli.py batch --config tables.yaml")
    print(f"    {_C.CYAN}[3]{_C.RESET}  Connections    → cd src && python validate_cli.py connections")
    print(f"    {_C.DIM}[4]{_C.RESET}  Docs           → docs-v3/SETUP_GUIDE.md")
    _sep("═")
    _blank()


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_wizard(env_path: Optional[Path] = None) -> None:
    """
    Run the full enterprise setup wizard.

    Args:
        env_path: Where to write .env.  Defaults to <project_root>/.env
    """
    if env_path is None:
        env_path = _ROOT_DIR / ".env"

    _wizard_banner()

    if env_path.exists():
        _warn(f"Existing .env found at: {env_path}")
        if not _yn("Re-run wizard and overwrite it?", default=False):
            _info("Setup cancelled — existing .env kept.")
            return

    _info("Type  h + Enter  at any prompt for an explanation of that field.")
    _blank()

    # Step 1: number of connections
    num = _step_how_many()

    # Steps 2.x: collect each source connection
    connections: List[SourceConnection] = []
    for i in range(1, num + 1):
        conn = _collect_one_source(i, num, connections)
        connections.append(conn)

    # Step 3: Snowflake target
    sf_env = _collect_snowflake_target()

    # Step 4: AI / DIAL
    ai_env = _collect_ai_settings()

    # Step 5: review + write
    _blank()
    _sep("═")
    print(f"  {_C.BOLD}{_C.CYAN}STEP FINAL — Review & Save{_C.RESET}")
    _review_and_save(connections, sf_env, ai_env, env_path)


# ─────────────────────────────────────────────────────────────────────────────
# CLI utility: read existing .env and print the registry
# ─────────────────────────────────────────────────────────────────────────────

def print_connection_registry(env_path: Optional[Path] = None) -> List[Dict]:
    """
    Read SRC_N_* and SOURCE_* vars from .env and return a list of
    connection dicts — used by validate_cli.py to show the registry.

    Returns list of dicts with keys: index, db_type, host, port,
    database, schema, username, display_name, label.
    """
    if env_path is None:
        env_path = _ROOT_DIR / ".env"

    try:
        from dotenv import dotenv_values
        env = {**os.environ, **dotenv_values(env_path)}
    except ImportError:
        env = {}

    connections = []

    # Scan SRC_N_* first (new format)
    for i in range(1, 11):
        p = f"SRC_{i}_"
        db_type = env.get(f"{p}TYPE", "")
        if not db_type:
            continue
        db_label, _ = DB_TYPES.get(db_type, (db_type, 0))
        # Athena uses SRC_N_REGION instead of SRC_N_HOST
        if db_type.lower() in ("athena", "aws_athena"):
            host = env.get(f"{p}REGION", env.get(f"{p}HOST", ""))
        else:
            host = env.get(f"{p}HOST", "")
        port = env.get(f"{p}PORT", "")
        database = env.get(f"{p}DATABASE", "")
        schema = env.get(f"{p}SCHEMA", "")
        username = env.get(f"{p}USERNAME", "")
        auth = env.get(f"{p}AUTH", "")
        # Athena-specific: S3 result location (SRC_N_QUERY_RESULT_LOCATION or global ATHENA_S3_OUTPUT)
        s3_output = env.get(f"{p}QUERY_RESULT_LOCATION", env.get("ATHENA_S3_OUTPUT", ""))
        connections.append({
            "index":     i,
            "prefix":    p,
            "db_type":   db_type,
            "db_label":  db_label,
            "host":      host,
            "port":      port,
            "database":  database,
            "schema":    schema,
            "username":  username,
            "auth":      auth,
            "s3_output": s3_output,
            "label":     f"SRC_{i}  {db_label}  {host}:{port}/{database}.{schema}",
        })

    # Fall back to legacy SOURCE_* if no SRC_N_* found
    if not connections:
        db_type = env.get("SOURCE_TYPE", "postgresql")
        host = env.get("SOURCE_HOST", "")
        if host:
            db_label, _ = DB_TYPES.get(db_type, (db_type, 0))
            connections.append({
                "index":    1,
                "prefix":   "SOURCE_",
                "db_type":  db_type,
                "db_label": db_label,
                "host":     host,
                "port":     env.get("SOURCE_PORT", "5432"),
                "database": env.get("SOURCE_DATABASE", ""),
                "schema":   env.get("SOURCE_SCHEMA", "public"),
                "username": env.get("SOURCE_USERNAME", ""),
                "auth":     env.get("SOURCE_AUTH", ""),
                "label":    (
                    f"SRC_1  {db_label}  {host}:{env.get('SOURCE_PORT','5432')}/"
                    f"{env.get('SOURCE_DATABASE','')}.{env.get('SOURCE_SCHEMA','public')}"
                ),
            })

    return connections


if __name__ == "__main__":
    try:
        run_wizard()
    except KeyboardInterrupt:
        print(f"\n\n{_C.YELLOW}  Interrupted — .env not modified.{_C.RESET}\n")
        sys.exit(0)
