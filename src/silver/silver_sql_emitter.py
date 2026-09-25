"""
Silver-only SQL emission -- ADR 0018 (verbatim Coalesce transform, no
generic Bronze normalization wrapper) and ADR 0019 (multisource join
concatenation).

This bypasses src/generated_queries/sql_query_generator.py ->
ai_sql_generator.py -> rules/base_rules.py entirely: that path wraps every
column in COALESCE(CAST(col AS STRING), '<<NULL>>') to bridge *heterogeneous*
source/target types (e.g. Postgres hstore vs Snowflake VARIANT). Silver is
Snowflake-to-Snowflake -- that heterogeneity problem doesn't exist, and the
wrapper would fight the user's actual requirement: emit the Coalesce
metadata's own declared transform, exactly as written.

Only entrypoint: emit_query_set(plan) -> ValidationQuerySet (the same shape
generated_queries/sql_query_generator.py's SQLQueryGenerator produces, so
YAMLConfigWriter.write_from_plan() keeps working unchanged, per ADR 0018 4).

resolve_ref_macro() is exported so coalesce_plan_builder.py can reuse the
exact same `{{ ref('LOCATION', 'NODE') }}` resolver when it builds
plan.population_scope["bronze_join_sql"] for multisource nodes (ADR 0019 3) --
implemented once, shared by both call sites, not duplicated.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, List

from generated_queries.sql_query_generator import ValidationQuerySet

if TYPE_CHECKING:
    from core.validation_plan import CanonicalValidationPlan

_REF_MACRO_RE = re.compile(r"\{\{\s*ref\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)\s*\}\}")


def resolve_ref_macro(text: str, bronze_schema: str) -> str:
    """Resolve every Coalesce `{{ ref('LOCATION', 'NODE') }}` macro in *text*
    to a real `"LOCATION"."schema"."NODE"` reference. Everything else in
    *text* (the surrounding FROM/JOIN/ON SQL) passes through verbatim -- ADR
    0019 3 explicitly rejects parsing joinCondition into a structured join
    spec, only the ref() macro itself gets resolved.

    bronze_schema is the Bronze-side schema to plug into the resolved
    reference. Per ADR 0014 2 (still unverified against a second real
    sample), this reuses the Silver node's own schema for every Bronze table
    -- a known, flagged assumption, not something this function decides on
    its own.
    """
    def _sub(match: "re.Match[str]") -> str:
        location, node = match.group(1), match.group(2)
        return f'"{location}"."{bronze_schema}"."{node}"'
    return _REF_MACRO_RE.sub(_sub, text or "")


def _bronze_select_lines(plan: "CanonicalValidationPlan") -> List[str]:
    """One line per column, in metadata order (ADR 0018 1):
      - skipped (macro-computed / non-deterministic / unclassifiable) -> omitted
        entirely, no inline comment (ADR 0022 -- a comment survives as long as
        the query stays multi-line, but there is no query-language-agnostic
        comment syntax that's safe if this ever gets flattened again; the
        `skip_reason` on each mapping, persisted via PlanStore, is the record
        of what was excluded and why).
      - everything else (passthrough / recomputable expression) -> selected verbatim.
    """
    selected = [m for m in plan.mappings if not m.skip_validation]
    return [
        f'{mapping.source_column} AS "{mapping.target_column}"' + ("," if i < len(selected) - 1 else "")
        for i, mapping in enumerate(selected)
    ]


def _bronze_from_clause(plan: "CanonicalValidationPlan") -> str:
    """FROM/JOIN clause for the Bronze recompute query.

    Multisource nodes: plan.population_scope["bronze_join_sql"] (already
    macro-resolved, Coalesce's own join SQL concatenated verbatim -- ADR 0019
    3/4). Single-source nodes (the common case, untouched): the plain
    single-table FROM clause built from the plan's own source identity.
    """
    join_sql = (plan.population_scope or {}).get("bronze_join_sql")
    if join_sql:
        return join_sql
    return f'FROM "{plan.source_database}"."{plan.source_schema}"."{plan.source_table}"'


def _silver_select_lines(plan: "CanonicalValidationPlan") -> List[str]:
    active = plan.active_mappings
    return [
        f'"{mapping.target_column}"' + ("," if i < len(active) - 1 else "")
        for i, mapping in enumerate(active)
    ]


def emit_query_set(plan: "CanonicalValidationPlan") -> ValidationQuerySet:
    """Build the Bronze recompute SQL and the plain Silver SELECT directly
    from *plan*, with none of the normalization/cast wrapping
    AISQLQueryGenerator applies for Bronze (ADR 0018). Returns the same
    ValidationQuerySet shape YAMLConfigWriter.write_from_plan() already
    consumes -- no change needed there.
    """
    bronze_select = "\n    ".join(_bronze_select_lines(plan))
    silver_select = "\n    ".join(_silver_select_lines(plan))

    main_validation_source = f"SELECT\n    {bronze_select}\n{_bronze_from_clause(plan)}"
    main_validation_target = (
        f"SELECT\n    {silver_select}\n"
        f'FROM "{plan.target_database}"."{plan.target_schema}"."{plan.target_table}"'
    )
    row_count_source = f"SELECT COUNT(*) AS count\n{_bronze_from_clause(plan)}"
    row_count_target = (
        f'SELECT COUNT(*) AS count FROM "{plan.target_database}"."{plan.target_schema}"."{plan.target_table}"'
    )

    return ValidationQuerySet(
        table_name=plan.source_table,
        source_db_label=f"snowflake://{plan.source_database}.{plan.source_schema}.{plan.source_table}",
        target_db_label=f"snowflake://{plan.target_database}.{plan.target_schema}.{plan.target_table}",
        generated_by="coalesce_metadata",
        model_used="N/A",
        main_validation_source=main_validation_source,
        main_validation_target=main_validation_target,
        row_count_source=row_count_source,
        row_count_target=row_count_target,
    )
