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
    filter_condition: str = ""
    transformation_note: str = ""


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


def load_excel(
    path: str,
    sheet: Optional[str] = None,
    unrecognized_out: Optional[List[str]] = None,
    ai_generator=None,
) -> List[ReportSpec]:
    """Parse the Excel file and return one ReportSpec per data row.

    Args:
        path: path to the .xlsx file.
        sheet: sheet name (defaults to the first sheet).
        unrecognized_out: optional list the caller passes in to receive any
            header names that neither the regex fast-path nor the AI
            classifier fallback could confidently place (role "unknown").
            Additive — default None preserves today's behavior exactly (no
            AI call, no unrecognized-column tracking).
        ai_generator: optional AISQLQueryGenerator instance to reuse for the
            header-classification AI fallback (avoids re-instantiating a
            client per sheet when the caller already has one). Created
            lazily only when needed (i.e. only when there are unmatched
            headers) if not supplied.
    """
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
    col_filter       = _find(r"^filter$|^condition$")
    col_transform    = _find(r"transformation")

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

    # --- AI fallback for headers the regex fast-path couldn't place --------
    _matched_cols = {
        c for c in (
            col_no, col_pack, col_yaml, col_report_name, col_summary,
            col_grain, col_sf, col_filter, col_transform, col_legacy,
        ) if c
    }
    unmatched_cols = [c for c in df.columns if c not in _matched_cols]
    if unmatched_cols:
        try:
            if ai_generator is None:
                from generated_queries.ai_sql_generator import AISQLQueryGenerator
                ai_generator = AISQLQueryGenerator()
            sample_values = {
                c: [v for v in df[c].dropna().astype(str).head(3).tolist()]
                for c in unmatched_cols
            }
            roles = ai_generator.classify_headers(unmatched_cols, sample_values)
        except Exception as exc:
            print(f"  ⚠ header classification AI fallback failed: {exc}", file=sys.stderr)
            roles = {c: "unknown" for c in unmatched_cols}

        _role_to_var = {
            "report_pack": "col_pack", "yaml_file_name": "col_yaml",
            "report_name": "col_report_name", "summary": "col_summary",
            "grain": "col_grain", "filter": "col_filter",
            "transformation": "col_transform", "target_query": "col_sf",
        }
        for header, role in roles.items():
            if role == "ignore":
                continue
            if role == "legacy_query" and not col_legacy:
                col_legacy = header
                detected_source = _detect_source_type(header)
                continue
            if role == "unknown":
                if unrecognized_out is not None:
                    unrecognized_out.append(header)
                continue
            var_name = _role_to_var.get(role)
            if var_name and locals().get(var_name) is None:
                if var_name == "col_pack":
                    col_pack = header
                elif var_name == "col_yaml":
                    col_yaml = header
                elif var_name == "col_report_name":
                    col_report_name = header
                elif var_name == "col_summary":
                    col_summary = header
                elif var_name == "col_grain":
                    col_grain = header
                elif var_name == "col_filter":
                    col_filter = header
                elif var_name == "col_transform":
                    col_transform = header
                elif var_name == "col_sf":
                    col_sf = header

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
        filter_cond = str(row[col_filter]).strip()     if col_filter      else ""
        transform_note = str(row[col_transform]).strip() if col_transform else ""
        row_num     = int(float(row[col_no])) if col_no and not _is_empty(row.get(col_no, "")) else idx + 2

        if _is_empty(legacy_q):
            legacy_q = ""
        if _is_empty(sf_q):
            sf_q = ""
        if _is_empty(filter_cond):
            filter_cond = ""
        if _is_empty(transform_note):
            transform_note = ""

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
            filter_condition=filter_cond,
            transformation_note=transform_note,
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
# Row-level plan derivation (AI-preview)
# ---------------------------------------------------------------------------

_SQL_LOOKING_RE = re.compile(
    r"\b(AND|OR)\b|[=<>]|\bIN\s*\(|\bLIKE\b|\bIS\s+NULL\b|\bIS\s+NOT\s+NULL\b",
    re.IGNORECASE,
)


def _looks_like_sql(text: str) -> bool:
    """Cheap heuristic: does this already read like a SQL boolean predicate?

    ponytail: naive regex heuristic, not a parser — false negatives just cost
    one extra (cheap, single) AI call; false positives are rare because a
    prose sentence rarely contains '=' or ' AND ' by accident. Upgrade to a
    real predicate parser only if this misfires in practice.
    """
    return bool(text) and bool(_SQL_LOOKING_RE.search(text))


