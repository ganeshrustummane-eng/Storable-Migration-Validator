"""
PostgreSQL → Snowflake Transformation Rules
============================================
All type-specific rules for normalizing PostgreSQL (and MSSQL/Athena — their
extractors already map types to PG-compatible names) column values before
comparing them with Snowflake.

Rule application order (innermost → outermost):
  1. integer / uuid / json / bytea / hstore
  2. boolean
  3. timestamp_tz → timestamp_ntz → date
  4. numeric
  5. text (trim) / fallback
  6. null placeholder  ← always LAST (outermost, inherited by base class)
"""

import re
from abc import ABC, abstractmethod

from typing import List, Optional, Tuple

# ── NULL placeholder ─────────────────────────────────────────────────────────
NULL_PLACEHOLDER = "<<NULL>>"


# ── Abstract base ─────────────────────────────────────────────────────────────

class BaseValidationRule(ABC):
    """Abstract base for all validation rules. No source database is optional:
    every concrete rule MUST implement all four dialect expression methods
    below (_pg_expression, _ms_expression, _athena_expression, _sf_expression)
    — Python enforces this at class-definition time, since PostgreSQL, MSSQL,
    and Athena syntax genuinely differ (e.g. TO_CHAR/::jsonb/encode() are not
    valid MSSQL or Athena syntax). The public API wraps each dialect's
    expression with COALESCE(CAST(… AS TEXT/STRING), '<<NULL>>')."""

    @property
    @abstractmethod
    def rule_name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def trigger_pairs(self) -> List[Tuple[str, str]]: ...

    @abstractmethod
    def _pg_expression(self, col: str) -> str: ...

    @abstractmethod
    def _sf_expression(self, col: str) -> str: ...

    # ── MSSQL (SQL Server) source expression — REQUIRED, not optional ────
    # PG syntax (TO_CHAR, CAST AS TEXT, encode(), ::jsonb, etc.) is often not
    # valid on SQL Server, so every rule must supply its own MSSQL-correct
    # expression rather than silently inheriting PostgreSQL syntax.
    @abstractmethod
    def _ms_expression(self, col: str) -> str: ...

    # ── Athena (Trino/Presto) source expression — REQUIRED, not optional ──
    # Trino/Presto syntax also differs from PostgreSQL (e.g. date_format vs
    # TO_CHAR), so every rule must supply its own Athena-correct expression.
    @abstractmethod
    def _athena_expression(self, col: str) -> str: ...

    def apply_postgresql(self, col: str, alias: Optional[str] = None) -> str:
        wrapped = self._coalesce_pg(self._pg_expression(col))
        return f"{wrapped} AS {alias}" if alias else wrapped

    def apply_mssql(self, col: str, alias: Optional[str] = None) -> str:
        wrapped = self._coalesce_ms(self._ms_expression(col))
        return f"{wrapped} AS {alias}" if alias else wrapped

    def apply_athena(self, col: str, alias: Optional[str] = None) -> str:
        wrapped = self._coalesce_athena(self._athena_expression(col))
        return f"{wrapped} AS {alias}" if alias else wrapped

    def apply_snowflake(self, col: str, alias: Optional[str] = None) -> str:
        wrapped = self._coalesce_sf(self._sf_expression(col))
        return f"{wrapped} AS {alias}" if alias else wrapped

    def apply_source(
        self, source_db_type: str, col: str, alias: Optional[str] = None
    ) -> str:
        """Dispatch to the correct source-database dialect.

        Args:
            source_db_type: 'postgresql' | 'postgres' | 'mssql' | 'sqlserver'
                            | 'athena' | 'trino' | 'presto' | 'snowflake'
            col           : Column name to normalize
            alias         : Optional SELECT alias

        Returns:
            A COALESCE-wrapped, dialect-correct SQL expression.
        """
        db = (source_db_type or "postgresql").strip().lower()
        if db in ("mssql", "sqlserver", "sql_server", "mssqlserver"):
            return self.apply_mssql(col, alias)
        if db in ("athena", "trino", "presto"):
            return self.apply_athena(col, alias)
        if db in ("snowflake",):
            return self.apply_snowflake(col, alias)
        # postgres / postgresql / default
        return self.apply_postgresql(col, alias)

    @property
    def is_skip_rule(self) -> bool:
        return False

    # When True, the Snowflake expression cannot be a scalar subquery because
    # LATERAL FLATTEN inside a correlated scalar subquery is unsupported.
    # Rules that set this must also implement snowflake_cte_sql(),
    # snowflake_cte_join_clause(), and snowflake_cte_select_expr().
    snowflake_needs_cte: bool = False

    @staticmethod
    def _coalesce_pg(expr: str) -> str:
        return f"COALESCE(CAST({expr} AS TEXT), '{NULL_PLACEHOLDER}')"

    @staticmethod
    def _coalesce_ms(expr: str) -> str:
        # SQL Server has no TEXT cast target for comparison; use VARCHAR(MAX).
        return f"COALESCE(CAST({expr} AS VARCHAR(MAX)), '{NULL_PLACEHOLDER}')"

    @staticmethod
    def _coalesce_athena(expr: str) -> str:
        # Athena/Trino uses VARCHAR (no TEXT type).
        return f"COALESCE(CAST({expr} AS VARCHAR), '{NULL_PLACEHOLDER}')"

    @staticmethod
    def _coalesce_sf(expr: str) -> str:
        return f"COALESCE(CAST({expr} AS STRING), '{NULL_PLACEHOLDER}')"

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} rule='{self.rule_name}'>"


