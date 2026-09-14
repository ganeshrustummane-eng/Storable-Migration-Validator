"""
Excel Batch Loader — Report-Pack Validation
============================================
Reads an Excel mapping sheet where each row describes one report validation
check and produces YAML configs compatible with the existing validation engine.

Expected columns (case-insensitive, extras ignored):
  No.                  — row number (optional, for logging)
  Report Pack          — subfolder under Project/config/report/<pack>/
  Yaml-File-name       — output filename (no .yaml extension needed)
  Report Name          — human label (written as summary in YAML)
  Summary              — description used as AI prompt context
  Grain                — composite key, e.g. "facility_key + report_date"
  Legacy Query (*)     — source SQL; * matches Redshift / Postgres / MSSQL / Athena
  Snowflake Query      — target SQL

When either query column is empty or "<<EMPTY>>":
  → AI generates the query from Summary + Grain + source/target context.

Environment substitution:
  Any {env} token in the stored or generated SQL is left as-is in the YAML
  template (so the runner replaces it at execution time) unless --env is passed,
  in which case it is replaced with the literal env string before writing.

Usage (CLI):
  python validate_cli.py excel-batch --file mapping.xlsx
  python validate_cli.py excel-batch --file mapping.xlsx --env dev
  python validate_cli.py excel-batch --file mapping.xlsx --env prod --sheet Sheet2
  python validate_cli.py excel-batch --file mapping.xlsx --dry-run

Programmatic:
  from excel_batch_loader import ExcelBatchLoader
  loader = ExcelBatchLoader(env="dev")
  loader.run("mapping.xlsx")
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

_SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(_SRC_DIR))
sys.path.insert(0, str(_SRC_DIR.parent))

_PROJECT_CONFIG = _SRC_DIR.parent / "Project" / "config" / "report"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ReportSpec:
    row_num: int
    report_pack: str        # e.g. "Management Report"
    yaml_file_name: str     # e.g. "Test_006_Count"
    report_name: str
    summary: str
    grain: str              # e.g. "facility_key + delinquency_aging_bucket + report_date"
    source_type: str        # detected from column header: redshift/postgres/mssql/athena
    legacy_query: str       # raw SQL or "" when missing
    snowflake_query: str    # raw SQL or "" when missing
    # Connection context — populated by the UI before calling _generate_queries
    source_database: str = ""
    source_schema: str = ""
    source_tables: list = field(default_factory=list)   # tables identified from grain/summary
    sf_database: str = ""
    sf_schema: str = ""


# ---------------------------------------------------------------------------
# Column name detection helpers
# ---------------------------------------------------------------------------

_LEGACY_PATTERN = re.compile(
    r"legacy\s*query|legacy\s*sql|source\s*query",
    re.IGNORECASE,
)
_SOURCE_TYPE_HINTS = {
    "redshift":   "redshift",
    "postgres":   "postgresql",
    "postgresql": "postgresql",
    "mssql":      "mssql",
    "sql server": "mssql",
    "athena":     "athena",
    "iceberg":    "redshift",  # iceberg is typically on Redshift/Athena; treat as redshift
}


def _detect_source_type(col_name: str) -> str:
    """Infer source DB type from the legacy query column header."""
    lower = col_name.lower()
    for hint, db in _SOURCE_TYPE_HINTS.items():
        if hint in lower:
            return db
    return "redshift"   # default for this client


def _is_empty(value) -> bool:
    if value is None:
        return True
    s = str(value).strip()
    return s == "" or s.upper() == "<<EMPTY>>" or s.upper() == "N/A"


# ---------------------------------------------------------------------------
# Excel reader
# ---------------------------------------------------------------------------

def _normalize_col(name: str) -> str:
    return name.strip().lower()


def load_excel(path: str, sheet: Optional[str] = None) -> List[ReportSpec]:
    """Parse the Excel file and return one ReportSpec per data row."""
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas is required: pip install pandas openpyxl") from exc

    df = pd.read_excel(path, sheet_name=sheet or 0, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    # Locate columns by fuzzy matching
    def _find(pattern: str) -> Optional[str]:
        pat = re.compile(pattern, re.IGNORECASE)
        for c in df.columns:
            if pat.search(c):
                return c
        return None

    col_no           = _find(r"^no\.?$|^#$|^row")
    col_pack         = _find(r"report\s*pack")
    col_yaml         = _find(r"yaml.?file|file.?name")
    col_report_name  = _find(r"report\s*name")
    col_summary      = _find(r"^summary$")
    col_grain        = _find(r"^grain$")
    col_sf           = _find(r"snowflake\s*query|target\s*query")

    # Find legacy query column(s) — pick the first matching one
    col_legacy = None
    detected_source = "redshift"
    for c in df.columns:
        if _LEGACY_PATTERN.search(c):
            col_legacy = c
            detected_source = _detect_source_type(c)
            break

    if not col_yaml:
        raise ValueError("Could not find a 'Yaml-File-name' column in the sheet.")

    specs: List[ReportSpec] = []
    for idx, row in df.iterrows():
        yaml_name = str(row[col_yaml]).strip() if col_yaml else ""
        if not yaml_name or _is_empty(yaml_name):
            continue  # skip header-continuation / blank rows

        # Strip .yaml extension if already there
        yaml_name = re.sub(r"\.yaml$", "", yaml_name, flags=re.IGNORECASE)

        pack        = str(row[col_pack]).strip()       if col_pack        else "general"
        report_name = str(row[col_report_name]).strip() if col_report_name else yaml_name
        summary     = str(row[col_summary]).strip()    if col_summary     else ""
        grain       = str(row[col_grain]).strip()      if col_grain       else ""
        legacy_q    = str(row[col_legacy]).strip()     if col_legacy      else ""
        sf_q        = str(row[col_sf]).strip()         if col_sf          else ""
        row_num     = int(float(row[col_no])) if col_no and not _is_empty(row.get(col_no, "")) else idx + 2

        if _is_empty(legacy_q):
            legacy_q = ""
        if _is_empty(sf_q):
            sf_q = ""

        specs.append(ReportSpec(
            row_num=row_num,
            report_pack=pack,
            yaml_file_name=yaml_name,
            report_name=report_name,
            summary=summary,
            grain=grain,
            source_type=detected_source,
            legacy_query=legacy_q,
            snowflake_query=sf_q,
        ))

    return specs


# ---------------------------------------------------------------------------
# AI query generation for missing queries
# ---------------------------------------------------------------------------

def _build_schema_context(
    extractor,
    db_type: str,
    database: str,
    schema: str,
    tables: list,
    grain_cols: list,
    is_snowflake: bool = False,
) -> dict:
    """
    Build a schema_context dict for generate_schema_aware_query by querying
    the actual DB for column metadata + PK/FK relationships.

    Each key is a fully-qualified "database.schema.table" string.
    Columns are annotated with is_primary_key=True for real PK columns;
    FK target tables are added automatically so the AI can write correct JOINs.

    Falls back to grain_cols only when extractor calls fail.
    """
    ctx: dict = {}
    fk_ref_tables: set = set()

    for tbl in tables:
        fqn = f"{database}.{schema}.{tbl}" if database else f"{schema}.{tbl}"
        try:
            cols = extractor.extract_columns(schema, tbl, database=database or None)
            pk_info = extractor.detect_primary_key(schema, tbl)
            pk_set = {c.lower() for c in pk_info.columns}
            fks = extractor.detect_foreign_keys(schema, tbl)
            for fk in fks:
                fk_ref_tables.add((fk["ref_schema"], fk["ref_table"]))
            col_dicts = [
                {
                    "column_name": c.column_name,
                    "data_type": c.data_type,
                    "is_nullable": c.is_nullable,
                    "is_primary_key": c.column_name.lower() in pk_set,
                }
                for c in cols
            ]
            ctx[fqn] = col_dicts
        except Exception as exc:
            print(f"  ⚠ schema context fallback for {fqn}: {exc}")
            # fall back to grain columns as a stub
            ctx[fqn] = [
                {"column_name": c, "data_type": "text", "is_nullable": True, "is_primary_key": True}
                for c in grain_cols
            ] or [{"column_name": "*", "data_type": "text", "is_nullable": True, "is_primary_key": False}]

    # Pull in FK-referenced tables so the AI knows the full JOIN graph
    for ref_schema, ref_table in fk_ref_tables:
        ref_fqn = f"{database}.{ref_schema}.{ref_table}" if database else f"{ref_schema}.{ref_table}"
        if ref_fqn in ctx:
            continue
        try:
            cols = extractor.extract_columns(ref_schema, ref_table, database=database or None)
            pk_info = extractor.detect_primary_key(ref_schema, ref_table)
            pk_set = {c.lower() for c in pk_info.columns}
            ctx[ref_fqn] = [
                {
                    "column_name": c.column_name,
                    "data_type": c.data_type,
                    "is_nullable": c.is_nullable,
                    "is_primary_key": c.column_name.lower() in pk_set,
                }
                for c in cols
            ]
        except Exception:
            pass  # best-effort — if we can't fetch the FK target, just omit it

    return ctx


def _generate_queries(
    spec: ReportSpec,
    model: Optional[str] = None,
    source_extractor=None,
    sf_extractor=None,
) -> tuple[str, str]:
    """
    Produce source + target SQL for a ReportSpec when either query is missing.

    When extractors are provided (always the case via the web UI), real column
    metadata and PK/FK relationships are fetched from the DB to build a fully-
    qualified schema context for generate_schema_aware_query.  This lets the AI
    write correct multi-table JOINs with proper db.schema.table.column references.

    Falls back to grain-column stubs when no extractor is available (CLI path).
    Returns (source_sql, target_sql) — both guaranteed non-empty on success.
    """
    from generated_queries.ai_sql_generator import AISQLQueryGenerator, AISQLGenerationError

    gen = AISQLQueryGenerator(model=model)

    grain_cols = [g.strip() for g in re.split(r"[+,]", spec.grain) if g.strip()]
    grain_desc = ", ".join(grain_cols) if grain_cols else "all columns"

    # Determine which source tables to inspect. UI sets spec.source_tables;
    # fall back to [spec.yaml_file_name] when not set.
    src_tables = spec.source_tables or [spec.yaml_file_name]
    src_db     = spec.source_database
    src_schema = spec.source_schema or "dbo"
    sf_db      = spec.sf_database
    sf_schema  = spec.sf_schema or "consume"

    # Build schema context
    if source_extractor and src_tables:
        schema_ctx_src = _build_schema_context(
            source_extractor, spec.source_type, src_db, src_schema, src_tables, grain_cols,
        )
    else:
        # stub — grain cols only, no real DB connection
        fqn_src = f"{src_db}.{src_schema}.{spec.yaml_file_name}" if src_db else f"{src_schema}.{spec.yaml_file_name}"
        schema_ctx_src = {
            fqn_src: [
                {"column_name": c, "data_type": "text", "is_nullable": True, "is_primary_key": True}
                for c in grain_cols
            ] or [{"column_name": "*", "data_type": "text", "is_nullable": True, "is_primary_key": False}]
        }

    if sf_extractor and src_tables:
        sf_tables = [t.upper() for t in src_tables]
        schema_ctx_tgt = _build_schema_context(
            sf_extractor, "snowflake", sf_db, sf_schema, sf_tables, grain_cols, is_snowflake=True,
        )
    else:
        fqn_tgt = f"{sf_db}.{sf_schema}.{spec.yaml_file_name}" if sf_db else f"{sf_schema}.{spec.yaml_file_name}"
        schema_ctx_tgt = {
            fqn_tgt: [
                {"column_name": c.upper(), "data_type": "STRING", "is_nullable": True, "is_primary_key": True}
                for c in grain_cols
            ] or [{"column_name": "*", "data_type": "STRING", "is_nullable": True, "is_primary_key": False}]
        }

    # Check if any table has a real PK; if not, instruct AI to use row hash
    has_real_pk = any(
        any(c.get("is_primary_key") for c in cols)
        for cols in schema_ctx_src.values()
    )
    pk_note = (
        f"Use the PK columns (marked [PK] in the schema above) as the join key."
        if has_real_pk else
        "No PRIMARY KEY constraint was found. Add "
        "MD5(CONCAT_WS('|', <all grain cols cast to VARCHAR>)) AS row_hash "
        "and use row_hash as the comparison key between source and target."
    )

    def _instruction(db_type: str) -> str:
        return (
            f"Write a {db_type.upper()} SQL query to validate: {spec.summary}\n"
            f"Grain (key/grouping columns): {grain_desc}\n"
            f"Always reference columns as fully-qualified db.schema.table.column. "
            f"Use COUNT(*) or SUM aggregates appropriate to the summary description. "
            f"{pk_note} "
            f"JOINs across multiple tables are allowed and should follow the FK relationships "
            f"shown in the schema context. "
            f"Use {{env}} as a literal placeholder for the environment prefix "
            f"(e.g. dev, prod) so the caller substitutes it at runtime."
        )

    src_sql = spec.legacy_query
    tgt_sql = spec.snowflake_query

    if not src_sql:
        try:
            result = gen.generate_schema_aware_query(
                user_instruction=_instruction(spec.source_type),
                schema_context=schema_ctx_src,
                db_type=spec.source_type,
                default_schema=src_schema,
                normalize=True,
            )
            src_sql = result.query
        except AISQLGenerationError as exc:
            raise RuntimeError(
                f"Row {spec.row_num} ({spec.yaml_file_name}): AI failed source query: {exc}"
            ) from exc

    if not tgt_sql:
        try:
            result = gen.generate_schema_aware_query(
                user_instruction=_instruction("snowflake"),
                schema_context=schema_ctx_tgt,
                db_type="snowflake",
                default_schema=sf_schema,
                normalize=True,
            )
            tgt_sql = result.query
        except AISQLGenerationError as exc:
            raise RuntimeError(
                f"Row {spec.row_num} ({spec.yaml_file_name}): AI failed target query: {exc}"
            ) from exc

    return src_sql, tgt_sql


# ---------------------------------------------------------------------------
# YAML writer
# ---------------------------------------------------------------------------

def _pack_slug(pack_name: str) -> str:
    """'Management Report' → 'management_report'"""
    return re.sub(r"[^a-z0-9]+", "_", pack_name.lower()).strip("_")


def _apply_env(sql: str, env: Optional[str]) -> str:
    """Replace {env} tokens if env is given; leave them if env is None."""
    if env and sql:
        return sql.replace("{env}", env)
    return sql


def write_yaml(spec: ReportSpec, src_sql: str, tgt_sql: str, env: Optional[str], output_dir: Path, dry_run: bool = False) -> Path:
    """Write one DataValidation YAML block for this spec."""
    pack_dir = output_dir / _pack_slug(spec.report_pack) / "data_validation"
    out_path  = pack_dir / f"{spec.yaml_file_name}.yaml"

    src_sql_final = _apply_env(src_sql.strip(), env)
    tgt_sql_final = _apply_env(tgt_sql.strip(), env)

    doc = {
        "tables": {
            spec.yaml_file_name: {
                "validations": {
                    "data_validation": {
                        "source_table_name": spec.yaml_file_name,
                        "source":            spec.source_type,
                        "source_database":   f"{env or '{env}'}_source" if env else "{env}_source",
                        "source_schema":     "consume",
                        "pksourcecolumn":    "row_hash",
                        "summary":           spec.summary,
                        "report_tile":       spec.report_name,
                        "sourcequery":       src_sql_final,
                        "target_table_name": spec.yaml_file_name,
                        "target":            "snowflake",
                        "target_database":   f"{env or '{env}'}_gold" if env else "{env}_gold",
                        "target_schema":     "consume",
                        "pktargetcolumn":    "row_hash",
                        "targetquery":       tgt_sql_final,
                    }
                }
            }
        }
    }

    if dry_run:
        print(f"  [DRY RUN] Would write: {out_path}")
        return out_path

    pack_dir.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(doc, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    return out_path


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class ExcelBatchLoader:
    def __init__(
        self,
        env: Optional[str] = None,
        model: Optional[str] = None,
        output_dir: Optional[Path] = None,
        dry_run: bool = False,
    ):
        self.env        = env
        self.model      = model
        self.output_dir = output_dir or _PROJECT_CONFIG
        self.dry_run    = dry_run

    def run(self, excel_path: str, sheet: Optional[str] = None) -> List[Path]:
        specs = load_excel(excel_path, sheet)
        print(f"  Loaded {len(specs)} spec(s) from {excel_path}")

        written: List[Path] = []
        for spec in specs:
            print(f"\n  Row {spec.row_num}: {spec.yaml_file_name}  ({spec.report_pack})")

            try:
                src_sql, tgt_sql = _generate_queries(spec, self.model) \
                    if (not spec.legacy_query or not spec.snowflake_query) \
                    else (spec.legacy_query, spec.snowflake_query)

                out = write_yaml(spec, src_sql, tgt_sql, self.env, self.output_dir, self.dry_run)
                print(f"    ✓ {out}")
                written.append(out)
            except Exception as exc:
                print(f"    ✗ FAILED: {exc}", file=sys.stderr)

        return written


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import textwrap
    # Verify column detection and env substitution without hitting any DB
    test_sql = "SELECT COUNT(*) FROM {env}_gold.consume.fact_test"
    assert _apply_env(test_sql, "dev") == "SELECT COUNT(*) FROM dev_gold.consume.fact_test"
    assert _apply_env(test_sql, None) == test_sql
    assert _is_empty("<<EMPTY>>")
    assert _is_empty("")
    assert not _is_empty("SELECT 1")
    assert _detect_source_type("Legacy Query (Redshift)") == "redshift"
    assert _detect_source_type("Legacy Query (PostgreSQL)") == "postgresql"
    assert _pack_slug("Management Report") == "management_report"
    print("Self-check passed.")