def derive_row_plan(
    spec: ReportSpec,
    source_extractor=None,
    ai_generator=None,
    model: Optional[str] = None,
) -> dict:
    """
    Derive a per-row validation plan preview for one ReportSpec: which
    table(s) it touches, its grain (candidate composite key), and its filter
    condition as both SQL and plain English — without generating the actual
    comparison SQL (that happens later, at YAML-generation time, via either
    run_with_plan() for single-table rows or _generate_queries() for joins).

    Single-table vs multi-table/join is decided the same way _generate_queries
    already does: spec.source_tables (set by the UI) determines the table
    list; more than one table means a join is needed. Join rows are NOT
    routed through CanonicalValidationPlan/run_with_plan() — that path stays
    scoped to what generate_schema_aware_query() already handles unchanged.

    Args:
        spec: the parsed ReportSpec (row_num, grain, filter_condition, ...).
        source_extractor: optional BaseExtractor, used only to confirm the
            grain columns are real columns on the source table(s) (best
            effort — falls back to the raw grain string on any failure).
        ai_generator: optional AISQLQueryGenerator to reuse (avoids creating
            a new AI client per row); created lazily if not supplied.
        model: AI model override, only used if ai_generator is created here.

    Returns:
        {
            "tables": List[str],
            "grain_columns": List[str],
            "filter_english": str,
            "filter_sql": str,
            "join_needed": bool,
            "warnings": List[str],
        }
    """
    warnings: List[str] = []
    grain_cols = [g.strip() for g in re.split(r"[+,]", spec.grain) if g.strip()]
    tables = spec.source_tables or ([spec.yaml_file_name] if spec.yaml_file_name else [])
    join_needed = len(tables) > 1

    if source_extractor and tables:
        try:
            _build_schema_context(
                source_extractor, spec.source_type, spec.source_database,
                spec.source_schema or "dbo", tables, grain_cols,
            )
        except Exception as exc:
            warnings.append(f"Could not verify grain columns against schema: {exc}")

    filter_condition = (spec.filter_condition or "").strip()
    filter_sql = ""
    filter_english = ""

    if not filter_condition:
        pass  # no filter on this row — leave both fields empty
    elif _looks_like_sql(filter_condition):
        filter_sql = filter_condition
        filter_english = filter_condition
    else:
        if ai_generator is None:
            from generated_queries.ai_sql_generator import AISQLQueryGenerator
            ai_generator = AISQLQueryGenerator(model=model)
        derived = ai_generator.explain_and_derive_filter(filter_condition)
        filter_sql = derived["filter_sql"]
        filter_english = derived["filter_english"]
        warnings.extend(derived["warnings"])

    if join_needed:
        # ponytail: no eager SQL generation here — the real join query is
        # built at generate-time by _generate_queries()/generate_schema_aware_query().
        # Re-deriving it here too would double the AI calls for a preview grid.
        warnings.append(
            "Multi-table row — full JOIN SQL is generated at YAML-write time "
            "via the existing generate_schema_aware_query() path, unchanged."
        )

    return {
        "tables": tables,
        "grain_columns": grain_cols,
        "filter_english": filter_english,
        "filter_sql": filter_sql,
        "join_needed": join_needed,
        "warnings": warnings,
    }


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

    # New ReportSpec fields default to "" (backward compatibility)
    _spec = ReportSpec(
        row_num=1, report_pack="p", yaml_file_name="y", report_name="r",
        summary="s", grain="g", source_type="redshift", legacy_query="",
        snowflake_query="",
    )
    assert _spec.filter_condition == ""
    assert _spec.transformation_note == ""

    # Regex fast-path still works unchanged: all headers regex-matchable,
    # so load_excel must not trigger any AI classification call.
    import tempfile
    import pandas as pd
    _df = pd.DataFrame([{
        "No.": 1, "Report Pack": "Mgmt", "Yaml-File-name": "Test_001",
        "Report Name": "Count check", "Summary": "row count check",
        "Grain": "facility_key", "Filter": "status = 'ACTIVE'",
        "Transformation": "", "Legacy Query (Redshift)": "SELECT 1",
        "Snowflake Query": "SELECT 1",
    }])
    with tempfile.TemporaryDirectory() as _tmpdir:
        _xlsx_path = str(Path(_tmpdir) / "mapping.xlsx")
        _df.to_excel(_xlsx_path, index=False)
        _unrecognized: list = []
        _specs = load_excel(_xlsx_path, unrecognized_out=_unrecognized)
        assert len(_specs) == 1, _specs
        _s = _specs[0]
        assert _s.yaml_file_name == "Test_001"
        assert _s.grain == "facility_key"
        assert _s.filter_condition == "status = 'ACTIVE'"
        assert _s.legacy_query == "SELECT 1"
        assert _unrecognized == [], f"unexpected unrecognized columns (AI fallback should not fire): {_unrecognized}"

    # derive_row_plan: SQL-looking filter takes the no-AI-call fast path.
    assert _looks_like_sql("status = 'ACTIVE'")
    assert not _looks_like_sql("only active facilities")
    _plan = derive_row_plan(_s)
    assert _plan["filter_sql"] == "status = 'ACTIVE'"
    assert _plan["join_needed"] is False
    assert _plan["grain_columns"] == ["facility_key"]

    print("Self-check passed.")