# ── Rule Registry ─────────────────────────────────────────────────────────────

class RuleRegistry:
    """Maps (pg_type, sf_type) pairs to validation rules — first match wins."""

    def __init__(self):
        self._rules: List[BaseValidationRule] = []
        self._default: Optional[BaseValidationRule] = None

    def register(self, rule: BaseValidationRule):
        self._rules.append(rule)
        if rule.rule_name == "text":
            self._default = rule

    def lookup(self, pg_type: str, sf_type: str) -> BaseValidationRule:
        pg_norm = _normalize_type(pg_type)
        sf_norm = _normalize_type(sf_type)
        for rule in self._rules:
            for pg_pat, sf_pat in rule.trigger_pairs:
                if _type_matches(pg_norm, pg_pat) and _type_matches(sf_norm, sf_pat):
                    return rule
        return self._default or (self._rules[0] if self._rules else _NoOpRule())

    def lookup_specific(self, pg_type: str, sf_type: str) -> Optional[BaseValidationRule]:
        """
        Same as lookup(), but ignores wildcard ("*", "*") trigger pairs.

        Returns None when no rule has a SPECIFIC type-pair match — i.e. the
        type pair would otherwise only be caught by TextRule's ("*", "*")
        catch-all. This is the seam the learned-rules gap-filler layer uses:
        it is only ever consulted here, so it can never shadow a rule that
        already owns this type pair specifically.
        """
        pg_norm = _normalize_type(pg_type)
        sf_norm = _normalize_type(sf_type)
        for rule in self._rules:
            for pg_pat, sf_pat in rule.trigger_pairs:
                if pg_pat == "*" and sf_pat == "*":
                    continue
                if _type_matches(pg_norm, pg_pat) and _type_matches(sf_norm, sf_pat):
                    return rule
        return None

    def get_by_name(self, rule_name: str) -> Optional[BaseValidationRule]:
        """Look up a registered base rule by its rule_name (e.g. 'text', 'boolean')."""
        for rule in self._rules:
            if rule.rule_name == rule_name:
                return rule
        return None

    def all_rules(self) -> List[BaseValidationRule]:
        return list(self._rules)


