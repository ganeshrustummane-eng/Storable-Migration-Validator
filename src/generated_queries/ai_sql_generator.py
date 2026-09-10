"""
AI-Powered SQL Query Generator
================================
Uses AI to dynamically generate database-specific SQL queries for validation.

This module leverages the AI model to write optimized, database-specific SQL
queries based on the source and target database types, ensuring proper syntax
and data type conversions for:
  - MS SQL Server → Snowflake
  - PostgreSQL → Snowflake
  - Athena → Snowflake
  - Any database → Any database

Key Features:
  - Database-specific syntax (CAST, CONVERT, FORMAT functions)
  - Proper data type conversions (INT → VARCHAR(MAX) for MSSQL, TEXT for PG)
  - Timezone handling per database
  - NULL placeholder insertion
  - Fivetran active record filtering
  - Self-correcting: dialect-check failures are fed back to the model

AI-only by design
-----------------
There is NO rule-based fallback. If AI is unavailable or cannot produce SQL
that passes dialect validation, generation raises AISQLGenerationError.
A silently degraded query is more dangerous than a failed run: it yields
validation results that look authoritative but were never trustworthy.

Environment Variables Required:
  DIAL_API_KEY      — EPAM DIAL API key (REQUIRED)
  DIAL_API_BASE     — defaults to https://ai-proxy.lab.epam.com
  DIAL_API_VERSION  — defaults to 2025-04-01-preview
  DIAL_MODEL        — defaults to gpt-4o
"""

import json
import os
import re
from typing import List, Optional, Tuple
from dataclasses import dataclass

from ai_transformation.column_mapping import ColumnRuleMapping

try:
    from token_usage_analysis.token_logger import (
        log_usage, extract_openai_usage, extract_anthropic_usage,
    )
except ImportError:
    def log_usage(*args, **kwargs):  # pragma: no cover - logging is best-effort
        pass

    def extract_openai_usage(response):  # pragma: no cover
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def extract_anthropic_usage(response):  # pragma: no cover
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_API_BASE     = "https://ai-proxy.lab.epam.com"
_DEFAULT_API_VERSION  = "2025-04-01-preview"
_DEFAULT_DIAL_MODEL   = "gpt-4o"
_DEFAULT_CLAUDE_MODEL = "claude-3-5-sonnet-20241022"
NULL_PLACEHOLDER      = "<<NULL>>"

_BACKEND_DIAL   = "dial"
_BACKEND_CLAUDE = "claude"

# Failed dialect checks are fed back to the model; this caps the correction loop.
MAX_GENERATION_ATTEMPTS = 3


class AISQLGenerationError(RuntimeError):
    """Raised when AI cannot produce SQL that passes dialect validation."""


# ---------------------------------------------------------------------------
# AI SQL Generator
# ---------------------------------------------------------------------------

@dataclass
class AIGeneratedQuery:
    """Container for AI-generated SQL query with metadata."""
    query: str
    database_type: str
    explanation: str
    confidence: float
    warnings: List[str]


