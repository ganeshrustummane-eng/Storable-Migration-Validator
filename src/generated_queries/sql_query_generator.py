"""
SQL Query Generator
====================
Builds validation SQL queries for a PostgreSQL → Snowflake table pair.

Applies the FULL normalization rules per specification:
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Type          │ PG Expression                │ SF Expression        │
  ├─────────────────────────────────────────────────────────────────────┤
  │ BOOLEAN       │ CASE WHEN…THEN '1'/'0'        │ CASE WHEN…THEN '1'  │
  │ NUMERIC       │ ROUND(CAST(…AS NUMERIC),2)    │ ROUND(CAST(…),2)    │
  │ TIMESTAMP_NTZ │ TO_CHAR(…,'YYYY-MM-DD HH24:  │ TO_VARCHAR(…,fmt)   │
  │               │          MI:SS')              │                     │
  │ TIMESTAMP_TZ  │ TO_CHAR(…AT TZ 'UTC',…)       │ TO_VARCHAR(CONVERT_ │
  │               │                               │ TIMEZONE('UTC',…),…)│
  │ DATE          │ TO_CHAR(…,'YYYY-MM-DD')        │ TO_VARCHAR(…,'…')   │
  │ TEXT/VARCHAR  │ TRIM(…)                       │ TRIM(…)             │
  │ UUID          │ UPPER(TRIM(CAST(…AS TEXT)))   │ UPPER(TRIM(…))      │
  │ INTEGER       │ CAST(…AS TEXT)                │ CAST(…AS STRING)    │
  │ JSON/JSONB    │ …::jsonb::text                │ TO_JSON(PARSE_JSON) │
  │ BYTEA         │ encode(…,'hex')               │ LOWER(HEX_ENCODE(…))│
  │ ALL (NULL)    │ COALESCE(…,'<<NULL>>')         │ COALESCE(…,'<<NULL>>│
  └─────────────────────────────────────────────────────────────────────┘

Snowflake-specific:
  WHERE _FIVETRAN_ACTIVE = TRUE  ← only latest active records compared

Generated queries (no PK dependency — PKs deferred to future milestone):
  ① Row count      (source — PostgreSQL)
  ② Row count      (target — Snowflake)
  ③ Main validation (source — normalised SELECT for all columns)
  ④ Main validation (target — normalised SELECT for all columns)
  ⑤ NULL % per column (source)
  ⑥ NULL % per column (target)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Tuple  # noqa: F401 (Optional used in generate())

from ai_transformation.column_mapping import ColumnRuleMapping
from rules import get_rule_for_type
from generated_queries.ai_sql_generator import (
    AIGeneratedQuery,
    AISQLGenerationError,
    AISQLQueryGenerator,
)

if TYPE_CHECKING:
    from core.validation_plan import CanonicalValidationPlan, ColumnMappingEntry


# ---------------------------------------------------------------------------
# Output container
# ---------------------------------------------------------------------------

@dataclass
class ValidationQuerySet:
    """
    All SQL queries generated for one source → target table pair.

    Attributes:
        table_name             : Source table name
        source_db_label        : e.g. 'postgresql://public.events'
        target_db_label        : e.g. 'snowflake://dev_edge_bronze.schema.EVENTS'
        generated_by           : 'AI' or 'static'
        generated_at           : ISO timestamp
        model_used             : AI model name used for generation (e.g. 'gpt-4o')

        row_count_source       : ① SELECT COUNT(*) on PostgreSQL
        row_count_target       : ② SELECT COUNT(*) on Snowflake
        main_validation_source : ③ Normalised SELECT on PostgreSQL
        main_validation_target : ④ Normalised SELECT on Snowflake
        null_pct_source        : ⑤ NULL % per column — PostgreSQL
        null_pct_target        : ⑥ NULL % per column — Snowflake
        distinct_count_source  : ⑦ DISTINCT count per column — PostgreSQL
        distinct_count_target  : ⑧ DISTINCT count per column — Snowflake

        -- PK-aware queries (only when primary_keys is non-empty) --
        pk_duplicate_source    : ⑨ Duplicate PK check — PostgreSQL
        pk_duplicate_target    : ⑩ Duplicate PK check — Snowflake
        pk_missing_rows        : ⑪ Source PKs NOT IN target
        pk_orphan_rows         : ⑫ Target PKs NOT IN source
        pk_ordered_source      : ⑬ Ordered SELECT (ORDER BY pk) — PostgreSQL
        pk_ordered_target      : ⑭ Ordered SELECT (ORDER BY pk) — Snowflake

        primary_keys           : PK column list (empty = no PK, queries ⑨-⑭ omitted)
        pk_warning             : Warning message when table has no PK
        combined_sql           : All queries combined into one file
    """
    table_name: str
    source_db_label: str
    target_db_label: str
    generated_by: str = "static"
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    model_used: str = "N/A"

    row_count_source: str = ""
    row_count_target: str = ""
    main_validation_source: str = ""
    main_validation_target: str = ""
    null_pct_source: str = ""
    null_pct_target: str = ""
    distinct_count_source: str = ""
    distinct_count_target: str = ""

    # PK-aware queries
    pk_duplicate_source: str = ""
    pk_duplicate_target: str = ""
    pk_missing_rows: str = ""
    pk_orphan_rows: str = ""
    pk_ordered_source: str = ""
    pk_ordered_target: str = ""

    row_hash_source: str = ""
    row_hash_target: str = ""
    transformation_source: str = ""
    transformation_target: str = ""
    aggregate_source: str = ""
    aggregate_target: str = ""

    primary_keys: List[str] = field(default_factory=list)
    pk_warning: str = ""
    combined_sql: str = ""


# ---------------------------------------------------------------------------
# SQL Query Generator
# ---------------------------------------------------------------------------

class SQLQueryGenerator:
    """
    Builds all validation SQL from a ColumnRuleMapping list.

    Multi-Database Support
    ----------------------
    Automatically generates database-specific SQL for:
      - MS SQL Server (mssql, sqlserver)
      - PostgreSQL (postgres, postgresql)
      - Athena (athena, trino, presto)
      - Snowflake (snowflake)

    AI-only comparison SQL
    ----------------------
    The data-validation SELECTs on BOTH sides are written by AI. There is no
    rule-based fallback: if AI is unavailable or its output fails dialect
    validation, generation raises AISQLGenerationError.

    Row counts remain deterministic — COUNT(*) carries no dialect ambiguity
    worth a model call, and a hand-written count cannot drift from intent.

    PK-Free Design
    --------------
    Primary key handling is deferred to a future milestone.
    Queries do NOT include ORDER BY pk, duplicate PK checks, or missing
    row checks. All columns are treated equally.

    NULL Handling
    -------------
    COALESCE(CAST(expr AS TEXT/STRING), '<<NULL>>') is applied to EVERY
    column so NULL and the literal string compare identically on both sides.

    Fivetran Filter
    ---------------
    When has_fivetran_active=True the Snowflake queries include:
        WHERE _FIVETRAN_ACTIVE = TRUE
    This ensures only the LATEST active record is compared.
    """

    NULL_PLACEHOLDER = "<<NULL>>"

    def __init__(self, use_ai: bool = True, ai_model: Optional[str] = None):
        """
        Initialize SQL Query Generator.

        Args:
        """
        if not use_ai:
            raise AISQLGenerationError(
                "SQLQueryGenerator(use_ai=False) is no longer supported. "
                "Validation SQL is AI-generated only."
            )

        self._use_ai = True
        try:
            self._ai_generator = AISQLQueryGenerator(model=ai_model)
        except Exception as exc:
            raise AISQLGenerationError(
                f"Could not initialise the AI SQL generator: {exc}"
            ) from exc

        if not self._ai_generator._ai_active:
            raise AISQLGenerationError(
                "No AI API key configured — cannot generate validation SQL.\n"
                "  Set one of the following in .env:\n"
                "    DIAL_API_KEY=...    (EPAM DIAL — access to GPT/Claude)\n"
                "    CLAUDE_API_KEY=...  (Anthropic direct — no VPN needed)\n"
                "  Or run: python validate_cli.py  →  choose [8] Configure API key"
            )

        print(
            f"  [SQLQueryGenerator] AI generation active (model: {self._ai_generator.model})"
        )

    def generate(
        self,
        pg_schema: str,
        pg_table: str,
        sf_database: str,
        sf_schema: str,
        sf_table: str,
        mappings: List[ColumnRuleMapping],
        has_fivetran_active: bool = False,
        generated_by: str = "static",
        model_used: str = "N/A",
        primary_keys: Optional[List[str]] = None,
        target_primary_keys: Optional[List[str]] = None,
        source_filter: str = "",
        target_filter: str = "",
        source_db_type: str = "postgresql",
    ) -> ValidationQuerySet:
        """
        Generate all validation queries for the given table pair.

        Args:
            pg_schema           : PostgreSQL schema (e.g. 'public')
            pg_table            : PostgreSQL table name

            sf_database         : Snowflake database name
            sf_schema           : Snowflake schema name
            sf_table            : Snowflake table name
            mappings            : Column rule mappings (active only)
            has_fivetran_active : If True, adds WHERE _FIVETRAN_ACTIVE = TRUE on SF side
            generated_by        : 'AI' or 'static'
            model_used          : AI model name (e.g. 'gpt-4o', 'N/A' for static)
            primary_keys        : Source PK column names (enables queries ⑨-⑭)
            target_primary_keys : Target PK column names (for cross-DB PK checks)

        Returns:
            ValidationQuerySet with all queries populated.
        """
        active = [m for m in mappings if not m.skip_validation]
        pks    = primary_keys or []
        tgt_pks = target_primary_keys or pks  # default to same names when not specified

        # Resolve source dialect. Explicit arg wins; fall back to env for
        # backward compatibility with callers that set SOURCE_TYPE.
        src_type = (source_db_type or os.getenv("SOURCE_TYPE", "postgresql")).strip().lower()

        sf_full = (
            f"{sf_database}.{sf_schema}.{sf_table}"
            if sf_database
            else f"{sf_schema}.{sf_table}"
        )

        qs = ValidationQuerySet(
            table_name=pg_table,
            source_db_label=f"{src_type}://{pg_schema}.{pg_table}",
            target_db_label=f"snowflake://{sf_full}",
            generated_by=generated_by,
            model_used=model_used,
            primary_keys=pks,
        )

        # Resolve effective target filter: explicit target_filter wins; fall back to source_filter.
        # This handles the common case where source and target predicates are identical
        # (e.g. both use created_at >= '2024-01-01' with same column name).
        eff_tgt_filter = target_filter or source_filter

        qs.row_count_source       = self._row_count_pg(pg_schema, pg_table, src_type, source_filter)
        qs.row_count_target       = self._row_count_sf(sf_full, has_fivetran_active, eff_tgt_filter)
        qs.main_validation_source = self._main_validation_pg(pg_schema, pg_table, active, src_type, source_filter)
        qs.main_validation_target = self._main_validation_sf(sf_full, active, has_fivetran_active, src_type, eff_tgt_filter)
        qs.null_pct_source        = self._null_pct_pg(pg_schema, pg_table, active, src_type, source_filter)
        qs.null_pct_target        = self._null_pct_sf(sf_full, active, has_fivetran_active, eff_tgt_filter)
        qs.distinct_count_source  = self._distinct_count_pg(pg_schema, pg_table, active, src_type, source_filter)
        qs.distinct_count_target  = self._distinct_count_sf(sf_full, active, has_fivetran_active, eff_tgt_filter)

        if pks:
            qs.pk_duplicate_source = self._pk_duplicate_pg(pg_schema, pg_table, pks, source_filter)
            qs.pk_duplicate_target = self._pk_duplicate_sf(sf_full, tgt_pks, has_fivetran_active, eff_tgt_filter)
            qs.pk_missing_rows     = self._pk_missing_rows(pg_schema, pg_table, pks, sf_full, tgt_pks, has_fivetran_active, source_filter, eff_tgt_filter)
            qs.pk_orphan_rows      = self._pk_orphan_rows(pg_schema, pg_table, pks, sf_full, tgt_pks, has_fivetran_active, source_filter, eff_tgt_filter)
            qs.pk_ordered_source   = self._pk_ordered_pg(pg_schema, pg_table, active, pks, src_type, source_filter)
            qs.pk_ordered_target   = self._pk_ordered_sf(sf_full, active, tgt_pks, has_fivetran_active, eff_tgt_filter)
        else:
            qs.pk_warning = (
                "No primary key detected — "
                "duplicate/missing row checks (⑨-⑭) skipped. "
                "Declare primary_keys in your batch YAML to enable them."
            )

        qs.combined_sql = self._combined(qs)
        return qs

    def _row_hash_queries(self, plan, mappings, source_filter: str, target_filter: str) -> Tuple[str, str]:
        spec = plan.row_hash
        selected = [m for m in mappings if not spec.columns or m.source_column in spec.columns]
        if not selected:
            raise AISQLGenerationError("Row hash requires mapped comparison columns")
        source_values = [m.rule.apply_source(plan.source_db_type, m.source_column) for m in selected]
        target_values = [m.rule.apply_snowflake(m.target_column) for m in selected]
        source_hash = self._hash_expression(plan.source_db_type, source_values)
        target_hash = self._hash_expression("snowflake", target_values)
        source_key = ", ".join(plan.source_primary_keys) or "NULL"
        target_key = ", ".join(plan.target_primary_keys or plan.source_primary_keys) or "NULL"
        source_prefix, source_from = self._comparison_source(plan, False)
        target_prefix, target_from = self._comparison_source(plan, True)
        return (
            f"{source_prefix}SELECT {source_key} AS record_key, {source_hash} AS row_hash FROM "
            f"{source_from}{self._where(source_filter)};",
            f"{target_prefix}SELECT {target_key} AS record_key, {target_hash} AS row_hash FROM "
            f"{target_from}{self._where(target_filter, plan.has_fivetran_active)};",
        )

    @staticmethod
    def _comparison_source(plan, target: bool) -> Tuple[str, str]:
        """Return CTE prefix and relation for comparison-purpose joins."""
        relationships = [r for r in plan.relationships if r.purpose == "comparison"]
        if not relationships:
            if target:
                return "", f"{plan.target_database}.{plan.target_schema}.{plan.target_table}"
            return "", f"{plan.source_schema}.{plan.source_table}"
        base = plan.target_table if target else plan.source_table
        base_qualified = (
            f"{plan.target_database}.{plan.target_schema}.{base}" if target
            else f"{plan.source_schema}.{base}"
        )
        joins = [f"SELECT base.* FROM {base_qualified} base"]
        for index, relationship in enumerate(relationships):
            right = relationship.target_right_table if target else relationship.source_right_table
            right = right or relationship.right_table
            left_alias = "base"
            right_alias = f"rel_{index}"
            right_qualified = (
                f"{plan.target_database}.{plan.target_schema}.{right}" if target
                else right
            )
            condition = " AND ".join(
                f"{left_alias}.{left} = {right_alias}.{right_col}"
                for left, right_col in zip(relationship.left_columns, relationship.right_columns)
            )
            joins.append(f"JOIN {right_qualified} {right_alias} ON {condition}")
        body = "\n".join([joins[0], *joins[1:]])
        return "WITH validation_population AS (\n" + body + "\n)\n", "validation_population"

    @staticmethod
    def _hash_expression(dialect: str, values: List[str]) -> str:
        dialect = (dialect or "postgresql").lower()
        if dialect in {"mssql", "sqlserver", "sql_server", "mssqlserver"}:
            joined = " + '|' + ".join(
                f"COALESCE(CAST({value} AS VARCHAR(MAX)), '{SQLQueryGenerator.NULL_PLACEHOLDER}')"
                for value in values
            )
            return f"CONVERT(VARCHAR(64), HASHBYTES('SHA2_256', {joined}), 2)"
        if dialect in {"athena", "trino", "presto"}:
            joined = "concat_ws('|', " + ", ".join(values) + ")"
            return f"to_hex(sha256(to_utf8({joined})))"
        if dialect == "snowflake":
            joined = "CONCAT_WS('|', " + ", ".join(values) + ")"
            return f"SHA2({joined}, 256)"
        joined = " || '|' || ".join(
            f"COALESCE(CAST({value} AS TEXT), '{SQLQueryGenerator.NULL_PLACEHOLDER}')"
            for value in values
        )
        if dialect == "redshift":
            return f"SHA2({joined}, 256)"
        return f"encode(digest({joined}, 'sha256'), 'hex')"

    def _transformation_queries(self, plan, source_filter: str, target_filter: str) -> Tuple[str, str]:
        source_expr = ", ".join(f"{item.source_expression} AS {item.name}_value" for item in plan.transformations)
        target_expr = ", ".join(f"{item.target_expression} AS {item.name}_value" for item in plan.transformations)
        source_prefix, source_from = self._comparison_source(plan, False)
        target_prefix, target_from = self._comparison_source(plan, True)
        return (
            f"{source_prefix}SELECT {source_expr} FROM {source_from}{self._where(source_filter)};",
            f"{target_prefix}SELECT {target_expr} FROM {target_from}"
            f"{self._where(target_filter, plan.has_fivetran_active)};",
        )

    def _aggregate_queries(self, plan, specs, source_filter: str, target_filter: str) -> Tuple[str, str]:
        source_parts, target_parts = [], []
        for spec in specs:
            function = str(spec.options.get("function", "SUM")).upper()
            if function == "COUNT_DISTINCT":
                function_sql = "COUNT(DISTINCT {column})"
            elif function in {"SUM", "MIN", "MAX", "AVG", "COUNT"}:
                function_sql = f"{function}({{column}})"
            else:
                raise AISQLGenerationError(f"Unsupported aggregate function: {function}")
            for column in spec.columns:
                alias = f"{spec.name or column}_{function.lower()}"
                source_parts.append(f"{function_sql.format(column=column)} AS {alias}")
                target_parts.append(f"{function_sql.format(column=column)} AS {alias}")
        source_prefix, source_from = self._comparison_source(plan, False)
        target_prefix, target_from = self._comparison_source(plan, True)
        return (
            f"{source_prefix}SELECT {', '.join(source_parts)} FROM {source_from}{self._where(source_filter)};",
            f"{target_prefix}SELECT {', '.join(target_parts)} FROM {target_from}"
            f"{self._where(target_filter, plan.has_fivetran_active)};",
        )

    def generate_from_plan(self, plan: "CanonicalValidationPlan") -> ValidationQuerySet:
        """
        Generate all validation queries from a CanonicalValidationPlan.

        This is the new plan-driven entry point. The plan is the single source
        of truth — no other input is needed.

        Args:
            plan: Fully constructed and validated CanonicalValidationPlan

        Returns:
            ValidationQuerySet with all queries populated (incl. PK queries when available).
        """
        mappings = _plan_to_rule_mappings(plan.active_mappings)
        source_filter, target_filter = self._plan_filters(plan)
        query_set = self.generate(
            pg_schema=plan.source_schema,
            pg_table=plan.source_table,
            sf_database=plan.target_database,
            sf_schema=plan.target_schema,
            sf_table=plan.target_table,
            mappings=mappings,
            has_fivetran_active=plan.has_fivetran_active,
            generated_by=plan.generated_by,
            model_used=plan.model_used,
            primary_keys=plan.source_primary_keys,
            target_primary_keys=plan.target_primary_keys,
            source_db_type=plan.source_db_type,
            source_filter=source_filter,
            target_filter=target_filter,
        )
        if plan.row_hash:
            query_set.row_hash_source, query_set.row_hash_target = self._row_hash_queries(
                plan, mappings, source_filter, target_filter
            )
        if plan.transformations:
            query_set.transformation_source, query_set.transformation_target = self._transformation_queries(
                plan, source_filter, target_filter
            )
        aggregate_specs = [v for v in plan.validations if v.validation_type.upper() == "AGGREGATE"]
        if aggregate_specs:
            query_set.aggregate_source, query_set.aggregate_target = self._aggregate_queries(
                plan, aggregate_specs, source_filter, target_filter
            )
        query_set.combined_sql = self._combined(query_set)
        return query_set

    @staticmethod
    def _plan_filters(plan: "CanonicalValidationPlan") -> Tuple[str, str]:
        """Compose explicit filters with safe, symmetric population relationships."""
        source_parts = [plan.source_filter] if plan.source_filter else []
        target_parts = [plan.target_filter or plan.source_filter] if (plan.target_filter or plan.source_filter) else []
        for predicate in plan.population_scope.get("filters", []):
            column = str(predicate.get("column", "")).strip()
            operator = str(predicate.get("operator", "=")).strip().upper()
            if not column or operator not in {"=", "!=", "<>", ">", ">=", "<", "<=", "IS", "IS NOT"}:
                continue
            value = predicate.get("value")
            if value is None and operator in {"IS", "IS NOT"}:
                rendered = "NULL"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            else:
                rendered = "'" + str(value).replace("'", "''") + "'"
            expression = f"{column} {operator} {rendered}"
            source_parts.append(expression)
            target_parts.append(expression)
        for relationship in plan.relationships:
            if relationship.purpose != "population":
                continue
            if not relationship.left_columns or len(relationship.left_columns) != len(relationship.right_columns):
                continue
            source_table = relationship.source_right_table or relationship.right_table
            target_table = relationship.target_right_table or relationship.right_table
            source_join = " AND ".join(
                f"{source_table}.{right} = {plan.source_table}.{left}"
                for left, right in zip(relationship.left_columns, relationship.right_columns)
            )
            target_join = " AND ".join(
                f"{target_table}.{right} = {plan.target_table}.{left}"
                for left, right in zip(relationship.left_columns, relationship.right_columns)
            )
            source_parts.append(f"EXISTS (SELECT 1 FROM {source_table} WHERE {source_join})")
            target_parts.append(f"EXISTS (SELECT 1 FROM {target_table} WHERE {target_join})")
        return " AND ".join(source_parts), " AND ".join(target_parts)

    # -----------------------------------------------------------------------
    # WHERE clause builder — composes migration filter + Fivetran flag
    # -----------------------------------------------------------------------

    @staticmethod
    def _where(filter_clause: str, fivetran: bool = False) -> str:
        """Return '\nWHERE ...' string, empty when nothing to filter."""
        parts = []
        if fivetran:
            parts.append("_FIVETRAN_ACTIVE = TRUE")
        if filter_clause:
            parts.append(filter_clause)
        return ("\nWHERE " + " AND ".join(parts)) if parts else ""

    # -----------------------------------------------------------------------
    # ① Row Count — PostgreSQL
    # -----------------------------------------------------------------------

    def _row_count_pg(self, schema: str, table: str, src_type: str = "postgresql", source_filter: str = "") -> str:
        label = _source_label(src_type)
        where = self._where(source_filter)
        return (
            f"-- ① ROW COUNT: {label} ({schema}.{table})\n"
            f"SELECT COUNT(*) AS source_row_count\n"
            f"FROM {schema}.{table}{where};"
        )

    # -----------------------------------------------------------------------
    # ② Row Count — Snowflake
    # -----------------------------------------------------------------------

    def _row_count_sf(self, sf_full: str, fivetran_active: bool, target_filter: str = "") -> str:
        where = self._where(target_filter, fivetran_active)
        return (
            f"-- ② ROW COUNT: Snowflake ({sf_full})\n"
            f"SELECT COUNT(*) AS target_row_count\n"
            f"FROM {sf_full}{where};"
        )

    # -----------------------------------------------------------------------
    # ③ Main Validation — Source (normalised SELECT, no PK ORDER BY)
    # -----------------------------------------------------------------------

    def _main_validation_pg(
        self,
        schema: str,
        table: str,
        mappings: List[ColumnRuleMapping],
        src_type: str = "postgresql",
        source_filter: str = "",
    ) -> str:
        label = _source_label(src_type)
        if not mappings:
            return (
                f"-- ③ SOURCE: {label} ({schema}.{table})\n"
                f"-- No comparable columns found.\n"
                f"SELECT 1;"
            )

        result = self._ai_generator.generate_validation_query(
            schema=schema,
            table=table,
            mappings=mappings,
            source_db_type=src_type,
            query_type="data_validation",
            has_fivetran_active=False,
            source_filter=source_filter,
        )
        return (
            f"-- ③ SOURCE: {label} ({schema}.{table})\n"
            f"-- AI-generated ({self._ai_generator.model}, confidence {result.confidence:.2f})\n"
            f"{result.query}"
        )

    # -----------------------------------------------------------------------
    # ④ Main Validation — Snowflake (normalised SELECT, no PK ORDER BY)
    # -----------------------------------------------------------------------

    def _main_validation_sf(
        self,
        sf_full: str,
        mappings: List[ColumnRuleMapping],
        fivetran_active: bool,
        src_type: str = "postgresql",
        target_filter: str = "",
    ) -> str:
        if not mappings:
            return (
                f"-- ④ TARGET: Snowflake ({sf_full})\n"
                f"-- No comparable columns found.\n"
                f"SELECT 1;"
            )

        result = self._ai_generator.generate_target_validation_query(
            target_fqn=sf_full,
            mappings=mappings,
            source_db_type=src_type,
            target_db_type="snowflake",
            has_fivetran_active=fivetran_active,
            target_filter=target_filter,
        )
        return (
            f"-- ④ TARGET: Snowflake ({sf_full})\n"
            f"-- AI-generated ({self._ai_generator.model}, confidence {result.confidence:.2f})\n"
            f"{result.query}"
        )

    # -----------------------------------------------------------------------
    # ⑤ NULL % Per Column — Source
    #
    # Pure aggregates, deterministic on purpose: there is no dialect ambiguity
    # in SUM(CASE WHEN col IS NULL ...) worth an AI call, and these blocks are
    # diagnostics rather than the comparison contract.
    # -----------------------------------------------------------------------

    def _null_pct_pg(
        self,
        schema: str,
        table: str,
        mappings: List[ColumnRuleMapping],
        src_type: str = "postgresql",
        source_filter: str = "",
    ) -> str:
        if not mappings:
            return ""

        label = _source_label(src_type)
        null_parts = ",\n    ".join(
            f"ROUND(100.0 * SUM(CASE WHEN {m.source_column} IS NULL THEN 1 ELSE 0 END)"
            f" / COUNT(*), 2) AS {m.source_column}_null_pct"
            for m in mappings
        )
        where = self._where(source_filter)
        return (
            f"-- ⑤ NULL % CHECK: {label} ({schema}.{table})\n"
            f"SELECT\n    COUNT(*) AS total_rows,\n    {null_parts}\n"
            f"FROM {schema}.{table}{where};"
        )

    # -----------------------------------------------------------------------
    # ⑥ NULL % Per Column — Snowflake
    # -----------------------------------------------------------------------

    def _null_pct_sf(
        self,
        sf_full: str,
        mappings: List[ColumnRuleMapping],
        fivetran_active: bool,
        target_filter: str = "",
    ) -> str:
        if not mappings:
            return ""

        null_parts = ",\n    ".join(
            f"ROUND(100.0 * SUM(CASE WHEN {m.target_column} IS NULL THEN 1 ELSE 0 END)"
            f" / COUNT(*), 2) AS {m.source_column}_null_pct"
            for m in mappings
        )
        where = self._where(target_filter, fivetran_active)
        return (
            f"-- ⑥ NULL % CHECK: Snowflake ({sf_full})\n"
            f"SELECT\n    COUNT(*) AS total_rows,\n    {null_parts}\n"
            f"FROM {sf_full}{where};"
        )

    # -----------------------------------------------------------------------
    # ⑦ Distinct value count per column — Source
    # -----------------------------------------------------------------------

    def _distinct_count_pg(
        self,
        schema: str,
        table: str,
        mappings: List[ColumnRuleMapping],
        src_type: str = "postgresql",
        source_filter: str = "",
    ) -> str:
        if not mappings:
            return ""

        label = _source_label(src_type)
        is_pg = src_type in ("postgres", "postgresql")

        # json has no equality operator in PostgreSQL; cast to jsonb first so
        # DISTINCT works. Only relevant for the PostgreSQL dialect.
        _JSON_TYPES = {"json"}

        def _pg_distinct_expr(m) -> str:
            col = m.source_column
            if is_pg and getattr(m, "source_type", "").lower() in _JSON_TYPES:
                return f"COUNT(DISTINCT {col}::jsonb) AS {col}_distinct_count"
            return f"COUNT(DISTINCT {col}) AS {col}_distinct_count"

        distinct_parts = ",\n    ".join(_pg_distinct_expr(m) for m in mappings)
        where = self._where(source_filter)
        return (
            f"-- ⑦ DISTINCT VALUE COUNT: {label} ({schema}.{table})\n"
            f"-- Compare distinct counts with ⑧ — large differences indicate data drift.\n"
            f"SELECT\n    COUNT(*) AS total_rows,\n    {distinct_parts}\n"
            f"FROM {schema}.{table}{where};"
        )

    # -----------------------------------------------------------------------
    # ⑧ Distinct value count per column — Snowflake
    # -----------------------------------------------------------------------

    def _distinct_count_sf(
        self,
        sf_full: str,
        mappings: List[ColumnRuleMapping],
        fivetran_active: bool,
        target_filter: str = "",
    ) -> str:
        if not mappings:
            return ""

        distinct_parts = ",\n    ".join(
            f"COUNT(DISTINCT {m.target_column}) AS {m.source_column}_distinct_count"
            for m in mappings
        )
        where = self._where(target_filter, fivetran_active)
        return (
            f"-- ⑧ DISTINCT VALUE COUNT: Snowflake ({sf_full})\n"
            f"-- Compare distinct counts with ⑦ — large differences indicate data drift.\n"
            f"SELECT\n    COUNT(*) AS total_rows,\n    {distinct_parts}\n"
            f"FROM {sf_full}{where};"
        )

    # -----------------------------------------------------------------------
    # ⑨ Duplicate PK check — PostgreSQL
    # -----------------------------------------------------------------------

    def _pk_duplicate_pg(self, schema: str, table: str, pks: List[str], source_filter: str = "") -> str:
        pk_cols = ", ".join(pks)
        where = self._where(source_filter)
        return (
            f"-- ⑨ DUPLICATE PK CHECK: PostgreSQL ({schema}.{table})\n"
            f"-- Expected: 0 rows. Any row here means a duplicate PK violation.\n"
            f"SELECT {pk_cols}, COUNT(*) AS duplicate_count\n"
            f"FROM {schema}.{table}{where}\n"
            f"GROUP BY {pk_cols}\n"
            f"HAVING COUNT(*) > 1\n"
            f"ORDER BY duplicate_count DESC;"
        )

    # -----------------------------------------------------------------------
    # ⑩ Duplicate PK check — Snowflake
    # -----------------------------------------------------------------------

    def _pk_duplicate_sf(self, sf_full: str, tgt_pks: List[str], fivetran_active: bool, target_filter: str = "") -> str:
        pk_cols = ", ".join(tgt_pks)
        where = self._where(target_filter, fivetran_active)
        return (
            f"-- ⑩ DUPLICATE PK CHECK: Snowflake ({sf_full})\n"
            f"-- Expected: 0 rows. Any row here means a duplicate PK in target.\n"
            f"SELECT {pk_cols}, COUNT(*) AS duplicate_count\n"
            f"FROM {sf_full}{where}\n"
            f"GROUP BY {pk_cols}\n"
            f"HAVING COUNT(*) > 1\n"
            f"ORDER BY duplicate_count DESC;"
        )

    # -----------------------------------------------------------------------
    # ⑪ Missing rows — source PKs NOT found in target
    # -----------------------------------------------------------------------

    def _pk_missing_rows(
        self,
        pg_schema: str, pg_table: str, src_pks: List[str],
        sf_full: str,   tgt_pks: List[str],
        fivetran_active: bool,
        source_filter: str = "",
        target_filter: str = "",
    ) -> str:
        tgt_where = self._where(target_filter, fivetran_active)
        src_where = self._where(source_filter)
        if len(src_pks) == 1:
            src_col, tgt_col = src_pks[0], tgt_pks[0]
            return (
                f"-- ⑪ MISSING ROWS: source PKs not found in target\n"
                f"-- Expected: 0 rows. Each row is a record lost during migration.\n"
                f"SELECT src.{src_col}\n"
                f"FROM {pg_schema}.{pg_table} src{src_where}\n"
                f"AND src.{src_col} NOT IN (\n"
                f"    SELECT {tgt_col} FROM {sf_full}{tgt_where}\n"
                f");"
            ) if src_where else (
                f"-- ⑪ MISSING ROWS: source PKs not found in target\n"
                f"-- Expected: 0 rows. Each row is a record lost during migration.\n"
                f"SELECT src.{src_col}\n"
                f"FROM {pg_schema}.{pg_table} src\n"
                f"WHERE src.{src_col} NOT IN (\n"
                f"    SELECT {tgt_col} FROM {sf_full}{tgt_where}\n"
                f");"
            )
        # Composite PK — use NOT EXISTS with correlated subquery
        join_cond = " AND ".join(
            f"src.{s} = tgt.{t}" for s, t in zip(src_pks, tgt_pks)
        )
        src_select = ", ".join(f"src.{c}" for c in src_pks)
        return (
            f"-- ⑪ MISSING ROWS: source PKs not found in target (composite PK)\n"
            f"-- Expected: 0 rows. Each row is a record lost during migration.\n"
            f"SELECT {src_select}\n"
            f"FROM {pg_schema}.{pg_table} src{src_where}\n"
            f"{'AND' if src_where else 'WHERE'} NOT EXISTS (\n"
            f"    SELECT 1 FROM {sf_full} tgt{tgt_where}\n"
            f"    {'AND' if tgt_where else 'WHERE'} {join_cond}\n"
            f");"
        )

    # -----------------------------------------------------------------------
    # ⑫ Orphan rows — target PKs NOT found in source
    # -----------------------------------------------------------------------

    def _pk_orphan_rows(
        self,
        pg_schema: str, pg_table: str, src_pks: List[str],
        sf_full: str,   tgt_pks: List[str],
        fivetran_active: bool,
        source_filter: str = "",
        target_filter: str = "",
    ) -> str:
        tgt_where = self._where(target_filter, fivetran_active)
        src_where_inner = self._where(source_filter)
        if len(tgt_pks) == 1:
            src_col, tgt_col = src_pks[0], tgt_pks[0]
            return (
                f"-- ⑫ ORPHAN ROWS: target PKs not found in source\n"
                f"-- Expected: 0 rows. Each row is an extra record inserted in target.\n"
                f"SELECT tgt.{tgt_col}\n"
                f"FROM {sf_full} tgt{tgt_where}\n"
                f"{'AND' if tgt_where else 'WHERE'} tgt.{tgt_col} NOT IN (\n"
                f"    SELECT {src_col} FROM {pg_schema}.{pg_table}{src_where_inner}\n"
                f");"
            )
        join_cond = " AND ".join(
            f"src.{s} = tgt.{t}" for s, t in zip(src_pks, tgt_pks)
        )
        tgt_select = ", ".join(f"tgt.{c}" for c in tgt_pks)
        return (
            f"-- ⑫ ORPHAN ROWS: target PKs not found in source (composite PK)\n"
            f"-- Expected: 0 rows. Each row is an extra record inserted in target.\n"
            f"SELECT {tgt_select}\n"
            f"FROM {sf_full} tgt{tgt_where}\n"
            f"{'AND' if tgt_where else 'WHERE'} NOT EXISTS (\n"
            f"    SELECT 1 FROM {pg_schema}.{pg_table} src{src_where_inner}\n"
            f"    {'AND' if src_where_inner else 'WHERE'} {join_cond}\n"
            f");"
        )

    # -----------------------------------------------------------------------
    # ⑬ Ordered validation SELECT — PostgreSQL (ORDER BY pk)
    # -----------------------------------------------------------------------

    def _pk_ordered_pg(
        self,
        schema: str,
        table: str,
        mappings: List[ColumnRuleMapping],
        pks: List[str],
        src_type: str = "postgresql",
        source_filter: str = "",
    ) -> str:
        if not mappings:
            return ""
        label = _source_label(src_type)
        select_lines = []
        for m in mappings:
            expr = m.rule.apply_source(src_type, m.source_column, alias=f"{m.source_column}_normalized")
            select_lines.append(f"    {expr}")
        cols     = ",\n".join(select_lines)
        order_by = ", ".join(pks)
        where    = self._where(source_filter)
        return (
            f"-- ⑬ ORDERED VALIDATION — {label} ({schema}.{table})\n"
            f"-- ORDER BY PK ensures reproducible row-by-row comparison with ⑭.\n"
            f"SELECT\n{cols}\n"
            f"FROM {schema}.{table}{where}\n"
            f"ORDER BY {order_by};"
        )

    # -----------------------------------------------------------------------
    # ⑭ Ordered validation SELECT — Snowflake (ORDER BY pk)
    # -----------------------------------------------------------------------

    def _pk_ordered_sf(
        self,
        sf_full: str,
        mappings: List[ColumnRuleMapping],
        tgt_pks: List[str],
        fivetran_active: bool,
        target_filter: str = "",
    ) -> str:
        if not mappings:
            return ""
        select_lines = []
        for m in mappings:
            expr = m.rule.apply_snowflake(m.target_column, alias=f"{m.source_column}_normalized")
            select_lines.append(f"    {expr}")
        cols     = ",\n".join(select_lines)
        order_by = ", ".join(tgt_pks)
        where    = self._where(target_filter, fivetran_active)
        return (
            f"-- ⑭ ORDERED VALIDATION — Snowflake ({sf_full})\n"
            f"-- ORDER BY PK ensures reproducible row-by-row comparison with ⑬.\n"
            f"SELECT\n{cols}\n"
            f"FROM {sf_full}{where}\n"
            f"ORDER BY {order_by};"
        )

    # -----------------------------------------------------------------------
    # Combined file — all queries in sequence
    # -----------------------------------------------------------------------

    def _combined(self, qs: ValidationQuerySet) -> str:
        sep  = "=" * 70
        dash = "─" * 70

        pk_line = (
            f"-- Primary Keys : {', '.join(qs.primary_keys)}"
            if qs.primary_keys
            else f"-- Primary Keys : NONE — {qs.pk_warning}"
        )

        sections = [
            f"-- {sep}",
            f"-- MIGRATION VALIDATOR — Generated Validation Queries",
            f"-- Table        : {qs.table_name}",
            f"-- Source       : {qs.source_db_label}",
            f"-- Target       : {qs.target_db_label}",
            f"-- Generated    : {qs.generated_at}",
            f"-- Generated by : {qs.generated_by.upper()}",
            f"-- AI Model     : {qs.model_used}",
            pk_line,
            f"-- {sep}",
            f"-- HOW TO USE:",
            f"--   ① Run on PostgreSQL  → compare count with ②",
            f"--   ② Run on Snowflake   → compare count with ①",
            f"--   ③ Run on PostgreSQL  → export to CSV",
            f"--   ④ Run on Snowflake   → export to CSV",
            f"--   Compare ③ vs ④ row-by-row — must be IDENTICAL",
            f"--   ⑤ Run on PostgreSQL  → compare NULL % with ⑥",
            f"--   ⑥ Run on Snowflake   → compare NULL % with ⑤",
            f"--   ⑦ Run on PostgreSQL  → compare distinct counts with ⑧",
            f"--   ⑧ Run on Snowflake   → compare distinct counts with ⑦",
        ]
        if qs.primary_keys:
            sections += [
                f"--   ⑨ PK DUPLICATE CHECK — PostgreSQL  (expect 0 rows)",
                f"--   ⑩ PK DUPLICATE CHECK — Snowflake   (expect 0 rows)",
                f"--   ⑪ MISSING ROWS — source PKs not in target (expect 0 rows)",
                f"--   ⑫ ORPHAN  ROWS — target PKs not in source (expect 0 rows)",
                f"--   ⑬ ORDERED VALIDATION — PostgreSQL (compare with ⑭ row-by-row)",
                f"--   ⑭ ORDERED VALIDATION — Snowflake  (compare with ⑬ row-by-row)",
            ]
        sections += [f"-- {sep}", ""]

        def section(label: str, sql: str) -> str:
            if not sql:
                return ""
            return f"-- {dash}\n-- {label}\n-- {dash}\n{sql}\n"

        sections.append(section("① ROW COUNT — PostgreSQL (Expected: matches ②)", qs.row_count_source))
        sections.append(section("② ROW COUNT — Snowflake  (Expected: matches ①)", qs.row_count_target))
        sections.append(section(
            "③ MAIN VALIDATION — PostgreSQL (normalised data — export CSV, compare with ④)",
            qs.main_validation_source,
        ))
        sections.append(section(
            "④ MAIN VALIDATION — Snowflake  (normalised data — export CSV, compare with ③)",
            qs.main_validation_target,
        ))
        sections.append(section(
            "⑤ NULL % PER COLUMN — PostgreSQL (compare null_pct values with ⑥)",
            qs.null_pct_source,
        ))
        sections.append(section(
            "⑥ NULL % PER COLUMN — Snowflake  (compare null_pct values with ⑤)",
            qs.null_pct_target,
        ))
        sections.append(section(
            "⑦ DISTINCT VALUE COUNT — PostgreSQL (compare distinct_count with ⑧)",
            qs.distinct_count_source,
        ))
        sections.append(section(
            "⑧ DISTINCT VALUE COUNT — Snowflake  (compare distinct_count with ⑦)",
            qs.distinct_count_target,
        ))
        sections.append(section("ROW HASH — source", qs.row_hash_source))
        sections.append(section("ROW HASH — target", qs.row_hash_target))
        sections.append(section("TRANSFORMATION — source expected values", qs.transformation_source))
        sections.append(section("TRANSFORMATION — target actual values", qs.transformation_target))
        sections.append(section("AGGREGATE — source", qs.aggregate_source))
        sections.append(section("AGGREGATE — target", qs.aggregate_target))

        if qs.primary_keys:
            sections.append(section(
                "⑨ DUPLICATE PK CHECK — PostgreSQL (expect 0 rows)", qs.pk_duplicate_source,
            ))
            sections.append(section(
                "⑩ DUPLICATE PK CHECK — Snowflake  (expect 0 rows)", qs.pk_duplicate_target,
            ))
            sections.append(section(
                "⑪ MISSING ROWS — Source PKs not in target (expect 0 rows)", qs.pk_missing_rows,
            ))
            sections.append(section(
                "⑫ ORPHAN ROWS  — Target PKs not in source (expect 0 rows)", qs.pk_orphan_rows,
            ))
            sections.append(section(
                "⑬ ORDERED VALIDATION — PostgreSQL (compare row-by-row with ⑭)", qs.pk_ordered_source,
            ))
            sections.append(section(
                "⑭ ORDERED VALIDATION — Snowflake  (compare row-by-row with ⑬)", qs.pk_ordered_target,
            ))

        return "\n".join(s for s in sections if s)


# ---------------------------------------------------------------------------
# Plan-to-mapping adapter (module-level helper)
# ---------------------------------------------------------------------------

def _source_label(src_type: str) -> str:
    """Human-readable label for the source dialect used in SQL comments."""
    db = (src_type or "postgresql").strip().lower()
    return {
        "postgres":    "PostgreSQL",
        "postgresql":  "PostgreSQL",
        "mssql":       "MS SQL Server",
        "sqlserver":   "MS SQL Server",
        "sql_server":  "MS SQL Server",
        "mssqlserver": "MS SQL Server",
        "athena":      "Athena",
        "trino":       "Trino",
        "presto":      "Presto",
        "snowflake":   "Snowflake",
    }.get(db, src_type)


def _plan_to_rule_mappings(
    active_entries: "List[ColumnMappingEntry]",
) -> List[ColumnRuleMapping]:
    """
    Convert CanonicalValidationPlan active_mappings to ColumnRuleMapping list.

    This bridges the plan-centric data model to the existing SQL generator.
    We look up the rule by (source_type, target_type) — same logic as StaticMapper.
    """
    from core.validation_plan import ColumnMappingEntry  # local import avoids cycle

    result: List[ColumnRuleMapping] = []
    for entry in active_entries:
        if entry.skip_validation:
            continue
        rule = get_rule_for_type(entry.source_type, entry.target_type)
        result.append(ColumnRuleMapping(
            source_column=entry.source_column,
            target_column=entry.target_column,
            source_type=entry.source_type,
            target_type=entry.target_type,
            rule=rule,
            is_primary_key=entry.is_primary_key,
            skip_validation=False,
            matched_by=entry.match_method,
        ))
    return result