def _normalize_type(type_str: str) -> str:
    if not type_str:
        return ""
    normalized = re.sub(r"\([^)]*\)", "", type_str)
    normalized = re.sub(r"\s+without\s+time\s+zone", "_NTZ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+with\s+time\s+zone",    "_TZ",  normalized, flags=re.IGNORECASE)
    return normalized.strip().upper()


def _type_matches(type_str: str, pattern: str) -> bool:
    if pattern == "*":
        return True
    return type_str.upper().startswith(pattern.upper())


class _NoOpRule(BaseValidationRule):
    @property
    def rule_name(self) -> str: return "noop"
    @property
    def description(self) -> str: return "No-op fallback."
    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]: return [("*", "*")]
    def _pg_expression(self, col: str) -> str: return f"CAST({col} AS TEXT)"
    def _sf_expression(self, col: str) -> str: return f"CAST({col} AS STRING)"
    def _ms_expression(self, col: str) -> str: return f"CAST({col} AS VARCHAR(MAX))"
    def _athena_expression(self, col: str) -> str: return f"CAST({col} AS VARCHAR)"


# ── Concrete Rules ────────────────────────────────────────────────────────────

class BooleanRule(BaseValidationRule):
    """Boolean: TRUE→'1', FALSE→'0'. NULL→'<<NULL>>'."""

    @property
    def rule_name(self) -> str: return "boolean"

    @property
    def description(self) -> str:
        return "Boolean: TRUE→'1', FALSE→'0'. NULL→'<<NULL>>'."

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [("BOOLEAN", "BOOLEAN"), ("BOOLEAN", "BOOL"),
                ("BOOL", "BOOLEAN"),   ("BOOL", "BOOL")]

    def _pg_expression(self, col: str) -> str:
        return (f"CASE WHEN {col} = true THEN '1' "
                f"WHEN {col} = false THEN '0' ELSE NULL END")

    def _sf_expression(self, col: str) -> str:
        return (f"CASE WHEN {col} = TRUE THEN '1' "
                f"WHEN {col} = FALSE THEN '0' ELSE NULL END")

    def _ms_expression(self, col: str) -> str:
        # SQL Server BIT: 1/0 (no true/false literals).
        return (f"CASE WHEN {col} = 1 THEN '1' "
                f"WHEN {col} = 0 THEN '0' ELSE NULL END")

    def _athena_expression(self, col: str) -> str:
        return (f"CASE WHEN {col} = true THEN '1' "
                f"WHEN {col} = false THEN '0' ELSE NULL END")


class IntegerRule(BaseValidationRule):
    """Integer types: cast to text for cross-system comparison. NULL→'<<NULL>>'."""

    @property
    def rule_name(self) -> str: return "integer"

    @property
    def description(self) -> str:
        return "Integer: cast to text. Handles SMALLINT/INT/BIGINT/SERIAL→NUMBER. NULL→'<<NULL>>'."

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [("SMALLINT", "NUMBER"), ("SMALLINT", "INTEGER"),
                ("INTEGER",  "NUMBER"), ("INTEGER",  "INTEGER"),
                ("INT",      "NUMBER"), ("INT",      "INTEGER"),
                ("BIGINT",   "NUMBER"), ("BIGINT",   "INTEGER"),
                ("SERIAL",   "NUMBER"), ("SERIAL",   "INTEGER"),
                ("BIGSERIAL","NUMBER"), ("BIGSERIAL","INTEGER")]

    def _pg_expression(self, col: str) -> str: return f"CAST({col} AS TEXT)"
    def _sf_expression(self, col: str) -> str: return f"CAST({col} AS STRING)"
    def _ms_expression(self, col: str) -> str: return f"CAST({col} AS VARCHAR(MAX))"
    def _athena_expression(self, col: str) -> str: return f"CAST({col} AS VARCHAR)"


DEFAULT_DECIMAL_PLACES: int = 2  # kept for backward-compat imports; no longer used to round