class AISQLQueryGenerator:
    """
    Generates database-specific SQL queries using AI.
    
    This generator understands the nuances of different SQL dialects and
    produces optimized, correct queries for each source/target combination.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        api_version: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """
        Args:
            api_key    : DIAL API key (default: DIAL_API_KEY env var)
            api_base   : DIAL endpoint base URL
            api_version: Azure OpenAI API version
            model      : Model deployment name (e.g. 'gpt-4o', 'gpt-4o-mini')
        """
        # Priority 1: EPAM DIAL
        dial_key   = api_key or os.getenv("DIAL_API_KEY", "")
        # Priority 2: Claude direct (only when DIAL key absent)
        claude_key = os.getenv("CLAUDE_API_KEY", "") if not dial_key else ""

        if dial_key:
            self._backend    = _BACKEND_DIAL
            self.api_key     = dial_key
            self.api_base    = api_base    or os.getenv("DIAL_API_BASE",    _DEFAULT_API_BASE)
            self.api_version = api_version or os.getenv("DIAL_API_VERSION", _DEFAULT_API_VERSION)
            self.model       = model       or os.getenv("DIAL_MODEL",        _DEFAULT_DIAL_MODEL)
        elif claude_key:
            self._backend    = _BACKEND_CLAUDE
            self.api_key     = claude_key
            self.api_base    = ""
            self.api_version = ""
            # Guard: never send a DIAL/GPT model name to Anthropic API
            claude_env_model = os.getenv("CLAUDE_MODEL", _DEFAULT_CLAUDE_MODEL)
            if model and model.lower().startswith("claude-"):
                self.model = model
            else:
                self.model = claude_env_model
        else:
            self._backend    = _BACKEND_DIAL
            self.api_key     = ""
            self.api_base    = _DEFAULT_API_BASE
            self.api_version = _DEFAULT_API_VERSION
            self.model       = model or _DEFAULT_DIAL_MODEL

        self._ai_active = bool(self.api_key)

    def generate_validation_query(
        self,
        schema: str,
        table: str,
        mappings: List[ColumnRuleMapping],
        source_db_type: str,
        target_db_type: str = "snowflake",
        query_type: str = "data_validation",
        has_fivetran_active: bool = False,
        source_filter: str = "",
    ) -> AIGeneratedQuery:
        """
        Generate a database-specific validation query using AI.

        There is no rule-based fallback. If AI cannot produce SQL that passes
        dialect validation, this raises — a silently degraded query is worse
        than no query, because it produces results nobody knows to distrust.

        Args:
            schema              : Source schema name
            table               : Source table name
            mappings            : Column rule mappings
            source_db_type      : Source database type (mssql, postgresql, athena)
            target_db_type      : Target database type (default: snowflake)
            query_type          : Type of query (data_validation, null_pct, distinct_count)
            has_fivetran_active : Whether to include Fivetran filter

        Returns:
            AIGeneratedQuery with validated SQL.

        Raises:
            AISQLGenerationError: no API key, SDK missing, API unreachable, or
                                  every attempt failed dialect validation.
        """
        if not self._ai_active:
            raise AISQLGenerationError(
                "No AI API key configured — cannot generate validation SQL.\n"
                "  Set one of the following in .env:\n"
                "    DIAL_API_KEY=...    (EPAM DIAL — access to GPT/Claude/Gemini)\n"
                "    CLAUDE_API_KEY=...  (Anthropic direct — no VPN needed)\n"
                "  Or run: python validate_cli.py  →  choose [8] Configure API key"
            )

        system_prompt = self._build_system_prompt(source_db_type, target_db_type)
        user_prompt   = self._build_user_prompt(
            schema, table, mappings, source_db_type, query_type, has_fivetran_active,
            source_filter=source_filter,
        )

        print(
            f"  [AISQLGenerator] Backend: "
            f"{'Claude Direct' if self._backend == _BACKEND_CLAUDE else 'EPAM DIAL'}  |  "
            f"Model: '{self.model}'  |  {schema}.{table} ({query_type})"
        )

        attempts: List[str] = []
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]

        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            raw    = self._call_ai(messages, f"{schema}.{table} ({query_type})", attempt)
            result = self._parse_response(raw, source_db_type, mappings, query_type)

            if not result.warnings:
                if attempt > 1:
                    print(
                        f"  [AISQLGenerator] {schema}.{table} ({query_type}) "
                        f"passed validation on attempt {attempt}."
                    )
                return result

            attempts.append(f"attempt {attempt}: " + "; ".join(result.warnings))
            print(
                f"  [AISQLGenerator] Attempt {attempt}/{MAX_GENERATION_ATTEMPTS} for "
                f"{schema}.{table} ({query_type}) failed dialect checks: "
                + "; ".join(result.warnings)
            )
            messages = messages + [
                {"role": "assistant", "content": result.query},
                {
                    "role": "user",
                    "content": (
                        "That query failed validation for these reasons:\n"
                        + "\n".join(f"- {w}" for w in result.warnings)
                        + f"\n\nRewrite it as valid {source_db_type.upper()} SQL that "
                        "fixes every issue above. Return ONLY the SQL."
                    ),
                },
            ]

        raise AISQLGenerationError(
            f"AI could not produce valid {source_db_type.upper()} SQL for "
            f"{schema}.{table} ({query_type}) after {MAX_GENERATION_ATTEMPTS} attempts.\n"
            + "\n".join(f"  {a}" for a in attempts)
        )

    def generate_target_validation_query(
        self,
        target_fqn: str,
        mappings: List[ColumnRuleMapping],
        source_db_type: str,
        target_db_type: str = "snowflake",
        has_fivetran_active: bool = False,
        target_filter: str = "",
    ) -> AIGeneratedQuery:
        """
        Generate the TARGET-side data validation query using AI.

        Generating both sides with AI (rather than AI source + rule-based target)
        is what keeps the two SELECT lists aligned: the model is given the
        source aliases and told they must match exactly, so a column can never
        be compared against the wrong one.

        Args:
            target_fqn          : Fully qualified target table (db.schema.table)
            mappings            : Column rule mappings (active only)
            source_db_type      : Source dialect, for alias context
            target_db_type      : Target dialect (default: snowflake)
            has_fivetran_active : Add the active-record filter

        Raises:
            AISQLGenerationError: same conditions as generate_validation_query.
        """
        if not self._ai_active:
            raise AISQLGenerationError(
                "No AI API key configured — cannot generate target validation SQL.\n"
                "  Set DIAL_API_KEY or CLAUDE_API_KEY in .env, "
                "or run: python validate_cli.py  →  choose [8]"
            )

        active = [m for m in mappings if not m.skip_validation]

        # Short-circuit for Snowflake targets when any column uses LATERAL FLATTEN.
        # Snowflake rejects LATERAL FLATTEN inside correlated scalar subqueries
        # ("Unsupported subquery type cannot be evaluated"), regardless of how the
        # correlation is expressed.  The CTE-based builder avoids scalar subqueries
        # entirely by moving FLATTEN to the FROM clause.
        if target_db_type.lower() == "snowflake" and any(
            getattr(m.rule, "snowflake_needs_cte", False) for m in active
        ):
            print(
                f"  [AISQLGenerator] Using CTE-based Snowflake query for {target_fqn} "
                f"(LATERAL FLATTEN columns detected)"
            )
            sql = self._build_snowflake_cte_query(target_fqn, active, has_fivetran_active, target_filter)
            return AIGeneratedQuery(
                query=sql,
                database_type=target_db_type,
                explanation="CTE-based Snowflake query (LATERAL FLATTEN in FROM clause)",
                confidence=0.99,
                warnings=[],
            )

        pk_target_col = active[0].target_column if active else None

        def _target_expr(m) -> str:
            alias = f"{m.source_column}_normalized"
            rule = m.rule
            if pk_target_col and hasattr(rule, "apply_snowflake_correlated"):
                return rule.apply_snowflake_correlated(
                    m.target_column, target_fqn, pk_target_col, alias=alias
                )
            return rule.apply_snowflake(m.target_column, alias=alias)

        columns_json = [
            {
                "target_column":    m.target_column,
                "target_type":      m.target_type,
                "source_column":    m.source_column,
                "source_type":      m.source_type,
                "required_alias":   f"{m.source_column}_normalized",
                "rule":             m.rule.rule_name,
                "sql_expression":   _target_expr(m),
            }
            for m in active
        ]

        filter_parts = []
        if has_fivetran_active:
            filter_parts.append("_FIVETRAN_ACTIVE = TRUE")
        if target_filter:
            filter_parts.append(target_filter)
        fivetran_note = (
            "\nThe query MUST include: WHERE " + " AND ".join(filter_parts)
            if filter_parts else ""
        )

        # Pre-built SELECT list with verbatim expressions for the AI to copy
        expr_lines = ["Use EXACTLY these pre-computed expressions (one per column, in order):"]
        for item in columns_json:
            expr_lines.append(f"  {item['sql_expression']}")
        expr_block = "\n".join(expr_lines)

        system_prompt = self._build_system_prompt(source_db_type, target_db_type)
        user_prompt = f"""Generate a {target_db_type.upper()} SQL query for: {target_fqn}

