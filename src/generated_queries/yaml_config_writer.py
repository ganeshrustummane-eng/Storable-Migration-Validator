"""
YAML Config Writer
===================
Generates YAML validation configuration files AUTOMATICALLY —
triggered by QueryOutputManager.generate().

YOU NEVER CREATE YAML FILES MANUALLY.
When the pipeline runs for any table, it automatically produces:
  Project/config/bronze/data_validation/<table>.yaml
      ← data validation queries (column-level comparison)
  Project/config/bronze/count_validation/bronze.yaml
      ← all tables' row count blocks in one shared file

NOTE: Migration Validator only generates YAML configs.
      The PROJECT folder executor consumes and runs them.

Output format — data_validation YAML (per-table file):
────────────────────────────────────────────────────────────────
tables:
  <source_table>:
    validations:
      data_validation:            ← normalised full-scan SELECT
        source_table_name: ...
        sourcecolumn: <first_column>
        sourcequery: |
          SELECT col1_normalized, ... FROM ...;
        target_table_name: ...
        pktargetcolumn: <first_column>   # source-derived alias, NOT the target's name
        targetquery: |
          SELECT col1_normalized, ... FROM ...;
────────────────────────────────────────────────────────────────

Output format — bronze_count_validation.yaml (shared file):
────────────────────────────────────────────────────────────────
tables:
  <table_1>:
    validations:
      count_validation:           ← ① / ② COUNT(*) only
        source_table_name: ...
        source: postgres
        sourcequery: |
          SELECT count(*) as count from ...;
        target_table_name: ...
        target: snowflake
        targetquery: |
          SELECT count(*) as count from ...;

  <table_2>:
    validations:
      count_validation:
        ...
────────────────────────────────────────────────────────────────

Key design decisions:
  - pksourcecolumn / pktargetcolumn only on data_validation (not needed for aggregates)
  - YAML literal block scalar (|) used for all multi-line queries
  - Query content is indented 10 spaces (YAML requires > 8 for nested block)
  - Only the generator header comment is stripped; all SELECT lines kept
  - NULL placeholder: <<NULL>>  (consistent with all SQL rules)
  - Fivetran: WHERE _FIVETRAN_ACTIVE = TRUE added on Snowflake side when detected
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

import yaml

from ai_transformation.column_mapping import ColumnRuleMapping
from generated_queries.sql_query_generator import ValidationQuerySet, _plan_to_rule_mappings

if TYPE_CHECKING:
    from core.validation_plan import CanonicalValidationPlan


# Bronze config output root: Project/config/bronze/
# Migration Validator generates YAMLs here; PROJECT folder consumes them
_BRONZE_CONFIG_DIR = Path(__file__).parent.parent.parent / "Project" / "config" / "bronze"

# Indentation for YAML literal block scalar content.
# The 'sourcequery: |' key sits at 8-space depth inside the YAML tree.
# All content lines must be indented MORE than 8 → we use 10 spaces.
_QUERY_INDENT = 10


class YAMLConfigWriter:
    """
    Writes YAML validation config files automatically when the pipeline runs.

    This class is called by QueryOutputManager.generate() — never directly.
    For every table the pipeline processes, one YAML file is written to
    config/bronze/data_validation/<table_name>_validation.yaml

    The YAML embeds the full normalised SELECT queries for both source
    and target (Snowflake) so automated validation runners can consume
    the file without any manual editing.
    """

    def write(
        self,
        query_set: ValidationQuerySet,
        source_db_type: str,
        pg_schema: str,
        pg_table: str,
        sf_database: str,
        sf_schema: str,
        sf_table: str,
        mappings: List[ColumnRuleMapping],
        has_fivetran_active: bool = False,
        output_dir: Optional[Path] = None,
        source_audit_column: str = "",
        target_audit_column: str = "",
        source_database: str = "",
        source_primary_keys: Optional[List[str]] = None,
        target_primary_keys: Optional[List[str]] = None,
        plan: Optional["CanonicalValidationPlan"] = None,
        layer: str = "bronze",
    ) -> Path:
        """
        Write the data validation YAML for a single table.

        Output: config/bronze/data_validation/<table>_validation.yaml

        Contains three validation blocks (row count is in the separate
        bronze_count_validation.yaml written by write_count_yaml):
          data_validation           — normalised full-scan SELECT
          null_pct_validation       — NULL % per column
          distinct_count_validation — distinct value counts per column

        Args:
            query_set          : ValidationQuerySet with the generated SQL
            source_db_type     : Source database type (e.g. 'postgresql', 'mssql')
            pg_schema          : PostgreSQL schema (e.g. 'public')
            pg_table           : PostgreSQL table name
            sf_database        : Snowflake database name
            sf_schema          : Snowflake schema name
            sf_table           : Snowflake table name
            mappings           : Active ColumnRuleMapping list
            has_fivetran_active: True → WHERE _FIVETRAN_ACTIVE = TRUE on SF side
            output_dir         : Override output dir (default: config/bronze/data_validation/)
            source_audit_column: Optional source-side audit column name (blank by default)
            target_audit_column: Optional target-side audit column name (blank by default)

        Returns:
            Path to the written YAML file.
        """
        _src_subdir = (source_db_type or "unknown").lower().replace("postgresql", "postgres")
        out_dir = (output_dir or _BRONZE_CONFIG_DIR) / "data_validation" / _src_subdir
        out_dir.mkdir(parents=True, exist_ok=True)

        active = [m for m in mappings if not m.skip_validation]

        # Build normalized PK column names — the SQL SELECT aliases every
        # expression as "{source_col}_normalized", so PK names must match.
        # Use explicit PK lists from the plan when available; fall back to
        # first active column (single-PK tables where list wasn't supplied).
        if source_primary_keys and len(source_primary_keys) > 1:
            src_pk = [f"{c}_normalized" for c in source_primary_keys]
            tgt_pk = src_pk  # target SQL aliases always use source column names
        elif source_primary_keys and len(source_primary_keys) == 1:
            src_pk = f"{source_primary_keys[0]}_normalized"
            tgt_pk = src_pk  # target SQL aliases always use source column names
        else:
            first_src_col = active[0].source_column if active else "id"
            src_pk = f"{first_src_col}_normalized"
            tgt_pk = src_pk

        def _prep(sql: str) -> str:
            clean = _strip_generator_header(sql)
            return _to_indented_multiline(clean) if layer == "silver" else _to_single_line(clean)

        yaml_content = _build_data_yaml(
            table_name_source=pg_table,
            table_name_target=sf_table,
            source_db_type=source_db_type,
            source_database=source_database,
            pg_schema=pg_schema,
            sf_database=sf_database,
            sf_schema=sf_schema,
            src_pk=src_pk,
            tgt_pk=tgt_pk,
            data_source_yaml=_prep(query_set.main_validation_source),
            data_target_yaml=_prep(query_set.main_validation_target),
            null_pct_source_yaml=_prep(query_set.null_pct_source),
            null_pct_target_yaml=_prep(query_set.null_pct_target),
            distinct_source_yaml=_prep(query_set.distinct_count_source),
            distinct_target_yaml=_prep(query_set.distinct_count_target),
            column_count=len(active),
            has_fivetran_active=has_fivetran_active,
            generated_at=query_set.generated_at,
            generated_by=query_set.generated_by,
            model_used=query_set.model_used,
            source_audit_column=source_audit_column,
            target_audit_column=target_audit_column,
            plan_intent={
                "population_scope": plan.population_scope,
                "relationships": [r.to_dict() for r in plan.relationships],
                "identity": {
                    "type": plan.identity_type,
                    "source_primary_keys": plan.source_primary_keys,
                    "target_primary_keys": plan.target_primary_keys,
                    "candidate_keys": plan.candidate_keys,
                },
                "row_hash": plan.row_hash.to_dict() if plan.row_hash else None,
                "transformations": [t.to_dict() for t in plan.transformations],
                "validations": [v.to_dict() for v in plan.validations],
                "requires_review": plan.requires_review,
                "review_reasons": plan.review_reasons,
                "execution_strategy": plan.execution_strategy,
            } if plan is not None else None,
            transformation_source_yaml=_prep(query_set.transformation_source),
            transformation_target_yaml=_prep(query_set.transformation_target),
            aggregate_source_yaml=_prep(query_set.aggregate_source),
            aggregate_target_yaml=_prep(query_set.aggregate_target),
            row_hash_source_yaml=_prep(query_set.row_hash_source),
            row_hash_target_yaml=_prep(query_set.row_hash_target),
        )

        yaml_path = out_dir / f"{pg_table}.yaml"
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(yaml_content)

        print(f"  📋 Data YAML saved     : {yaml_path.resolve()}")
        return yaml_path

    def write_count_yaml(
        self,
        query_set: ValidationQuerySet,
        source_db_type: str,
        pg_schema: str,
        pg_table: str,
        sf_database: str,
        sf_schema: str,
        sf_table: str,
        has_fivetran_active: bool = False,
        output_dir: Optional[Path] = None,
        source_database: str = "",
    ) -> Path:
        """
        Upsert this table's count_validation block into the shared
        config/<layer>/count_validation/<layer>.yaml.

        Idempotent by construction: the existing file is parsed, this table's
        key is replaced (not appended), and the whole document is rewritten.
        Regenerating the same table N times yields byte-identical output.

        The previous implementation appended raw text, so a second run produced
        a duplicate top-level key. YAML resolves duplicates by last-wins, which
        meant the file silently disagreed with itself about what would run.

        Returns:
            Path to the <layer>.yaml file.
        """
        out_dir = (output_dir or _BRONZE_CONFIG_DIR) / "count_validation"
        out_dir.mkdir(parents=True, exist_ok=True)
        layer = out_dir.parent.name or "bronze"
        _source_file = (source_db_type or "unknown").lower().replace("postgresql", "postgres")
        yaml_path = out_dir / f"{_source_file}.yaml"

        block = {
            "validations": {
                "count_validation": {
                    "source_table_name": pg_table,
                    "source": source_db_type,
                    "source_database": source_database,
                    "source_schema": pg_schema,
                    "sourcequery": " ".join(_strip_generator_header(query_set.row_count_source).split()),
                    "target_table_name": sf_table or pg_table,
                    "target": "snowflake",
                    "target_database": sf_database,
                    "target_schema": sf_schema,
                    "targetquery": " ".join(_strip_generator_header(query_set.row_count_target).split()),
                }
            }
        }

        document = _load_yaml_document(yaml_path)
        tables = document.setdefault("tables", {})
        action = "updated" if pg_table in tables else "added"
        tables[pg_table] = block

        header = [
            "# ============================================================",
            f"# Migration Validator — {layer.capitalize()} Count Validation ({_source_file.upper()})",
            f"# Contains   : row count checks for all {_source_file.upper()} tables in the {layer.capitalize()} layer.",
            "#",
            "# GENERATED FILE — do not hand-edit.",
            "# Rendered from the canonical validation plans in output/plans/.",
            "# Re-running generation upserts each table in place; it never appends.",
            "#",
            "# No generation timestamp is recorded here on purpose: this file is",
            "# shared by every table, so a timestamp would make each run produce a",
            "# diff even when nothing changed. Per-table provenance (when, which",
            "# model) lives in the plan JSON.",
            "# ============================================================",
            "",
        ]
        _dump_yaml_document(yaml_path, document, header)

        print(f"  📋 Count YAML {action:<7}: {yaml_path.resolve()}  ({pg_table})")
        return yaml_path

    def write_count_yaml_from_plan(
        self,
        plan: "CanonicalValidationPlan",
        query_set: ValidationQuerySet,
        output_dir: Optional[Path] = None,
    ) -> Path:
        """Write the count-only YAML directly from a CanonicalValidationPlan."""
        return self.write_count_yaml(
            query_set=query_set,
            source_db_type=plan.source_db_type,
            source_database=plan.source_database,
            pg_schema=plan.source_schema,
            pg_table=plan.source_table,
            sf_database=plan.target_database,
            sf_schema=plan.target_schema,
            sf_table=plan.target_table,
            has_fivetran_active=plan.has_fivetran_active,
            output_dir=output_dir,
        )

    def write_from_plan(
        self,
        plan: "CanonicalValidationPlan",
        query_set: ValidationQuerySet,
        output_dir: Optional[Path] = None,
        layer: str = "bronze",
    ) -> Path:
        """
        Write the YAML config file directly from a CanonicalValidationPlan.

        This is the new plan-driven entry point. The plan is the single
        source of truth — no separate column list is needed.

        Args:
            plan      : Fully constructed CanonicalValidationPlan
            query_set : ValidationQuerySet already generated from the same plan
            output_dir: Output directory (default: config/bronze/data_validation/)
            layer     : 'bronze' or 'silver' -- controls only sourcequery/targetquery
                        formatting (see _prep in write()); Silver keeps its
                        readable multi-line SQL instead of being flattened
                        to one line (ADR 0022).

        Returns:
            Path to the written YAML file.
        """
        mappings = _plan_to_rule_mappings(plan.active_mappings)
        return self.write(
            query_set=query_set,
            source_db_type=plan.source_db_type,
            source_database=plan.source_database,
            pg_schema=plan.source_schema,
            pg_table=plan.source_table,
            sf_database=plan.target_database,
            sf_schema=plan.target_schema,
            sf_table=plan.target_table,
            mappings=mappings,
            has_fivetran_active=plan.has_fivetran_active,
            source_primary_keys=plan.source_primary_keys or [],
            target_primary_keys=plan.target_primary_keys or [],
            output_dir=output_dir,
            plan=plan,
            layer=layer,
        )


# ---------------------------------------------------------------------------
# YAML content builder — exact format per project specification
# ---------------------------------------------------------------------------

def _pk_yaml_lines(key: str, pk) -> List[str]:
    """
    Render a pksourcecolumn / pktargetcolumn YAML block.

    Single PK  → '        pksourcecolumn: col_normalized'
    Composite  → '        pksourcecolumn:\n          - col1_normalized\n          - col2_normalized'
    """
    if isinstance(pk, list) and len(pk) > 1:
        lines = [f"        {key}:"]
        for col in pk:
            lines.append(f"          - {col}")
        return lines
    val = pk[0] if isinstance(pk, list) else pk
    return [f"        {key}: {val}"]


def _build_data_yaml(
    table_name_source: str,
    table_name_target: str,
    source_db_type: str,
    source_database: str,
    pg_schema: str,
    sf_database: str,
    sf_schema: str,
    src_pk,       # str (single) or List[str] (composite) — already has _normalized suffix
    tgt_pk,       # str (single) or List[str] (composite) — already has _normalized suffix
    data_source_yaml: str,
    data_target_yaml: str,
    null_pct_source_yaml: str,
    null_pct_target_yaml: str,
    distinct_source_yaml: str,
    distinct_target_yaml: str,
    column_count: int,
    has_fivetran_active: bool,
    generated_at: str,
    generated_by: str,
    model_used: str,
    source_audit_column: str = "",
    target_audit_column: str = "",
    plan_intent: Optional[dict] = None,
    transformation_source_yaml: str = "",
    transformation_target_yaml: str = "",
    aggregate_source_yaml: str = "",
    aggregate_target_yaml: str = "",
    row_hash_source_yaml: str = "",
    row_hash_target_yaml: str = "",
) -> str:
    fivetran_comment = (
        "\n#   - Fivetran  : WHERE _FIVETRAN_ACTIVE = TRUE (Snowflake side — active records only)"
        if has_fivetran_active
        else ""
    )

    lines = [
        "# ============================================================",
        "# Migration Validator — Data Validation Config",
        f"# Table      : {table_name_source}",
        f"# Generated  : {generated_at}",
        f"# By         : {generated_by} (model: {model_used})",
        f"# Columns    : {column_count} comparable columns",
        "#",
        "# Validation blocks (row count is in bronze.yaml):",
        "#   data_validation           — normalised full-scan SELECT (all columns)",
        "#",
        "# Note: null_pct_validation and distinct_count_validation have been",
        "# removed. Only data_validation and count_validation are used.",
        "#",
        "# Normalization rules applied automatically:",
        "#   - Boolean    : TRUE/FALSE -> '1'/'0'",
        "#   - Numeric    : ROUND to 2 decimal places, then text",
        "#   - Timestamp  : 'YYYY-MM-DD HH24:MI:SS'  (microseconds stripped)",
        "#   - Timestamp_TZ: convert to UTC -> 'YYYY-MM-DD HH24:MI:SS'",
        "#   - Date       : 'YYYY-MM-DD'",
        "#   - Text/Char  : TRIM leading/trailing spaces",
        "#   - UUID       : UPPER(TRIM()) — case-insensitive comparison",
        "#   - Integer    : CAST to text",
        "#   - JSON/JSONB : canonical serialization (jsonb::text / TO_JSON)",
        "#   - Bytea      : hex text encoding",
        f"#   - NULL       : COALESCE -> '<<NULL>>' sentinel (ALL columns){fivetran_comment}",
        "# ============================================================",
        "",
        "tables:",
        f"  {table_name_source}:",
        "    validations:",
        "",
        "      # ── ③ / ④ Normalised data validation (all columns) ───────────",
        "      data_validation:",
        f"        source_table_name: {table_name_source}",
        f"        source: {source_db_type}",
        f"        source_database: {source_database}",
        f"        source_schema: {pg_schema}",
        *_pk_yaml_lines("pksourcecolumn", src_pk),
        f"        source_audit_column: {source_audit_column}",
        "        sourcequery: |",
        data_source_yaml,
        f"        target_table_name: {table_name_target}",
        "        target: snowflake",
        f"        target_database: {sf_database}",
        f"        target_schema: {sf_schema}",
        *_pk_yaml_lines("pktargetcolumn", tgt_pk),
        f"        target_audit_column: {target_audit_column}",
        "        targetquery: |",
        data_target_yaml,
        "",
    ]

    if plan_intent:
        lines.extend([
            "      validation_plan:",
            *[f"        {line}" if line else "" for line in yaml.safe_dump(
                plan_intent, sort_keys=False, default_flow_style=False
            ).rstrip().splitlines()],
            "",
        ])

    if transformation_source_yaml and transformation_target_yaml:
        lines.extend([
            "      transformation_validation:",
            f"        source_table_name: {table_name_source}",
            f"        source: {source_db_type}",
            f"        source_database: {source_database}",
            f"        source_schema: {pg_schema}",
            "        sourcequery: |", transformation_source_yaml,
            f"        target_table_name: {table_name_target}",
            "        target: snowflake",
            f"        target_database: {sf_database}",
            f"        target_schema: {sf_schema}",
            "        targetquery: |", transformation_target_yaml,
        ])
    if aggregate_source_yaml and aggregate_target_yaml:
        lines.extend([
            "      aggregate_validation:",
            f"        source_table_name: {table_name_source}",
            f"        source: {source_db_type}",
            f"        source_database: {source_database}",
            f"        source_schema: {pg_schema}",
            "        sourcequery: |", aggregate_source_yaml,
            f"        target_table_name: {table_name_target}",
            "        target: snowflake",
            f"        target_database: {sf_database}",
            f"        target_schema: {sf_schema}",
            "        targetquery: |", aggregate_target_yaml,
        ])

    # Tier-1 hash query for the large-table hybrid engine (Project/tiered_runner.py)
    # — only written when the plan configured a row_hash spec (plan.row_hash).
    # Consumed only when a table's validation_plan.execution_strategy is also
    # set to hybrid_v1; every other table ignores this block entirely, same as
    # transformation_validation/aggregate_validation above.
    if row_hash_source_yaml and row_hash_target_yaml:
        lines.extend([
            "      row_hash_validation:",
            f"        source_table_name: {table_name_source}",
            f"        source: {source_db_type}",
            f"        source_database: {source_database}",
            f"        source_schema: {pg_schema}",
            "        sourcequery: |", row_hash_source_yaml,
            f"        target_table_name: {table_name_target}",
            "        target: snowflake",
            f"        target_database: {sf_database}",
            f"        target_schema: {sf_schema}",
            "        targetquery: |", row_hash_target_yaml,
        ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# YAML document helpers — idempotent upsert support
# ---------------------------------------------------------------------------

class _LiteralStr(str):
    """A string that always serialises as a YAML literal block scalar (|)."""


def _literal_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.add_representer(_LiteralStr, _literal_representer, Dumper=yaml.SafeDumper)


def _load_yaml_document(path: Path) -> dict:
    """
    Read an existing generated YAML file, tolerating absence.

    A malformed file is treated as empty rather than fatal: it is a render
    target that this writer is about to overwrite, so refusing to proceed
    would strand the user with a file only this code can repair.
    """
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        print(f"  [YAMLConfigWriter] {path.name} was unparseable ({exc}); rebuilding it.")
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _dump_yaml_document(path: Path, document: dict, header_lines: List[str]) -> None:
    """Write a YAML document with a comment header, SQL as literal blocks."""
    normalized = _mark_sql_literals(document)
    body = yaml.safe_dump(
        normalized,
        default_flow_style=False,
        sort_keys=False,
        width=10_000,
        allow_unicode=True,
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(header_lines))
        handle.write("\n")
        handle.write(body)


def _mark_sql_literals(node):
    """Recursively tag *query fields so they round-trip as readable SQL blocks."""
    if isinstance(node, dict):
        return {
            key: (
                _LiteralStr(value)
                if isinstance(key, str)
                and key.endswith("query")
                and isinstance(value, str)
                else _mark_sql_literals(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_mark_sql_literals(item) for item in node]
    return node


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

def _strip_generator_header(sql: str) -> str:
    """
    Strip the leading generator comment lines from a SQL string.

    SQLQueryGenerator prefixes each query with one or more comment lines:
        -- ③ SOURCE: MSSQL (dbo.AcctSoftware)
        -- AI-generated (gpt-4o, confidence 0.95)

    All leading '--' lines are removed so the YAML sourcequery/targetquery
    holds clean, runnable SQL. Comments inside the SELECT body are preserved.

    Args:
        sql: SQL string that may begin with header comment lines

    Returns:
        Clean SQL without the leading comment header.
    """
    if not sql:
        return "SELECT 1;"

    lines = sql.strip().splitlines()
    while lines and lines[0].strip().startswith("--"):
        lines = lines[1:]

    while lines and not lines[0].strip():
        lines = lines[1:]

    return "\n".join(lines).strip() if lines else sql.strip()


def _to_single_line(sql: str) -> str:
    """Collapse multi-line SQL to one space-separated line, 10-space indented for YAML block."""
    single = " ".join(line.strip() for line in sql.splitlines() if line.strip())
    return " " * _QUERY_INDENT + single


def _to_indented_multiline(sql: str) -> str:
    """Re-indent already multi-line SQL (one column per line, as
    silver_sql_emitter.py emits it) to _QUERY_INDENT spaces per line, keeping
    every line break -- unlike _to_single_line(), used for Bronze. ADR 0022:
    flattening Silver's SQL onto one line broke it once a `--` comment was
    present (the comment ate everything after it), and is unreadable besides;
    Silver's SQL now never contains comments (macro-skip columns are simply
    omitted from the SELECT), so keeping it multi-line is safe."""
    return "\n".join(" " * _QUERY_INDENT + line for line in sql.splitlines())