class NumericRule(BaseValidationRule):
    """Numeric/decimal: compare at full native precision, no rounding. NULL→'<<NULL>>'."""

    @property
    def rule_name(self) -> str: return "numeric"

    @property
    def description(self) -> str:
        return "Numeric: cast to text at full native precision (no rounding). NULL→'<<NULL>>'."

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [("NUMERIC",          "NUMBER"),  ("NUMERIC",          "NUMERIC"),
                ("DECIMAL",          "NUMBER"),  ("DECIMAL",          "DECIMAL"),
                ("FLOAT",            "FLOAT"),   ("REAL",             "FLOAT"),
                ("DOUBLE PRECISION", "FLOAT"),   ("DOUBLE PRECISION", "NUMBER"),
                ("MONEY",            "NUMBER"),  ("MONEY",            "NUMERIC")]

    def _pg_expression(self, col: str) -> str:
        # CAST to NUMERIC first strips MONEY's currency formatting ('$100.00' → 100.00)
        # without touching precision.
        return f"CAST(CAST({col} AS NUMERIC) AS TEXT)"

    def _sf_expression(self, col: str) -> str:
        return f"CAST({col} AS STRING)"

    def _ms_expression(self, col: str) -> str:
        return f"CAST({col} AS VARCHAR(MAX))"

    def _athena_expression(self, col: str) -> str:
        return f"CAST({col} AS VARCHAR)"


class TimestampTZRule(BaseValidationRule):
    """Timestamp TZ: convert to UTC then format to microsecond precision. NULL→'<<NULL>>'."""

    @property
    def rule_name(self) -> str: return "timestamp_tz"

    @property
    def description(self) -> str:
        return "Timestamp TZ: UTC normalize, format to microsecond precision. NULL→'<<NULL>>'."

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [("TIMESTAMP_TZ", "TIMESTAMP_TZ"),
                ("TIMESTAMP_TZ", "TIMESTAMPTZ"),
                ("TIMESTAMPTZ",  "TIMESTAMP_TZ")]

    def _pg_expression(self, col: str) -> str:
        return f"TO_CHAR({col} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')"

    def _sf_expression(self, col: str) -> str:
        return f"TO_VARCHAR(CONVERT_TIMEZONE('UTC', {col}), 'YYYY-MM-DD HH24:MI:SS.FF6')"

    def _ms_expression(self, col: str) -> str:
        # SQL Server: shift to UTC then format (24-hour clock, 6-digit fractional seconds).
        return f"FORMAT({col} AT TIME ZONE 'UTC', 'yyyy-MM-dd HH:mm:ss.ffffff')"

    def _athena_expression(self, col: str) -> str:
        # Trino/Athena: normalize to UTC then format (%f = 6-digit microseconds).
        return f"date_format(at_timezone({col}, 'UTC'), '%Y-%m-%d %H:%i:%s.%f')"


class TimestampNTZRule(BaseValidationRule):
    """Timestamp NTZ: format to microsecond precision. NULL→'<<NULL>>'."""

    @property
    def rule_name(self) -> str: return "timestamp_ntz"

    @property
    def description(self) -> str:
        return "Timestamp NTZ: format to microsecond precision. NULL→'<<NULL>>'."

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [("TIMESTAMP",     "TIMESTAMP_NTZ"),
                ("TIMESTAMP_NTZ", "TIMESTAMP_NTZ"),
                ("TIMESTAMP",     "TIMESTAMP"),
                ("TIMESTAMP_NTZ", "TIMESTAMP"),
                ("DATETIME",        "TIMESTAMP_NTZ")]

    def _pg_expression(self, col: str) -> str:
        return f"TO_CHAR({col}, 'YYYY-MM-DD HH24:MI:SS.US')"

    def _sf_expression(self, col: str) -> str:
        return f"TO_VARCHAR({col}, 'YYYY-MM-DD HH24:MI:SS.FF6')"

    def _ms_expression(self, col: str) -> str:
        return f"FORMAT({col}, 'yyyy-MM-dd HH:mm:ss.ffffff')"

    def _athena_expression(self, col: str) -> str:
        return f"date_format({col}, '%Y-%m-%d %H:%i:%s.%f')"


