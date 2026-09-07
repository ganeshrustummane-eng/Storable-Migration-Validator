"""
Universal Extractor — All Database Schema Extractors in One File
=================================================================
Contains the abstract base, all DB-specific extractors, and the factory.
Add a new source DB by: (1) writing a new class below, (2) adding it to
_REGISTRY at the bottom. No other file needs changing.

Supported sources:
    postgresql / postgres / pg    → PostgresExtractor
    mssql / sqlserver / sql_server → MSSQLExtractor
    snowflake                     → SnowflakeExtractor
    athena / aws_athena           → AthenaExtractor
    redshift / aws_redshift       → RedshiftExtractor
"""

import importlib
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Shared data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ColumnMetadata:
    """Metadata for a single database column."""
    ordinal_position: int
    column_name: str
    data_type: str
    is_nullable: bool = True
    char_max_length: Optional[int] = None
    numeric_precision: Optional[int] = None
    numeric_scale: Optional[int] = None
    column_default: Optional[str] = None
    is_primary_key: bool = False
    pk_ordinal: Optional[int] = None

    @property
    def normalized_name(self) -> str:
        return re.sub(r"[^a-z0-9]", "", self.column_name.lower())

    @property
    def type_summary(self) -> str:
        dtype = self.data_type.upper()
        if self.char_max_length:
            return f"{dtype}({self.char_max_length})"
        if self.numeric_precision and self.numeric_scale is not None:
            return f"{dtype}({self.numeric_precision},{self.numeric_scale})"
        if self.numeric_precision:
            return f"{dtype}({self.numeric_precision})"
        return dtype

    @property
    def normalized_type(self) -> str:
        return re.sub(r"\s*\([^)]*\)", "", self.data_type).strip().upper()

    def __repr__(self) -> str:
        null_str = "NULL" if self.is_nullable else "NOT NULL"
        return f"Column({self.column_name}: {self.type_summary} {null_str})"


@dataclass
class TableMetadata:
    """Metadata for a database table including all its columns."""
    database: str
    schema: str
    table_name: str
    columns: List[ColumnMetadata] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.database}.{self.schema}.{self.table_name}"

    @property
    def column_names(self) -> List[str]:
        return [c.column_name for c in self.columns]

    def get_column(self, name: str) -> Optional[ColumnMetadata]:
        name_upper = name.upper()
        for col in self.columns:
            if col.column_name.upper() == name_upper:
                return col
        return None

    def __repr__(self) -> str:
        return f"Table({self.full_name}, {len(self.columns)} columns)"


@dataclass
class PrimaryKeyInfo:
    """Primary key metadata for a single table."""
    table_name: str
    columns: List[str] = field(default_factory=list)
    detected: bool = False
    detection_note: str = ""

    @property
    def is_composite(self) -> bool: return len(self.columns) > 1

    @property
    def has_pk(self) -> bool: return self.detected and bool(self.columns)

    def __repr__(self) -> str:
        if not self.has_pk:
            return f"PrimaryKey({self.table_name}: none)"
        return f"PrimaryKey({self.table_name}: {self.columns})"


class ExtractionError(Exception):
    """Raised when schema extraction fails."""
    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(message)
        self.original_error = original_error


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Abstract base
# ═══════════════════════════════════════════════════════════════════════════════

class BaseExtractor(ABC):
    """Abstract interface all extractors implement."""

    @abstractmethod
    def extract_columns(self, schema: str, table: str, database: Optional[str] = None) -> List[ColumnMetadata]: ...

    @abstractmethod
    def list_tables(self, schema: str) -> List[str]: ...

    def list_schemas(self) -> List[str]:
        """Return available schemas in the connected database. Override in subclasses."""
        return []

    def detect_primary_key(self, schema: str, table: str) -> PrimaryKeyInfo:
        return PrimaryKeyInfo(table_name=table, columns=[], detected=False,
                              detection_note="PK detection not implemented for this extractor")

    @staticmethod
    def has_fivetran_active(columns: List[ColumnMetadata]) -> bool:
        return any(col.column_name.upper() == "_FIVETRAN_ACTIVE" for col in columns)

    def extract_table(self, schema: str, table: str, database: str = "") -> TableMetadata:
        columns = self.extract_columns(schema, table)
        return TableMetadata(database=database, schema=schema,
                             table_name=table, columns=columns)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — PostgreSQL extractor