Query Type: SELECT normalized columns for row-by-row comparison against the
{source_db_type.upper()} source. The two result sets are compared positionally
AND by alias, so the aliases below are non-negotiable.

Columns ({len(columns_json)} total, in this exact order):
{json.dumps(columns_json, indent=2)}

{expr_block}

Requirements:
1. SELECT the columns in exactly the order listed above.
2. Copy the sql_expression values VERBATIM — do NOT simplify UUID/HSTORE/JSON to a plain CAST.
3. Read from the column named in "target_column".
4. Alias each expression with EXACTLY the string in "required_alias" — same
   spelling and same case. Quote the alias with double quotes so
   {target_db_type.upper()} preserves the case verbatim.
5. Wrap every expression: COALESCE(CAST(expression AS {self._get_text_type(target_db_type)}), '{NULL_PLACEHOLDER}')
   (already included in the sql_expression — do not double-wrap)
6. Timestamps: {self._get_format_function(target_db_type)}
7. Booleans: CASE WHEN col = TRUE THEN '1' WHEN col = FALSE THEN '0' ELSE NULL END
8. Apply the same normalization semantics the source side uses, so equal data
   produces byte-identical strings on both sides.{fivetran_note}

Generate the complete SELECT query now:
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]
        attempts: List[str] = []

        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            raw    = self._call_ai(messages, f"target:{target_fqn}", attempt)
            result = self._parse_response(raw, target_db_type, active, "data_validation")
            if not result.warnings:
                return result

            attempts.append(f"attempt {attempt}: " + "; ".join(result.warnings))
            print(
                f"  [AISQLGenerator] Target attempt {attempt}/{MAX_GENERATION_ATTEMPTS} "
                f"for {target_fqn} failed checks: " + "; ".join(result.warnings)
            )
            messages = messages + [
                {"role": "assistant", "content": result.query},
                {
                    "role": "user",
                    "content": (
                        "That query failed validation for these reasons:\n"
                        + "\n".join(f"- {w}" for w in result.warnings)
                        + f"\n\nRewrite it as valid {target_db_type.upper()} SQL that "
                        "fixes every issue. Return ONLY the SQL."
                    ),
                },
            ]

        raise AISQLGenerationError(
            f"AI could not produce valid {target_db_type.upper()} SQL for "
            f"{target_fqn} after {MAX_GENERATION_ATTEMPTS} attempts.\n"
            + "\n".join(f"  {a}" for a in attempts)
        )

    def _build_snowflake_cte_query(
        self,
        target_fqn: str,
        mappings: List[ColumnRuleMapping],
        has_fivetran_active: bool,
        target_filter: str = "",
    ) -> str:
        """Build a Snowflake SELECT that uses CTEs for JSON/HStore columns.

        Snowflake rejects LATERAL FLATTEN inside any correlated scalar subquery,
        regardless of how the correlation is expressed. Moving FLATTEN to the
        FROM clause via CTEs + LEFT JOIN is the only supported pattern.

        This method is called instead of AI generation when any active column
        has a rule with snowflake_needs_cte=True.
        """
        active = [m for m in mappings if not m.skip_validation]
        if not active:
            raise AISQLGenerationError(
                f"No active column mappings for target {target_fqn}"
            )

        pk_col = active[0].target_column

        cte_blocks: list[str] = []
        join_clauses: list[str] = []
        select_exprs: list[str] = []

        cte_index = 0
        for m in active:
            alias = f"{m.source_column}_normalized"
            rule = m.rule

            if getattr(rule, "snowflake_needs_cte", False):
                cte_name = f"_cte_{m.target_column.lower()}_{cte_index}"
                cte_index += 1
                cte_blocks.append(rule.snowflake_cte_sql(m.target_column, target_fqn, pk_col, cte_name))
                join_clauses.append(rule.snowflake_cte_join_clause(target_fqn, pk_col, cte_name))
                select_exprs.append(rule.snowflake_cte_select_expr(cte_name, alias=alias))
            else:
                expr = rule.apply_snowflake(m.target_column, alias=f'"{alias}"')
                select_exprs.append(expr)

        parts: list[str] = []
        if cte_blocks:
            parts.append("WITH " + ",\n".join(cte_blocks))

        parts.append("SELECT\n  " + ",\n  ".join(select_exprs))
        parts.append(f"FROM {target_fqn}")
        for jc in join_clauses:
            parts.append(jc)
        where_parts = []
        if has_fivetran_active:
            where_parts.append("_FIVETRAN_ACTIVE = TRUE")
        if target_filter:
            where_parts.append(target_filter)
        if where_parts:
            parts.append("WHERE " + " AND ".join(where_parts))

        return "\n".join(parts)

    def generate_custom_query(
        self,
        user_instruction: str,
        table_fqn: str,
        columns: List[dict],
        db_type: str,
    ) -> AIGeneratedQuery:
        """
        Generate a free-form SQL query from a plain-English request, scoped to
        an explicit, human-reviewed column list for ONE real table.

        Args:
            user_instruction: Plain-English description of the desired query.
            table_fqn: The exact table the query must read FROM (e.g.
                "public.employees" or "MYDB.PUBLIC.EMPLOYEES") — required, so
                the model never has to invent a placeholder table name.
            columns: One dict per available column, each with keys "column"
                and "type" for THIS table/dialect — the caller decides which
                columns are in scope (e.g. only the ones checked in a UI
                grid) and which side's names to use; this method does not
                re-derive that from a full mapping run.
            db_type: Dialect the query should be written in.

        Unlike generate_validation_query, there is no fixed alias contract to
        enforce (this isn't a source-vs-target comparison query), so
        validation is looser: still requires a real SELECT...FROM, still
        rejects wrong-dialect syntax, but does not require the
        '<col>_normalized' alias or the NULL placeholder. Uses the same
        self-correcting retry loop as the rest of this class.

        Raises:
            AISQLGenerationError: no API key configured, or every attempt
                failed the (looser) validation checks.
        """
        if not self._ai_active:
            raise AISQLGenerationError(
                "No AI API key configured — cannot generate custom SQL.\n"
                "  Set DIAL_API_KEY or CLAUDE_API_KEY in .env, "
                "or run: python validate_cli.py  →  choose [8] Configure API key"
            )

        columns_json = columns

        system_prompt = self._build_system_prompt(db_type, db_type)
        user_prompt = f"""Generate a {db_type.upper()} SQL query.

Table: {table_fqn}
The query's FROM clause MUST read exactly "{table_fqn}" — do not invent,
alias away, or use a placeholder table name.

Available columns on THIS table (name, type, and the normalization rule
already assigned to it elsewhere in this project, if any):
{json.dumps(columns_json, indent=2)}

User request:
\"\"\"{user_instruction}\"\"\"

Requirements:
1. Use only the column names listed above — do not invent columns.
2. Write valid {db_type.upper()} SQL syntax (see the dialect rules above).
3. If the request involves comparing, deduplicating, or unioning values,
   apply the same normalization style already used for these columns (trim
   text, cast NULLs to a comparable placeholder) so results stay consistent
   with the rest of this project's validation queries.
4. Return ONLY the SQL — no explanation, no markdown fences.

Generate the query now:
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]
        attempts: List[str] = []

        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            raw    = self._call_ai(messages, f"custom:{db_type}", attempt)
            result = self._parse_response(raw, db_type, [], query_type="custom")
            if table_fqn.split(".")[-1].strip('"').upper() not in result.query.upper():
                result.warnings.append(
                    f"Query does not reference the required table '{table_fqn}' — "
                    "it may have used a placeholder table name instead."
                )
            if not result.warnings:
                return result

            attempts.append(f"attempt {attempt}: " + "; ".join(result.warnings))
            print(
                f"  [AISQLGenerator] Custom query attempt {attempt}/{MAX_GENERATION_ATTEMPTS} "
                f"failed checks: " + "; ".join(result.warnings)
            )
            messages = messages + [
                {"role": "assistant", "content": result.query},
                {
                    "role": "user",
                    "content": (
                        "That query failed validation for these reasons:\n"
                        + "\n".join(f"- {w}" for w in result.warnings)
                        + f"\n\nRewrite it as valid {db_type.upper()} SQL that fixes every "
                        "issue above. Return ONLY the SQL."
                    ),
                },
            ]

        raise AISQLGenerationError(
            f"AI could not produce valid {db_type.upper()} SQL for this request "
            f"after {MAX_GENERATION_ATTEMPTS} attempts.\n"
            + "\n".join(f"  {a}" for a in attempts)
        )

    def generate_schema_aware_query(
        self,
        user_instruction: str,
        schema_context: dict,
        db_type: str,
        default_schema: str = "",
        normalize: bool = True,
    ) -> AIGeneratedQuery:
        """
        Generate SQL from a plain-English request with full multi-table schema context.

        Unlike generate_custom_query (scoped to one pre-selected table), this method
        feeds the AI the INFORMATION_SCHEMA of every table the user selected so it can
        write JOINs, subqueries, aggregations, CTEs — anything the DQE asks for.

        Args:
            user_instruction: Plain-English description (e.g. "Show employees under
                each manager with their department, salary, and years of experience,
                ordered by salary descending").
            schema_context: Dict mapping "schema.table" -> list of column dicts
                {column_name, data_type, is_nullable, is_primary_key}.
                Built from extractor.extract_columns() calls in the UI.
            db_type: SQL dialect to write (postgresql, mssql, athena, snowflake).
            default_schema: The schema the tables live in — used in the prompt so
                the AI knows whether to prefix table names.

        Returns:
            AIGeneratedQuery with validated SQL.

        Raises:
            AISQLGenerationError: no API key, or every attempt failed validation.
        """
        if not self._ai_active:
            raise AISQLGenerationError(
                "No AI API key configured — cannot generate schema-aware SQL.\n"
                "  Set DIAL_API_KEY or CLAUDE_API_KEY in .env."
            )

        if not schema_context:
            raise AISQLGenerationError("schema_context is empty — select at least one table.")

        # Build a compact but complete schema dump the AI can reason over
        schema_lines = []
        for table_fqn, columns in schema_context.items():
            col_lines = []
            for c in columns:
                pk_marker = " [PK]" if c.get("is_primary_key") else ""
                null_marker = " NULL" if c.get("is_nullable", True) else " NOT NULL"
                col_lines.append(f"    {c['column_name']}  {c['data_type'].upper()}{null_marker}{pk_marker}")
            schema_lines.append(f"  TABLE {table_fqn} (\n" + "\n".join(col_lines) + "\n  )")

        schema_block = "\n".join(schema_lines)
        table_list = ", ".join(schema_context.keys())

        if normalize:
            system_prompt = self._build_system_prompt(db_type, db_type)
        else:
            system_prompt = (
                f"You are an expert SQL developer. Write clean, readable {db_type.upper()} SQL. "
                "Use correct dialect syntax. No COALESCE normalization, no <<NULL>> sentinels. "
                "Return ONLY the SQL — no markdown, no explanation."
            )
        user_prompt = f"""You are writing a {db_type.upper()} SQL query for a Data Quality Engineer.

## Available tables and their schemas

{schema_block}

## Your task

{user_instruction}

## Requirements
1. Write valid {db_type.upper()} SQL only — use the correct dialect (see system rules).
2. Use only the tables and columns listed above — do NOT invent columns or table names.
3. When referencing tables, prefix with the schema "{default_schema}." if it is non-empty.
4. JOINs are allowed (and often necessary). Use the column types listed to choose
   the right JOIN key — primary key columns are marked [PK].
5. Aggregations, GROUP BY, ORDER BY, CTEs, subqueries are all allowed — write
   whatever SQL structure best answers the request.
6. The query is for DATA QUALITY VALIDATION, not an application query — it may read
   any rows from any listed table. Return ALL rows that satisfy the request (no arbitrary LIMIT).
7. Return ONLY the SQL — no markdown fences, no explanation.

Generate the {db_type.upper()} query now:
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]
        attempts: List[str] = []

        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            raw    = self._call_ai(messages, f"schema_aware:{db_type}", attempt)
            result = self._parse_response(raw, db_type, [], query_type="custom")

            # Verify at least one of the selected tables is referenced
            upper_q = result.query.upper()
            any_table = any(
                t.split(".")[-1].strip('"').upper() in upper_q
                for t in schema_context.keys()
            )
            if not any_table:
                result.warnings.append(
                    "Query does not reference any of the selected tables — "
                    "it may have used placeholder names."
                )

            if not result.warnings:
                return result

            attempts.append(f"attempt {attempt}: " + "; ".join(result.warnings))
            messages = messages + [
                {"role": "assistant", "content": result.query},
                {
                    "role": "user",
                    "content": (
                        "That query failed validation for these reasons:\n"
                        + "\n".join(f"- {w}" for w in result.warnings)
                        + f"\n\nRewrite it as valid {db_type.upper()} SQL that fixes every "
                        "issue above. Return ONLY the SQL."
                    ),
                },
            ]

        raise AISQLGenerationError(
            f"AI could not produce valid {db_type.upper()} SQL after "
            f"{MAX_GENERATION_ATTEMPTS} attempts.\n"
            + "\n".join(f"  {a}" for a in attempts)
        )

    # -----------------------------------------------------------------------
    # Backend dispatch helpers
    # -----------------------------------------------------------------------

    def _call_ai(self, messages: list, context: str, attempt: int) -> str:
        """Dispatch to the correct backend (DIAL or Claude direct)."""
        if self._backend == _BACKEND_CLAUDE:
            return self._call_claude(messages, context, attempt)
        return self._call_dial(messages, context, attempt)

    def _call_dial(self, messages: list, context: str, attempt: int) -> str:
        """Call EPAM DIAL via AzureOpenAI SDK."""
        try:
            from openai import AzureOpenAI
        except ImportError as exc:
            raise AISQLGenerationError(
                "The 'openai' package is required for DIAL SQL generation.\n"
                "Install it with: pip install -r requirements.txt"
            ) from exc
        client = AzureOpenAI(
            api_key=self.api_key,
            api_version=self.api_version,
            azure_endpoint=self.api_base,
        )
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0,
                extra_headers={"Api-Key": self.api_key},
            )
            usage = extract_openai_usage(response)
            log_usage(
                backend="dial",
                model=self.model,
                call_type="sql_generation",
                context=context,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                attempt=attempt,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            raise AISQLGenerationError(
                f"DIAL API call failed for {context} "
                f"on attempt {attempt}/{MAX_GENERATION_ATTEMPTS}: {exc}"
            ) from exc

    def _call_claude(self, messages: list, context: str, attempt: int) -> str:
        """
        Call Anthropic Claude directly.
        Converts OpenAI-style messages list to Anthropic format.
        """
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise AISQLGenerationError(
                "The 'anthropic' package is required for Claude direct SQL generation.\n"
                "Install it with: pip install anthropic"
            ) from exc

        client = anthropic.Anthropic(api_key=self.api_key)

        # Extract system prompt and conversation messages
        system_content = ""
        conv_messages  = []
        for msg in messages:
            if msg["role"] == "system":
                system_content = msg["content"]
            else:
                conv_messages.append({"role": msg["role"], "content": msg["content"]})

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_content,
                messages=conv_messages,
            )
        except Exception as exc:
            raise AISQLGenerationError(
                f"Claude API call failed for {context} "
                f"on attempt {attempt}/{MAX_GENERATION_ATTEMPTS}: {exc}"
            ) from exc

        usage = extract_anthropic_usage(response)
        log_usage(
            backend="claude",
            model=self.model,
            call_type="sql_generation",
            context=context,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            attempt=attempt,
        )

        raw = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw += block.text

        # Strip markdown fences if Claude wrapped SQL in ```sql ... ```
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw   = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            ).strip()
        return raw

    # -----------------------------------------------------------------------
    # Prompt builders
    # -----------------------------------------------------------------------

    def _build_system_prompt(self, source_db: str, target_db: str) -> str:
        """Build the system prompt with database-specific instructions."""
        return f"""You are an expert SQL Query Generator specializing in data migration validation.

Your task: Generate database-specific SQL queries for validation between {source_db.upper()} (source) and {target_db.upper()} (target).

## Database-Specific Syntax Rules:

### MS SQL Server (MSSQL)
- NO TEXT data type — use VARCHAR(MAX) for text casting
- NO TO_CHAR function — use FORMAT() or CONVERT()
- Integers: CAST(column AS VARCHAR(MAX)) NOT CAST(column AS TEXT)
- Timestamps: FORMAT(column, 'yyyy-MM-dd HH:mm:ss')
- Booleans: Use BIT type (0/1), no true/false keywords
- String trimming: LTRIM(RTRIM(column))
- NULL coalesce: COALESCE(CAST(column AS VARCHAR(MAX)), '<<NULL>>')

### PostgreSQL
- Use TEXT for text casting: CAST(column AS TEXT)
- Use TO_CHAR for formatting: TO_CHAR(column, 'YYYY-MM-DD HH24:MI:SS')
- Booleans: true/false keywords supported
- String trimming: TRIM(column)
- NULL coalesce: COALESCE(CAST(column AS TEXT), '<<NULL>>')

### Snowflake
- Use STRING for text casting: CAST(column AS STRING)
- Use TO_VARCHAR for formatting: TO_VARCHAR(column, 'YYYY-MM-DD HH24:MI:SS')
- Booleans: TRUE/FALSE keywords
- String trimming: TRIM(column)
- NULL coalesce: COALESCE(CAST(column AS STRING), '<<NULL>>')

### Athena/Trino/Presto
- Use VARCHAR for text casting: CAST(column AS VARCHAR)
- Use date_format for formatting: date_format(column, '%Y-%m-%d %H:%i:%s')
- String trimming: TRIM(column)
- NULL coalesce: COALESCE(CAST(column AS VARCHAR), '<<NULL>>')

## MANDATORY Type-Specific Normalization Rules (USE EXACTLY AS SHOWN):

### UUID — use UPPER+TRIM so both sides compare case-insensitively
- PostgreSQL source: COALESCE(CAST(UPPER(TRIM(CAST(col AS TEXT))) AS TEXT), '<<NULL>>')
- Snowflake target:  COALESCE(CAST(UPPER(TRIM(CAST(col AS STRING))) AS STRING), '<<NULL>>')
- MSSQL source:      COALESCE(CAST(UPPER(LTRIM(RTRIM(CAST(col AS VARCHAR(MAX))))) AS VARCHAR(MAX)), '<<NULL>>')
- Athena source:     COALESCE(CAST(UPPER(TRIM(CAST(col AS VARCHAR))) AS VARCHAR), '<<NULL>>')
- NEVER use plain CAST(col AS TEXT/STRING) for UUID — always apply UPPER(TRIM(...)).

### HSTORE (PostgreSQL) — raw JSON text; canonicalized in Python, NOT in SQL
- PostgreSQL source:
  COALESCE(CAST(CAST(hstore_to_json(col) AS TEXT) AS TEXT), '<<NULL>>')
- Snowflake target:
  COALESCE(CAST(COALESCE(TO_JSON(TRY_PARSE_JSON(CAST(col AS STRING))), CAST(col AS STRING)) AS STRING), '<<NULL>>')
- Do NOT sort keys, flatten, or build key=value pairs in SQL. Do NOT emit
  '<<EMPTY>>' or '<<INVALID_JSON>>' sentinels.

### JSONB / JSON — raw document text; canonicalized in Python, NOT in SQL
- PostgreSQL source:
  COALESCE(CAST(CAST(col AS TEXT) AS TEXT), '<<NULL>>')
- Snowflake target:
  COALESCE(CAST(COALESCE(TO_JSON(TRY_PARSE_JSON(CAST(col AS STRING))), CAST(col AS STRING)) AS STRING), '<<NULL>>')
- Do NOT emit '<<EMPTY>>' or '<<INVALID_JSON>>' sentinels.

### Why JSON/JSONB/HSTORE must NOT be canonicalized in SQL
Both sides emit the document verbatim and Python canonicalizes them
(Project/utils/semantic_normalize.py: recursive key sort, recursion into values
that are themselves JSON documents, number normalization). Building a canonical
string in SQL was tried and does not work, because the two engines disagree:
  - Snowflake LISTAGG returns '' (not NULL) when all inputs are NULL, so the
    NULL/empty sentinel fallback never fires and a NULL column compares '' to
    '<<EMPTY>>'.
  - ORDER BY collation differs — Postgres uses the DB collation (en_US.UTF-8
    ignores '_' and case at the primary level), Snowflake orders by codepoint.
  - jsonb_each() raises "cannot call jsonb_each on a non-object" for a
    top-level array or scalar, aborting the whole query.
  - A value that is a string containing JSON compared byte-for-byte, so any
    re-serialization by the loader read as data drift.
Therefore: NEVER use LATERAL FLATTEN, LISTAGG, WITH RECURSIVE, string_agg,
jsonb_each, jsonb_array_elements, hstore_to_jsonb or PARSE_JSON-with-sorting for
these columns. Emit exactly the expressions above.

## Critical Requirements:
1. ALWAYS use database-specific syntax — never mix dialects.
2. For UUID columns: ALWAYS apply UPPER(TRIM(...)) — never bare CAST.
3. For HSTORE columns: emit the raw-JSON expressions shown above — never flatten or sort in SQL.
4. For JSON/JSONB columns: emit the raw-document expressions shown above — never flatten or sort in SQL.
5. Preserve numeric precision and scale before text conversion.
6. Normalize timezone semantics before formatting timestamps; do not silently drop offsets.
7. ALL normalized data-validation columns MUST have COALESCE(..., '<<NULL>>').
8. Text casts MUST match the database type (VARCHAR(MAX) for MSSQL, TEXT for PG, STRING for SF).
9. Format functions MUST match the database (FORMAT for MSSQL, TO_CHAR for PG, TO_VARCHAR for SF).
10. Use commas between every SELECT expression and preserve requested aliases exactly.
11. Include the target active-record filter when requested; never mix source and target syntax.
12. Return ONLY the SQL query — no markdown, no explanation.
13. Mentally validate the complete query as executable SQL before returning it.

## NULL Handling (CRITICAL):
- Source query: COALESCE(CAST(expression AS <database_text_type>), '<<NULL>>')
- Target query: COALESCE(CAST(expression AS STRING), '<<NULL>>')

## Output Format:
Return plain SQL query only. No markdown, no comments, no explanations outside the SQL.
"""

    def _build_user_prompt(
        self,
        schema: str,
        table: str,
        mappings: List[ColumnRuleMapping],
        source_db_type: str,
        query_type: str,
        has_fivetran_active: bool,
        source_filter: str = "",
    ) -> str:
        """Build the user prompt with column details including exact SQL expressions."""
        active = [m for m in mappings if not m.skip_validation]
        columns_json = [
            {
                "source_column": m.source_column,
                "target_column": m.target_column,
                "source_type": m.source_type,
                "target_type": m.target_type,
                "rule": m.rule.rule_name,
                "sql_expression": m.rule.apply_source(source_db_type, m.source_column, alias=f"{m.source_column}_normalized"),
            }
            for m in active
        ]

        filter_note = ""
        if source_filter:
            filter_note = f"\nThe query MUST include: WHERE {source_filter}"

        query_descriptions = {
            "data_validation": "SELECT normalized columns for row-by-row comparison",
            "null_pct": "SELECT NULL percentage per column: ROUND(100.0 * SUM(CASE WHEN col IS NULL THEN 1 ELSE 0 END) / COUNT(*), 2)",
            "distinct_count": "SELECT distinct value count per column: COUNT(DISTINCT col)",
        }

        compatibility = self._build_compatibility_matrix(mappings, source_db_type, "snowflake")

        # For data_validation queries, also emit the per-column expressions as a
        # ready-to-copy SELECT list so the AI can use them verbatim.
        expr_block = ""
        if query_type == "data_validation":
            expr_lines = ["Use EXACTLY these pre-computed expressions (one per column, in order):"]
            for item in columns_json:
                expr_lines.append(f"  {item['sql_expression']}")
            expr_block = "\n" + "\n".join(expr_lines) + "\n"

        return f"""Generate {source_db_type.upper()} SQL query for: {schema}.{table}

Query Type: {query_descriptions.get(query_type, query_type)}

Columns to process ({len(columns_json)} total):
{json.dumps(columns_json, indent=2)}

Source → target compatibility decisions:
{compatibility}
{expr_block}
Requirements:
1. Use {source_db_type.upper()}-specific syntax
2. For data_validation: copy the sql_expression values VERBATIM — do NOT simplify UUID/HSTORE/JSON to a plain CAST
3. ALL columns MUST be wrapped: COALESCE(CAST(expression AS {self._get_text_type(source_db_type)}), '<<NULL>>')
4. Integers: CAST(col AS {self._get_text_type(source_db_type)})
5. Timestamps: {self._get_format_function(source_db_type)}
6. Booleans: CASE WHEN col = {self._get_bool_true(source_db_type)} THEN '1' WHEN col = {self._get_bool_false(source_db_type)} THEN '0' ELSE NULL END
7. Each normalized column MUST have alias: column_name_normalized{filter_note}

Generate the complete SELECT query now:
"""

    def _build_compatibility_matrix(
        self,
        mappings: List[ColumnRuleMapping],
        source_db_type: str,
        target_db_type: str,
    ) -> str:
        """Give the model explicit per-column castability context."""
        lines = []
        for mapping in mappings:
            if mapping.skip_validation:
                continue
            lines.append(
                f"- {mapping.source_column} -> {mapping.target_column}: "
                f"{mapping.source_type.upper()} -> {mapping.target_type.upper()}; "
                f"rule={mapping.rule.rule_name}; "
                f"source_text_cast={self._get_text_type(source_db_type)}; "
                f"target_text_cast={self._get_text_type(target_db_type)}"
            )
        return "\n".join(lines) or "- No comparable columns"

    def _get_text_type(self, db_type: str) -> str:
        """Get the text data type for the database."""
        db = db_type.lower()
        if db in ("mssql", "sqlserver", "sql_server"):
            return "VARCHAR(MAX)"
        elif db in ("postgres", "postgresql"):
            return "TEXT"
        elif db in ("snowflake",):
            return "STRING"
        elif db in ("athena", "trino", "presto"):
            return "VARCHAR"
        return "TEXT"

    def _get_format_function(self, db_type: str) -> str:
        """Get the timestamp format function for the database."""
        db = db_type.lower()
        if db in ("mssql", "sqlserver", "sql_server"):
            return "FORMAT(col, 'yyyy-MM-dd HH:mm:ss')"
        elif db in ("postgres", "postgresql"):
            return "TO_CHAR(col, 'YYYY-MM-DD HH24:MI:SS')"
        elif db in ("snowflake",):
            return "TO_VARCHAR(col, 'YYYY-MM-DD HH24:MI:SS')"
        elif db in ("athena", "trino", "presto"):
            return "date_format(col, '%Y-%m-%d %H:%i:%s')"
        return "TO_CHAR(col, 'YYYY-MM-DD HH24:MI:SS')"

    def _get_bool_true(self, db_type: str) -> str:
        """Get the boolean TRUE value for the database."""
        db = db_type.lower()
        if db in ("mssql", "sqlserver", "sql_server"):
            return "1"
        return "true"

    def _get_bool_false(self, db_type: str) -> str:
        """Get the boolean FALSE value for the database."""
        db = db_type.lower()
        if db in ("mssql", "sqlserver", "sql_server"):
            return "0"
        return "false"

    def _parse_response(
        self,
        raw: str,
        db_type: str,
        mappings: Optional[List[ColumnRuleMapping]] = None,
        query_type: str = "data_validation",
    ) -> AIGeneratedQuery:
        """Parse AI response and extract the query."""
        # Strip markdown code fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            # Remove first line (```sql or ```) and last line (```)
            if lines[-1].strip() == "```":
                cleaned = "\n".join(lines[1:-1])
            else:
                cleaned = "\n".join(lines[1:])
            cleaned = cleaned.strip()

        warnings = self._validate_generated_query(cleaned, db_type, mappings or [], query_type)

        return AIGeneratedQuery(
            query=cleaned,
            database_type=db_type,
            explanation=f"AI-generated query for {db_type}",
            confidence=0.95 if not warnings else 0.7,
            warnings=warnings,
        )

    def _validate_generated_query(
        self,
        query: str,
        db_type: str,
        mappings: List[ColumnRuleMapping],
        query_type: str,
    ) -> List[str]:
        """Reject unsafe or incomplete AI output before it reaches SQL files."""
        warnings = []
        upper = query.upper()
        dialect = db_type.lower().replace("server", "")
        if not re.search(r"\bSELECT\b", upper) or not re.search(r"\bFROM\b", upper):
            warnings.append("AI response is not a complete SELECT query")

        # These generated queries are read-only validation SELECTs by design. The AI
        # backend is an external dependency and its output must never be trusted to
        # be inert — reject anything containing a destructive/DDL/DML keyword or a
        # second statement (stacked via `;`), rather than letting it through with a
        # lower confidence score. Any non-empty warning here already forces a retry
        # and, after exhausting attempts, a hard failure — see the calling loop.
        destructive = re.findall(
            r"\b(DROP|DELETE|INSERT|UPDATE|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|EXEC|EXECUTE|MERGE|CALL)\b",
            upper,
        )
        if destructive:
            warnings.append(
                f"AI response contains forbidden non-SELECT keyword(s): {sorted(set(destructive))}"
            )
        stripped = query.strip().rstrip(";")
        if ";" in stripped:
            warnings.append("AI response contains multiple statements (unexpected ';')")
        if dialect in {"mssql", "sqlserver"}:
            forbidden = {
                r"::\s*[A-Z_]": "PostgreSQL cast operator (::)",
                r"\bTO_CHAR\s*\(": "PostgreSQL TO_CHAR",
                r"\bAS\s+TEXT\b": "PostgreSQL TEXT cast",
                r"\bJSONB\b": "PostgreSQL JSONB",
                r"\bENCODE\s*\(": "PostgreSQL encode()",
            }
            for pattern, label in forbidden.items():
                if re.search(pattern, upper):
                    warnings.append(f"MSSQL query contains {label}")
        if query_type == "data_validation":
            if "<<NULL>>" not in query:
                warnings.append("Data-validation query is missing the NULL placeholder")
            for mapping in mappings:
                if mapping.skip_validation:
                    continue
                alias = f"{mapping.source_column}_normalized".upper()
                if alias not in upper:
                    warnings.append(f"Missing required alias {mapping.source_column}_normalized")
            # Detect adjacent SELECT function expressions without a comma.
            # Exclude SQL keywords that legitimately follow ')' without a comma:
            #   AS (CTE / alias), WITH (CTE anchor), FROM (subquery end),
            #   WHERE, WITHIN (WITHIN GROUP), UNION, ORDER, OVER, THEN,
            #   WHEN, ELSE, END, AND, OR, NOT, IN, ON, AT (AT TIME ZONE),
            #   ORDINALITY (jsonb_array_elements ... WITH ORDINALITY).
            _SQL_KEYWORDS_AFTER_PAREN = (
                r"AS|WITH|FROM|WHERE|WITHIN|UNION|ORDER|OVER|THEN|WHEN|ELSE|END"
                r"|AND|OR|NOT|IN|ON|AT|ORDINALITY|GROUP|HAVING|LATERAL|RECURSIVE"
            )
            _comma_check = re.compile(
                rf"\)\s+(?!(?:{_SQL_KEYWORDS_AFTER_PAREN})\b)\w+\s*\(", re.IGNORECASE
            )
            if _comma_check.search(query):
                warnings.append("SELECT expressions may be missing commas")
        return warnings