class DateRule(BaseValidationRule):
    """Date: format as 'YYYY-MM-DD'. NULL→'<<NULL>>'."""

    @property
    def rule_name(self) -> str: return "date"

    @property
    def description(self) -> str: return "Date: format YYYY-MM-DD. NULL→'<<NULL>>'."

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [("DATE", "DATE")]

    def _pg_expression(self, col: str) -> str: return f"TO_CHAR({col}, 'YYYY-MM-DD')"
    def _sf_expression(self, col: str) -> str: return f"TO_VARCHAR({col}, 'YYYY-MM-DD')"
    def _ms_expression(self, col: str) -> str: return f"FORMAT({col}, 'yyyy-MM-dd')"
    def _athena_expression(self, col: str) -> str: return f"date_format({col}, '%Y-%m-%d')"


class TextRule(BaseValidationRule):
    """Text/VARCHAR: TRIM whitespace. Wildcard fallback for all unmatched types. NULL→'<<NULL>>'."""

    @property
    def rule_name(self) -> str: return "text"

    @property
    def description(self) -> str:
        return "Text: trim leading/trailing spaces. Empty string stays empty. NULL→'<<NULL>>'."

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [("CHARACTER VARYING", "TEXT"),    ("CHARACTER VARYING", "VARCHAR"),
                ("CHARACTER VARYING", "STRING"),  ("VARCHAR",           "VARCHAR"),
                ("VARCHAR",           "STRING"),  ("VARCHAR",           "TEXT"),
                ("CHAR",              "CHAR"),    ("CHAR",              "VARCHAR"),
                ("CHAR",              "STRING"),  ("TEXT",              "TEXT"),
                ("TEXT",              "VARCHAR"), ("TEXT",              "STRING"),
                ("*",                 "*")]       # wildcard fallback — must be LAST

    def _pg_expression(self, col: str) -> str: return f"TRIM({col})"
    def _sf_expression(self, col: str) -> str: return f"TRIM({col})"
    def _ms_expression(self, col: str) -> str: return f"LTRIM(RTRIM({col}))"
    def _athena_expression(self, col: str) -> str: return f"TRIM({col})"


class UUIDRule(BaseValidationRule):
    """UUID: UPPER(TRIM(CAST AS TEXT)) — case-insensitive per RFC 4122. NULL→'<<NULL>>'."""

    @property
    def rule_name(self) -> str: return "uuid"

    @property
    def description(self) -> str:
        return "UUID: UPPER(TRIM()), case-insensitive comparison. Handles PG UUID→SF VARCHAR/STRING. NULL→'<<NULL>>'."

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [("UUID", "TEXT"), ("UUID", "VARCHAR"),
                ("UUID", "STRING"), ("UUID", "UUID")]

    def _pg_expression(self, col: str) -> str: return f"UPPER(TRIM(CAST({col} AS TEXT)))"
    def _sf_expression(self, col: str) -> str: return f"UPPER(TRIM(CAST({col} AS STRING)))"
    def _ms_expression(self, col: str) -> str: return f"UPPER(LTRIM(RTRIM(CAST({col} AS VARCHAR(MAX)))))"
    def _athena_expression(self, col: str) -> str: return f"UPPER(TRIM(CAST({col} AS VARCHAR)))"