# ═══════════════════════════════════════════════════════════════════════════════

class PostgresExtractor(BaseExtractor):
    """Extracts schema metadata from PostgreSQL via psycopg2."""

    _COLUMNS_SQL = """
        SELECT ordinal_position, column_name, data_type, udt_name,
               character_maximum_length, numeric_precision, numeric_scale,
               CASE WHEN is_nullable = 'YES' THEN TRUE ELSE FALSE END AS is_nullable,
               column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position;
    """
    _TABLES_SQL = """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name;
    """
    _PK_SQL = """
        SELECT kcu.column_name, kcu.ordinal_position
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema    = kcu.table_schema
         AND tc.table_name      = kcu.table_name
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema    = %s AND tc.table_name = %s
        ORDER BY kcu.ordinal_position;
    """

    def __init__(self, host=None, port=None, database=None, username=None, password=None, **_):
        self.host     = host     or os.getenv("SOURCE_HOST",     "localhost")
        self.port     = int(port or os.getenv("SOURCE_PORT",     "5432"))
        self.database = database or os.getenv("SOURCE_DATABASE", "postgres")
        self.username = username or os.getenv("SOURCE_USERNAME", "postgres")
        self.password = password or os.getenv("SOURCE_PASSWORD", "")

    def _get_connection(self):
        try:
            import psycopg2
        except ImportError as e:
            raise ExtractionError("psycopg2 required. pip install psycopg2-binary", e)
        try:
            return psycopg2.connect(host=self.host, port=self.port, database=self.database,
                                    user=self.username, password=self.password, connect_timeout=15)
        except Exception as e:
            raise ExtractionError(f"Cannot connect to PostgreSQL {self.host}:{self.port}/{self.database}: {e}", e)

    def extract_columns(self, schema: str, table: str, database: Optional[str] = None) -> List[ColumnMetadata]:
        import psycopg2.extras
        active_db = self.database
        if database:
            self.database = database
        conn = self._get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(self._COLUMNS_SQL, (schema, table.lower()))
                rows = cur.fetchall()
        finally:
            conn.close()
            self.database = active_db
        if not rows:
            raise ExtractionError(f"No columns found for {schema}.{table} in PostgreSQL '{database or active_db}'.")
        columns = [self._row_to_column(dict(r)) for r in rows]
        print(f"  ✓ [PostgreSQL] Extracted {len(columns)} columns from {database or active_db}.{schema}.{table}")
        return columns

    def list_tables(self, schema: str) -> List[str]:
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(self._TABLES_SQL, (schema,))
                return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def list_schemas(self) -> List[str]:
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name NOT IN ('pg_catalog','information_schema','pg_toast') "
                    "ORDER BY schema_name"
                )
                return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def detect_primary_key(self, schema: str, table: str) -> PrimaryKeyInfo:
        try:
            import psycopg2.extras
            conn = self._get_connection()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(self._PK_SQL, (schema, table.lower()))
                    rows = cur.fetchall()
            finally:
                conn.close()
            cols = [r["column_name"] for r in rows]
            if cols:
                print(f"  ✓ [PostgreSQL] PK for {schema}.{table}: {cols}")
            return PrimaryKeyInfo(table_name=table, columns=cols, detected=True,
                                  detection_note="" if cols else "No PRIMARY KEY constraint found")
        except Exception as e:
            print(f"  ⚠ [PostgreSQL] PK detection failed for {schema}.{table}: {e}")
            return PrimaryKeyInfo(table_name=table, columns=[], detected=False,
                                  detection_note=f"Detection failed: {e}")

    @staticmethod
    def _row_to_column(row: dict) -> ColumnMetadata:
        nullable_val = row.get("is_nullable")
        is_nullable = nullable_val if isinstance(nullable_val, bool) else str(nullable_val).upper() in ("YES", "TRUE", "1")
        raw_type = row["data_type"]
        if raw_type.upper() == "USER-DEFINED":
            raw_type = row.get("udt_name", raw_type)
        return ColumnMetadata(
            ordinal_position=int(row["ordinal_position"]),
            column_name=row["column_name"], data_type=raw_type, is_nullable=is_nullable,
            char_max_length=int(row["character_maximum_length"]) if row.get("character_maximum_length") is not None else None,
            numeric_precision=int(row["numeric_precision"]) if row.get("numeric_precision") is not None else None,
            numeric_scale=int(row["numeric_scale"]) if row.get("numeric_scale") is not None else None,
            column_default=row.get("column_default"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — MSSQL extractor
# ═══════════════════════════════════════════════════════════════════════════════

def _normalise_mssql_type(raw: str) -> str:
    """Map MSSQL DATA_TYPE strings to PG-compatible names for rule matching."""
    mapping = {
        "nvarchar": "character varying", "varchar": "character varying",
        "nchar": "character", "char": "character", "ntext": "text",
        "datetime": "timestamp without time zone", "datetime2": "timestamp without time zone",
        "datetimeoffset": "timestamp with time zone", "smalldatetime": "timestamp without time zone",
        "date": "date", "time": "time without time zone",
        "bit": "boolean", "tinyint": "smallint", "int": "integer",
        "bigint": "bigint", "smallint": "smallint",
        "decimal": "numeric", "money": "numeric", "smallmoney": "numeric",
        "float": "double precision", "real": "real",
        "uniqueidentifier": "uuid", "varbinary": "bytea", "binary": "bytea",
        "xml": "json",
    }
    return mapping.get(raw.lower(), raw.lower())


class MSSQLExtractor(BaseExtractor):
    """Extracts schema metadata from Microsoft SQL Server via pyodbc."""

    _COLUMNS_SQL = """
        SELECT ORDINAL_POSITION AS ordinal_position, COLUMN_NAME AS column_name,
               DATA_TYPE AS data_type, CHARACTER_MAXIMUM_LENGTH AS character_maximum_length,
               NUMERIC_PRECISION AS numeric_precision, NUMERIC_SCALE AS numeric_scale,
               CASE WHEN IS_NULLABLE = 'YES' THEN 1 ELSE 0 END AS is_nullable,
               COLUMN_DEFAULT AS column_default
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION;
    """
    _TABLES_SQL = """
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME;
    """
    _PK_SQL = """
        SELECT kcu.COLUMN_NAME, kcu.ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
          ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
         AND tc.TABLE_NAME = kcu.TABLE_NAME
        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
        ORDER BY kcu.ORDINAL_POSITION;
    """

    def __init__(self, host=None, port=None, database=None, username=None, password=None,
                 driver=None, auth=None, **_):
        self.host     = host     or os.getenv("SOURCE_HOST",     "localhost")
        self.port     = int(port or os.getenv("SOURCE_PORT",     "1433"))
        self.database = database or os.getenv("SOURCE_DATABASE", "master")
        self.username = username or os.getenv("SOURCE_USERNAME", "")
        self.password = password or os.getenv("SOURCE_PASSWORD", "")
        self.driver   = driver   or os.getenv("MSSQL_DRIVER",    "{ODBC Driver 18 for SQL Server}")
        # auth="windows" → Trusted_Connection=yes (NTLM/Kerberos)
        # auth="sql" or ""  → UID/PWD (SQL Server authentication)
        raw_auth = (auth or os.getenv("MSSQL_AUTH", "")).lower().strip()
        self.auth = "windows" if raw_auth in ("windows", "win", "ntlm", "trusted") else "sql"

    def _get_connection(self):
        try:
            import pyodbc
        except ImportError as e:
            raise ExtractionError("pyodbc required. pip install pyodbc", e)
        base = (f"DRIVER={self.driver};SERVER={self.host},{self.port};"
                f"DATABASE={self.database};TrustServerCertificate=yes;Encrypt=optional;")
        if self.auth == "windows":
            conn_str = base + "Trusted_Connection=yes;"
        else:
            conn_str = base + f"UID={self.username};PWD={self.password};"
        try:
            return pyodbc.connect(conn_str, timeout=15)
        except Exception as e:
            raise ExtractionError(f"Cannot connect to MSSQL {self.host}:{self.port}/{self.database}: {e}", e)

    def extract_columns(self, schema: str, table: str, database: Optional[str] = None) -> List[ColumnMetadata]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(self._COLUMNS_SQL, (schema, table))
            col_names = [d[0].lower() for d in cur.description]
            rows = [dict(zip(col_names, row)) for row in cur.fetchall()]
        finally:
            conn.close()
        if not rows:
            raise ExtractionError(f"No columns found for {schema}.{table} in MSSQL '{self.database}'.")
        columns = [self._row_to_column(r) for r in rows]
        print(f"  ✓ [MSSQL] Extracted {len(columns)} columns from {self.database}.{schema}.{table}")
        return columns

    def list_tables(self, schema: str) -> List[str]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(self._TABLES_SQL, (schema,))
            return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def list_schemas(self) -> List[str]:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME")
            return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def detect_primary_key(self, schema: str, table: str) -> PrimaryKeyInfo:
        try:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(self._PK_SQL, (schema, table))
                rows = cur.fetchall()
            finally:
                conn.close()
            cols = [row[0] for row in rows]
            if cols:
                print(f"  ✓ [MSSQL] PK for {schema}.{table}: {cols}")
            return PrimaryKeyInfo(table_name=table, columns=cols, detected=True,
                                  detection_note="" if cols else "No PRIMARY KEY constraint found")
        except Exception as e:
            print(f"  ⚠ [MSSQL] PK detection failed for {schema}.{table}: {e}")
            return PrimaryKeyInfo(table_name=table, columns=[], detected=False,
                                  detection_note=f"Detection failed: {e}")

    @staticmethod
    def _row_to_column(row: dict) -> ColumnMetadata:
        nullable_val = row.get("is_nullable")
        if isinstance(nullable_val, bool):
            is_nullable = nullable_val
        elif isinstance(nullable_val, int):
            is_nullable = bool(nullable_val)
        else:
            is_nullable = str(nullable_val).upper() in ("YES", "TRUE", "1")
        return ColumnMetadata(
            ordinal_position=int(row["ordinal_position"]),
            column_name=row["column_name"],
            data_type=_normalise_mssql_type(row.get("data_type", "")),
            is_nullable=is_nullable,
            char_max_length=int(row["character_maximum_length"]) if row.get("character_maximum_length") not in (None, -1) else None,
            numeric_precision=int(row["numeric_precision"]) if row.get("numeric_precision") is not None else None,
            numeric_scale=int(row["numeric_scale"]) if row.get("numeric_scale") is not None else None,
            column_default=row.get("column_default"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Snowflake extractor
# ═══════════════════════════════════════════════════════════════════════════════

FIVETRAN_ACTIVE_COLUMN = "_FIVETRAN_ACTIVE"


class SnowflakeExtractor(BaseExtractor):
    """Extracts schema metadata from Snowflake. Detects Fivetran _FIVETRAN_ACTIVE column."""

    _COLUMNS_SQL = """
        SELECT ORDINAL_POSITION AS ordinal_position, COLUMN_NAME AS column_name,
               DATA_TYPE AS data_type, CHARACTER_MAXIMUM_LENGTH AS character_maximum_length,
               NUMERIC_PRECISION AS numeric_precision, NUMERIC_SCALE AS numeric_scale,
               CASE WHEN IS_NULLABLE = 'YES' THEN TRUE ELSE FALSE END AS is_nullable,
               COLUMN_DEFAULT AS column_default
        FROM {{database}}.INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION;
    """
    _TABLES_SQL = """
        SELECT TABLE_NAME FROM {{database}}.INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME;
    """

    def __init__(self, account=None, database=None, schema=None, username=None, password=None, **_):
        self.account  = account  or os.getenv("SNOWFLAKE_ACCOUNT",  "")
        self.database = database or os.getenv("SNOWFLAKE_DATABASE", "")
        self.schema   = schema   or os.getenv("SNOWFLAKE_SCHEMA",   "")
        self.username = username or os.getenv("SNOWFLAKE_USERNAME", "")
        self.password = password or os.getenv("SNOWFLAKE_PASSWORD", "")

    def _get_connection(self, database: Optional[str] = None):
        try:
            import snowflake.connector
        except ImportError as e:
            raise ExtractionError("snowflake-connector-python required. pip install snowflake-connector-python", e)
        try:
            return snowflake.connector.connect(user=self.username, password=self.password,
                                               account=self.account, database=database or self.database,
                                               login_timeout=30)
        except Exception as e:
            raise ExtractionError(f"Cannot connect to Snowflake account '{self.account}': {e}", e)

    def extract_columns(self, schema: str, table: str, database: Optional[str] = None) -> List[ColumnMetadata]:
        import snowflake.connector
        db = database or self.database
        conn = self._get_connection(db)
        sql = self._COLUMNS_SQL.replace("{{database}}", db)
        try:
            with conn.cursor(snowflake.connector.DictCursor) as cur:
                cur.execute(sql, (schema.upper(), table.upper()))
                rows = [{k.lower(): v for k, v in row.items()} for row in cur.fetchall()]
        finally:
            conn.close()
        if not rows:
            raise ExtractionError(f"No columns found for {schema}.{table} in Snowflake '{db}'.")
        columns = [self._row_to_column(r) for r in rows]
        print(f"  ✓ [Snowflake] Extracted {len(columns)} columns from {db}.{schema}.{table}")
        return columns

    def list_tables(self, schema: str) -> List[str]:
        import snowflake.connector
        conn = self._get_connection()
        sql = self._TABLES_SQL.replace("{{database}}", self.database)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (schema.upper(),))
                return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def list_schemas(self) -> List[str]:
        import snowflake.connector
        conn = self._get_connection()
        sql = (f"SELECT SCHEMA_NAME FROM {self.database}.INFORMATION_SCHEMA.SCHEMATA "
               f"WHERE SCHEMA_NAME != 'INFORMATION_SCHEMA' ORDER BY SCHEMA_NAME")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def detect_primary_key(self, schema: str, table: str, database: Optional[str] = None) -> PrimaryKeyInfo:
        import snowflake.connector
        db = database or self.database
        try:
            conn = self._get_connection(db)
            full = f"{db}.{schema.upper()}.{table.upper()}"
            try:
                with conn.cursor(snowflake.connector.DictCursor) as cur:
                    cur.execute(f"SHOW PRIMARY KEYS IN TABLE {full}")
                    rows = cur.fetchall()
            finally:
                conn.close()
            sorted_rows = sorted([{k.lower(): v for k, v in r.items()} for r in rows],
                                  key=lambda r: int(r.get("key_sequence", 1)))
            cols = [r["column_name"] for r in sorted_rows]
            if cols:
                print(f"  ✓ [Snowflake] PK for {schema}.{table}: {cols}")
            return PrimaryKeyInfo(table_name=table, columns=cols, detected=True,
                                  detection_note="" if cols else "No PRIMARY KEY constraint found")
        except Exception as e:
            print(f"  ⚠ [Snowflake] PK detection failed for {schema}.{table}: {e}")
            return PrimaryKeyInfo(table_name=table, columns=[], detected=False,
                                  detection_note=f"Detection failed: {e}")

    @staticmethod
    def has_fivetran_active(columns: List[ColumnMetadata]) -> bool:
        return any(col.column_name.upper() == FIVETRAN_ACTIVE_COLUMN for col in columns)

    @staticmethod
    def _row_to_column(row: dict) -> ColumnMetadata:
        nullable_val = row.get("is_nullable")
        is_nullable = nullable_val if isinstance(nullable_val, bool) else str(nullable_val).upper() in ("YES", "TRUE", "1")
        return ColumnMetadata(
            ordinal_position=int(row["ordinal_position"]),
            column_name=row["column_name"], data_type=row["data_type"], is_nullable=is_nullable,
            char_max_length=int(row["character_maximum_length"]) if row.get("character_maximum_length") is not None else None,
            numeric_precision=int(row["numeric_precision"]) if row.get("numeric_precision") is not None else None,
            numeric_scale=int(row["numeric_scale"]) if row.get("numeric_scale") is not None else None,
            column_default=row.get("column_default"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Athena extractor
# ═══════════════════════════════════════════════════════════════════════════════

_ATHENA_TYPE_MAP = {
    "varchar": "character varying", "string": "character varying", "char": "character",
    "tinyint": "smallint", "smallint": "smallint", "int": "integer", "integer": "integer",
    "bigint": "bigint", "float": "real", "real": "real", "double": "double precision",
    "decimal": "numeric", "boolean": "boolean", "date": "date",
    "timestamp": "timestamp without time zone", "binary": "bytea",
    "array": "array", "map": "json", "struct": "json", "json": "json",
}


def _normalise_athena_type(raw: str) -> str:
    base = re.sub(r"\s*\(.*\)$", "", raw.strip().lower())
    return _ATHENA_TYPE_MAP.get(base, base)


class AthenaExtractor(BaseExtractor):
    """Extracts schema metadata from AWS Athena via Glue Data Catalog using pyathena."""

    def __init__(self, database=None, region=None, s3_output=None, catalog=None,
                 workgroup=None, access_key=None, secret_key=None,
                 host=None, port=None, username=None, password=None, **_):
        self.database  = database  or os.getenv("SOURCE_DATABASE",   os.getenv("ATHENA_DATABASE", ""))
        self.region    = region    or host or os.getenv("ATHENA_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        self.s3_output = s3_output or os.getenv("ATHENA_S3_OUTPUT",  "")
        self.catalog   = catalog   or os.getenv("ATHENA_CATALOG",    "AwsDataCatalog")
        self.workgroup = workgroup  or os.getenv("ATHENA_WORKGROUP",  "primary")
        self.access_key = (access_key or username
                           or os.getenv("SOURCE_USERNAME", os.getenv("AWS_ACCESS_KEY_ID", "")))
        self.secret_key = (secret_key or password
                           or os.getenv("SOURCE_PASSWORD", os.getenv("AWS_SECRET_ACCESS_KEY", "")))

    def _get_connection(self):
        if not self.s3_output:
            raise ExtractionError("ATHENA_S3_OUTPUT is required. Set it to an S3 URI in your .env file.")
        try:
            import pyathena
        except ImportError as e:
            raise ExtractionError("pyathena required. pip install pyathena", e)
        kwargs = dict(region_name=self.region, s3_staging_dir=self.s3_output,
                      catalog_name=self.catalog, work_group=self.workgroup,
                      schema_name=self.database)
        if self.access_key and self.secret_key:
            kwargs["aws_access_key_id"]     = self.access_key
            kwargs["aws_secret_access_key"] = self.secret_key
        try:
            return pyathena.connect(**kwargs)
        except Exception as e:
            raise ExtractionError(f"Cannot connect to Athena (region={self.region}): {e}", e)

    def _run_query(self, sql: str) -> list:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            if cur.description is None:
                return []
            cols = [d[0].lower() for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def extract_columns(self, schema: str, table: str, database: Optional[str] = None) -> List[ColumnMetadata]:
        # Athena's INFORMATION_SCHEMA.COLUMNS only exposes a subset of standard columns —
        # character_maximum_length, numeric_precision, numeric_scale, column_default are absent.
        sql = f"""
            SELECT ordinal_position, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = '{schema.lower()}' AND table_name = '{table.lower()}'
            ORDER BY ordinal_position
        """
        try:
            rows = self._run_query(sql)
        except ExtractionError:
            raise
        except Exception as e:
            raise ExtractionError(f"Failed to query INFORMATION_SCHEMA for {schema}.{table}: {e}", e)
        if not rows:
            raise ExtractionError(f"No columns found for {schema}.{table} in Athena catalog '{self.catalog}'.")
        columns = [self._row_to_column(r) for r in rows]
        print(f"  ✓ [Athena] Extracted {len(columns)} columns from {self.catalog}.{schema}.{table}")
        return columns

    def list_tables(self, schema: str) -> List[str]:
        # Use Glue Data Catalog via boto3 — avoids Athena workgroup permission requirements.
        try:
            import boto3
            kwargs = dict(region_name=self.region)
            if self.access_key and self.secret_key:
                kwargs["aws_access_key_id"]     = self.access_key
                kwargs["aws_secret_access_key"] = self.secret_key
            glue = boto3.client("glue", **kwargs)
            paginator = glue.get_paginator("get_tables")
            tables = []
            for page in paginator.paginate(DatabaseName=schema or self.database):
                tables.extend(t["Name"] for t in page.get("TableList", []))
            return sorted(tables)
        except ImportError:
            pass
        except Exception as e:
            raise ExtractionError(f"Failed to list tables via Glue for '{schema}': {e}", e)

        # Fallback: Athena SQL (requires workgroup execute permissions)
        sql = f"""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = '{schema.lower()}'
              AND table_type IN ('BASE TABLE', 'EXTERNAL TABLE')
            ORDER BY table_name
        """
        try:
            rows = self._run_query(sql)
        except ExtractionError:
            raise
        except Exception as e:
            raise ExtractionError(f"Failed to list tables in Athena schema '{schema}': {e}", e)
        return [r["table_name"] for r in rows]

    def list_schemas(self) -> List[str]:
        # Use Glue Data Catalog to list databases (= schemas for Athena).
        try:
            import boto3
            kwargs = dict(region_name=self.region)
            if self.access_key and self.secret_key:
                kwargs["aws_access_key_id"]     = self.access_key
                kwargs["aws_secret_access_key"] = self.secret_key
            glue = boto3.client("glue", **kwargs)
            paginator = glue.get_paginator("get_databases")
            dbs = []
            for page in paginator.paginate():
                dbs.extend(d["Name"] for d in page.get("DatabaseList", []))
            return sorted(dbs) if dbs else ([self.database] if self.database else [])
        except Exception:
            return [self.database] if self.database else []

    def detect_primary_key(self, schema: str, table: str) -> PrimaryKeyInfo:
        return PrimaryKeyInfo(table_name=table, columns=[], detected=False,
                              detection_note="Athena (S3-backed) tables have no primary key constraints")

    @staticmethod
    def _row_to_column(row: dict) -> ColumnMetadata:
        nullable_val = row.get("is_nullable", "YES")
        is_nullable = nullable_val if isinstance(nullable_val, bool) else str(nullable_val).upper() in ("YES", "TRUE", "1")
        char_max = row.get("character_maximum_length")
        num_prec = row.get("numeric_precision")
        num_scal = row.get("numeric_scale")
        return ColumnMetadata(
            ordinal_position=int(row.get("ordinal_position", 1)),
            column_name=row["column_name"],
            data_type=_normalise_athena_type(row.get("data_type", "")),
            is_nullable=is_nullable,
            char_max_length=int(char_max) if char_max is not None else None,
            numeric_precision=int(num_prec) if num_prec is not None else None,
            numeric_scale=int(num_scal) if num_scal is not None else None,
            column_default=row.get("column_default"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6b — Redshift extractor (Postgres wire protocol, different type names)
# ═══════════════════════════════════════════════════════════════════════════════

_REDSHIFT_TYPE_MAP = {
    "super": "json",       # semi-structured type — closest PG-compatible bucket
    "varbyte": "bytea",
    "hllsketch": "text",
    "geometry": "text",
    "geography": "text",
}


def _normalise_redshift_type(raw: str) -> str:
    """Redshift's information_schema reports standard types (varchar, int4,
    int8, numeric, timestamp, boolean, ...) identically to PostgreSQL — only
    Redshift-only types need mapping to a PostgreSQL-compatible name."""
    return _REDSHIFT_TYPE_MAP.get(raw.lower().strip(), raw)


class RedshiftExtractor(PostgresExtractor):
    """Redshift speaks the PostgreSQL wire protocol and shares Postgres's
    information_schema shape (including PK constraint metadata) — only type
    normalization differs."""

    @staticmethod
    def _row_to_column(row: dict) -> ColumnMetadata:
        col = PostgresExtractor._row_to_column(row)
        col.data_type = _normalise_redshift_type(col.data_type)
        return col


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Factory
# ═══════════════════════════════════════════════════════════════════════════════

# Registry: db_type alias → (module_path, class_name)
# All classes are in this file — module_path is this module itself.
_REGISTRY = {
    "postgresql": ("sql_extractor.extractors", "PostgresExtractor"),
    "postgres":   ("sql_extractor.extractors", "PostgresExtractor"),
    "pg":         ("sql_extractor.extractors", "PostgresExtractor"),
    "mssql":      ("sql_extractor.extractors", "MSSQLExtractor"),
    "sqlserver":  ("sql_extractor.extractors", "MSSQLExtractor"),
    "sql_server": ("sql_extractor.extractors", "MSSQLExtractor"),
    "snowflake":  ("sql_extractor.extractors", "SnowflakeExtractor"),
    "athena":     ("sql_extractor.extractors", "AthenaExtractor"),
    "aws_athena": ("sql_extractor.extractors", "AthenaExtractor"),
    "redshift":     ("sql_extractor.extractors", "RedshiftExtractor"),
    "aws_redshift": ("sql_extractor.extractors", "RedshiftExtractor"),
}

_DEFAULT_PORTS = {
    "postgresql": 5432, "postgres": 5432, "pg": 5432,
    "mssql": 1433, "sqlserver": 1433, "sql_server": 1433,
    "snowflake": 443, "athena": 443, "aws_athena": 443,
    "redshift": 5439, "aws_redshift": 5439,
}


class ExtractorFactory:
    """Instantiates the correct BaseExtractor subclass from a db_type string."""

    @staticmethod
    def supported_types() -> list:
        return sorted(set(_REGISTRY.keys()))

    @staticmethod
    def create(db_type: str, **kwargs) -> BaseExtractor:
        key = db_type.lower().strip()
        if key not in _REGISTRY:
            raise ValueError(f"Unsupported db type '{db_type}'. Supported: {', '.join(sorted(_REGISTRY))}")
        module_path, class_name = _REGISTRY[key]
        module = importlib.import_module(module_path)
        return getattr(module, class_name)(**kwargs)

    @staticmethod
    def create_from_env(prefix: Optional[int] = None) -> BaseExtractor:
        p = f"SOURCE_{prefix}_" if prefix is not None else "SOURCE_"
        db_type  = os.getenv(f"{p}TYPE",     "postgresql")
        host     = os.getenv(f"{p}HOST",     os.getenv("SOURCE_HOST", "localhost"))
        port_str = os.getenv(f"{p}PORT",     os.getenv("SOURCE_PORT", "5432"))
        database = os.getenv(f"{p}DATABASE", os.getenv("SOURCE_DATABASE", ""))
        username = os.getenv(f"{p}USERNAME", os.getenv("SOURCE_USERNAME", ""))
        password = os.getenv(f"{p}PASSWORD", os.getenv("SOURCE_PASSWORD", ""))
        try:
            port = int(port_str)
        except (TypeError, ValueError):
            port = _DEFAULT_PORTS.get(db_type.lower(), 5432)
        return ExtractorFactory.create(db_type, host=host, port=port,
                                       database=database, username=username, password=password)

    @staticmethod
    def list_env_sources() -> list:
        results = []
        default_type = os.getenv("SOURCE_TYPE")
        if default_type:
            results.append((None, default_type))
        for i in range(1, 20):
            t = os.getenv(f"SOURCE_{i}_TYPE")
            if t:
                results.append((i, t))
        return results