class JSONRule(BaseValidationRule):
    """JSON/JSONB → raw document text; canonicalized in Python, not in SQL.

    Both sides emit the document as-is and
    Project/utils/semantic_normalize.canonicalize_value() parses, recursively
    sorts and re-serializes it before comparison.

    Why not canonicalize in SQL:
      An earlier version built a byte-identical canonical string in SQL on both
      sides — a recursive CTE with string_agg(... ORDER BY path) on Postgres,
      LATERAL FLATTEN + LISTAGG(... WITHIN GROUP (ORDER BY f.path)) on
      Snowflake. Two engines cannot be relied on to agree, and they did not:

        * Snowflake LISTAGG returns '' (not NULL) when every input is NULL, so
          COALESCE(_listagg, _fallback) always picked '' and the NULL/empty
          sentinel fallback was dead code. A NULL column read '<<EMPTY>>' on
          the source and '' on the target — a guaranteed false mismatch.
        * ORDER BY collation differs: Postgres uses the database collation
          (en_US.UTF-8 ignores '_' and case at the primary level), Snowflake
          orders by codepoint. Mixed-case keys sorted 'apple, Zebra' on one
          side and 'Zebra, apple' on the other.
        * jsonb_each() hard-errors on a top-level array or scalar. The
          recursive term evaluated it unconditionally, so one row holding
          [1,2,3] aborted the whole source query.
        * A value that is a *string containing JSON* was compared byte-for-byte,
          so any re-serialization by the loader read as data drift.
        * path=value pairs joined by '|' were ambiguous for values containing
          '=' or '|'.

      Canonicalizing once in Python makes symmetry structural instead of
      something two dialects have to coincidentally agree on.

    Why no TRIM():
      Whitespace inside a JSON string value is semantically significant. The
      inherited COALESCE cast is the only wrapper applied.
    """

    @property
    def rule_name(self) -> str: return "json"

    @property
    def description(self) -> str:
        return (
            "JSON/JSONB → raw document text; canonicalized in Python "
            "(recursive key sort, JSON-in-string recursion, number "
            "normalization) before comparison. SQL NULL → '<<NULL>>'."
        )

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [
            ("JSON",  "VARIANT"), ("JSON",  "VARCHAR"), ("JSON",  "STRING"), ("JSON",  "TEXT"),
            ("JSONB", "VARIANT"), ("JSONB", "VARCHAR"), ("JSONB", "STRING"), ("JSONB", "TEXT"),
            ("JSON",  "ARRAY"),   ("JSONB", "ARRAY"),
        ]

    def _pg_expression(self, col: str) -> str:
        # Raw document text. No jsonb_each()/jsonb_array_elements() traversal,
        # so top-level arrays and scalars are handled like anything else instead
        # of aborting the query.
        return f"CAST({col} AS TEXT)"

    def _sf_expression(self, col: str) -> str:
        # Works whether the target column is VARIANT or VARCHAR, without needing
        # to know which at generation time:
        #   VARCHAR holding JSON text -> TRY_PARSE_JSON parses it -> compact JSON
        #   VARIANT object/array      -> CAST yields its (pretty-printed) JSON
        #                                text, re-parsed -> compact JSON
        #   non-JSON text (e.g. a native hstore literal "k"=>"v", or a bare
        #                                scalar string) -> TRY_PARSE_JSON is
        #                                NULL, so the raw text passes through
        #                                for Python to handle
        #   SQL NULL                  -> NULL -> '<<NULL>>' via the wrapper
        #
        # TYPEOF() is deliberately NOT used: it rejects VARCHAR outright
        # ("Invalid argument types for function 'TYPEOF': (VARCHAR)"), and these
        # columns are frequently landed as VARCHAR rather than VARIANT.
        # Verified against live Snowflake.
        return (
            f"COALESCE(TO_JSON(TRY_PARSE_JSON(CAST({col} AS STRING))), "
            f"CAST({col} AS STRING))"
        )

    def _ms_expression(self, col: str) -> str:
        return f"CAST({col} AS NVARCHAR(MAX))"

    def _athena_expression(self, col: str) -> str:
        return f"CAST({col} AS VARCHAR)"


class ByteaRule(BaseValidationRule):
    """Binary/BYTEA: hex encoding for cross-system comparison. NULL→'<<NULL>>'."""

    @property
    def rule_name(self) -> str: return "bytea"

    @property
    def description(self) -> str:
        return "Binary/BYTEA: hex text representation. NULL→'<<NULL>>'."

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [("BYTEA",     "BINARY"),   ("BYTEA",     "VARBINARY"),
                ("BINARY",    "BINARY"),   ("VARBINARY", "BINARY"),
                ("BYTEA",     "VARCHAR"),  ("BYTEA",     "STRING")]

    def _pg_expression(self, col: str) -> str: return f"encode({col}, 'hex')"
    def _sf_expression(self, col: str) -> str: return f"LOWER(HEX_ENCODE({col}))"
    def _ms_expression(self, col: str) -> str: return f"LOWER(CONVERT(VARCHAR(MAX), {col}, 2))"
    def _athena_expression(self, col: str) -> str: return f"LOWER(to_hex({col}))"


class HStoreRule(BaseValidationRule):
    """HStore → raw JSON text; canonicalized in Python, not in SQL.

    Source (PostgreSQL):
      hstore_to_json() emits the map as a JSON object. This is lossless — hstore
      values are always text-or-NULL — and means the Python side never has to
      parse the native "k"=>"v" literal for Postgres sources.

    Target (Snowflake VARIANT / VARCHAR):
      Fivetran and most loaders migrate hstore as a JSON object, so TO_JSON
      renders it directly. Rows migrated in *native* hstore format instead are
      still handled: they arrive as text and
      semantic_normalize.parse_hstore_text() reads them.

    Why not canonicalize in SQL — see JSONRule for the full list. The one that
    bit this rule hardest: an hstore value can be a *string containing a JSON
    document* (e.g. "payables"=>"[{...}]"), and the SQL form compared those
    byte-for-byte, so any re-serialization during migration read as data drift.
    The Python canonicalizer recurses into them.

    Why no TRIM():
      Whitespace is semantically meaningful inside hstore values.
    """

    @property
    def rule_name(self) -> str: return "hstore"

    @property
    def description(self) -> str:
        return (
            "HStore → raw JSON text; canonicalized in Python (recursive key "
            "sort, JSON-in-string recursion, number normalization) before "
            "comparison. Values preserve whitespace. SQL NULL → '<<NULL>>'."
        )

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return [
            ("HSTORE", "TEXT"), ("HSTORE", "VARCHAR"),
            ("HSTORE", "STRING"), ("HSTORE", "VARIANT"),
        ]

    def _pg_expression(self, col: str) -> str:
        # hstore_to_json emits a real JSON object, so the Python canonicalizer
        # takes the same code path for hstore as for json/jsonb. Lossless:
        # hstore values are always text or NULL.
        return f"CAST(hstore_to_json({col}) AS TEXT)"

    def _sf_expression(self, col: str) -> str:
        # Same form as JSONRule — see there for why TYPEOF() is avoided.
        # A row migrated in native hstore format ("k"=>"v") is not valid JSON,
        # so TRY_PARSE_JSON returns NULL and the raw literal passes through for
        # semantic_normalize.parse_hstore_text() to read.
        return (
            f"COALESCE(TO_JSON(TRY_PARSE_JSON(CAST({col} AS STRING))), "
            f"CAST({col} AS STRING))"
        )

    def _ms_expression(self, col: str) -> str:
        # SQL Server has no hstore type; treat as raw text if encountered.
        return f"CAST({col} AS NVARCHAR(MAX))"

    def _athena_expression(self, col: str) -> str:
        return f"CAST({col} AS VARCHAR)"


class ArrayRule(BaseValidationRule):
    """PostgreSQL ARRAY → raw JSON text; canonicalized in Python.

    Before this rule existed, an array column matched nothing specific and fell
    through to TextRule's ("*", "*") catch-all, which applies TRIM(col). That
    compared two different serializations and could never pass:
        source  TRIM(col)  ->  {a,b,c}          (PostgreSQL array literal)
        target  TRIM(col)  ->  ["a","b","c"]    (Snowflake VARIANT/ARRAY as JSON)

    array_to_json() converts the array to real JSON on the source side — the
    same trick HStoreRule uses with hstore_to_json() — so both sides hand the
    Python canonicalizer a JSON document. Multidimensional arrays nest
    naturally ([[1,2],[3,4]]), SQL NULL elements become JSON null, and an empty
    array becomes [].

    Element order is PRESERVED, not sorted: a JSON/SQL array is ordered, so a
    reordered array is genuine drift and must stay visible. Only object keys
    are sorted.
    """

    @property
    def rule_name(self) -> str: return "array"

    @property
    def description(self) -> str:
        return (
            "ARRAY → array_to_json() text; canonicalized in Python. "
            "Element order preserved (arrays are ordered); nested objects have "
            "their keys sorted. SQL NULL → '<<NULL>>'."
        )

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        # PostgreSQL's information_schema reports array columns with
        # data_type = 'ARRAY' (udt_name is _text/_int4/...), and the extractor
        # only substitutes udt_name for 'USER-DEFINED', so 'ARRAY' is the string
        # that reaches the registry.
        return [
            ("ARRAY", "VARIANT"), ("ARRAY", "ARRAY"),
            ("ARRAY", "VARCHAR"), ("ARRAY", "STRING"), ("ARRAY", "TEXT"),
        ]

    def _pg_expression(self, col: str) -> str:
        return f"CAST(array_to_json({col}) AS TEXT)"

    def _sf_expression(self, col: str) -> str:
        # Identical to JSONRule/HStoreRule — see JSONRule for why TYPEOF() is
        # avoided and why the raw text falls through when parsing fails.
        return (
            f"COALESCE(TO_JSON(TRY_PARSE_JSON(CAST({col} AS STRING))), "
            f"CAST({col} AS STRING))"
        )

    def _ms_expression(self, col: str) -> str:
        # SQL Server has no array type; treat as raw text if encountered.
        return f"CAST({col} AS NVARCHAR(MAX))"

    def _athena_expression(self, col: str) -> str:
        # Trino/Presto: render the array as JSON so Python sees a document.
        return f"CAST(json_format(CAST({col} AS JSON)) AS VARCHAR)"


class NullPlaceholderRule(BaseValidationRule):
    """Bare NULL→'<<NULL>>' with plain text cast. Used when no other transformation needed."""

    @property
    def rule_name(self) -> str: return "null_placeholder"

    @property
    def description(self) -> str:
        return f"NULL→'{NULL_PLACEHOLDER}'. Cast to text. Universal rule."

    @property
    def trigger_pairs(self) -> List[Tuple[str, str]]:
        return []  # not auto-matched; explicitly registered for direct use

    def _pg_expression(self, col: str) -> str: return f"CAST({col} AS TEXT)"
    def _sf_expression(self, col: str) -> str: return f"CAST({col} AS STRING)"
    def _ms_expression(self, col: str) -> str: return f"CAST({col} AS VARCHAR(MAX))"
    def _athena_expression(self, col: str) -> str: return f"CAST({col} AS VARCHAR)"

    def apply_postgresql(self, col: str, alias=None) -> str:
        expr = f"COALESCE(CAST({col} AS TEXT), '{NULL_PLACEHOLDER}')"
        return f"{expr} AS {alias}" if alias else expr

    def apply_snowflake(self, col: str, alias=None) -> str:
        expr = f"COALESCE(CAST({col} AS STRING), '{NULL_PLACEHOLDER}')"
        return f"{expr} AS {alias}" if alias else expr

    def apply_mssql(self, col: str, alias=None) -> str:
        expr = f"COALESCE(CAST({col} AS VARCHAR(MAX)), '{NULL_PLACEHOLDER}')"
        return f"{expr} AS {alias}" if alias else expr

    def apply_athena(self, col: str, alias=None) -> str:
        expr = f"COALESCE(CAST({col} AS VARCHAR), '{NULL_PLACEHOLDER}')"
        return f"{expr} AS {alias}" if alias else expr
