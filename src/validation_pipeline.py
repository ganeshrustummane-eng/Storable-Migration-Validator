"""
Validation Pipeline — End-to-End Orchestrator
===============================================
Wires all modules together into a single callable pipeline.

Pipeline (run_with_plan):
  ┌─────────────────────────────────────────────────────────────────────┐
  │  1. Extract schemas       (PostgreSQL + Snowflake)                  │
  │  2. Exact matching        (case-insensitive + normalized name)      │
  │  3. Fuzzy matching        (RapidFuzz — top N candidates per col)   │
  │  4. Confidence scoring    (multi-factor: name+type+position+learned)│
  │  5. AI Rule Planner       (ONLY for ambiguous columns — token eff.) │
  │  6. Plan validation       (structural integrity check)              │
  │  7. SQL + YAML generation (deterministic from plan)                 │
  └─────────────────────────────────────────────────────────────────────┘

(The older run() method — a 100%-AI, no-fallback mapping path used only by
the unused validate_cli.py CLI — has been removed. run_with_plan() is the
only pipeline now; it is what the Streamlit webapp calls.)

PK-Free Design
--------------
Primary key handling is deferred to a future milestone.
All queries operate on full table scans without ORDER BY pk,
duplicate checks, or missing-row checks.

AI Model Selection
------------------
Pass model= to ValidationPipeline() to select which AI model to use.
Backend is chosen automatically: EPAM DIAL if DIAL_API_KEY is set (current
setup — proxies to GPT/Claude/etc. for building/testing), else direct
Anthropic Claude if CLAUDE_API_KEY is set (the intended path once a Claude
API key is issued — no DIAL needed). Examples:
  - "gpt-4o"                          (DIAL, default)
  - "gpt-4o-mini"                     (DIAL, faster/cheaper)
  - "claude-3-5-sonnet-20241022"      (Claude direct)
  - Any model on your DIAL endpoint

Usage (Python API)
------------------
    from validation_pipeline import ValidationPipeline

    pipeline = ValidationPipeline(model="gpt-4o")
    result = pipeline.run(
        pg_schema="public",
        pg_table="events",
        sf_database="dev_edge_bronze",
        sf_schema="storedge_fms_public",
        sf_table="EVENTS",
    )
    print(result.summary())

Usage (CLI)
-----------
    python src/validation_pipeline.py \\
        --pg-table events --sf-table EVENTS

    python src/validation_pipeline.py \\
        --pg-table events --sf-table EVENTS --model gpt-4o-mini

Environment Variables (.env)
-----------------------------
    SOURCE_HOST, SOURCE_PORT, SOURCE_DATABASE
    SOURCE_USERNAME, SOURCE_PASSWORD, SOURCE_SCHEMA
    SNOWFLAKE_ACCOUNT, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA
    SNOWFLAKE_USERNAME, SNOWFLAKE_PASSWORD
    DIAL_API_KEY      ← optional; enables AI rule mapping
    DIAL_MODEL        ← optional; default model name
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Load .env before any env-reading imports
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from sql_extractor import PostgresExtractor, SnowflakeExtractor, ExtractorFactory
from sql_extractor.extractors import FIVETRAN_ACTIVE_COLUMN
from ai_transformation.ai_rule_mapper import AVAILABLE_MODELS
from generated_queries import QueryOutputManager, GenerationResult
from generated_queries.yaml_config_writer import YAMLConfigWriter

# New matching pipeline imports
from matching.candidate_matcher import CandidateMatcher
from ai.rule_planner import RulePlanner
from core.validation_plan import (
    CanonicalValidationPlan,
    ColumnMappingEntry,
    PlanStatus,
)
from validation.plan_validator import PlanValidator, PlanValidationError
from learning.retrieval import LearnedRuleRetriever


class ValidationPipeline:
    """
    Full end-to-end validation pipeline.

    Steps
    -----
      1. Extract live column metadata from PostgreSQL and Snowflake.
      2. Map source → target columns and assign transformation rules
         (AI if DIAL_API_KEY is set, static otherwise).
      3. Generate SQL validation queries + YAML config files.

    Model Selection
    ---------------
    Pass model= to the constructor to control which AI model is used.
    If DIAL_API_KEY is not set, static rule matching is used regardless.
    """

    def __init__(self, model: Optional[str] = None, source_extractor=None):
        """
        Args:
            model: AI model name to use (e.g. 'gpt-4o', 'gpt-4o-mini').
                   Defaults to DIAL_MODEL env var, then 'gpt-4o'.
                   Has no effect when DIAL_API_KEY is not set.
            source_extractor: Optional BaseExtractor for the source DB.
                   When None, the constructor reads SOURCE_TYPE from the
                   environment and creates the correct extractor automatically
                   (PostgreSQL, MSSQL, Athena, etc.).
        """
        if source_extractor is not None:
            self._src_extractor = source_extractor
        else:
            src_type = os.getenv("SOURCE_TYPE", "postgresql")
            self._src_extractor = ExtractorFactory.create(src_type)
        self._sf_extractor   = SnowflakeExtractor()
        self._model          = model
        self._output_mgr     = QueryOutputManager()
        self._yaml_writer    = YAMLConfigWriter()

    def run_with_plan(
        self,
        pg_schema: str,
        pg_table: str,
        sf_schema: str,
        sf_table: str,
        sf_database: Optional[str] = None,
        pg_database: Optional[str] = None,
        explicit_mappings: Optional[dict] = None,
        exclude_columns: Optional[List[str]] = None,
        source_db_type: Optional[str] = None,
        output_dir: Optional[Path] = None,
        layer: str = "bronze",
        source_filter: str = "",
        target_filter: str = "",
        candidate_keys: Optional[List[List[str]]] = None,
    ) -> "tuple[GenerationResult, CanonicalValidationPlan]":
        """
        Run the full new pipeline using the CanonicalValidationPlan architecture.

        7-step pipeline:
          1. Extract schemas from PostgreSQL + Snowflake
          2. Exact matching (case-insensitive + normalized)
          3. Fuzzy matching (RapidFuzz top-N candidates)
          4. Confidence scoring (multi-factor)
          5. AI Rule Planner (ONLY for ambiguous columns)
          6. Plan validation
          7. SQL + YAML generation from the plan

        Args:
            pg_schema        : PostgreSQL schema (e.g. 'public')
            pg_table         : PostgreSQL table name
            sf_schema        : Snowflake schema name
            sf_table         : Snowflake table name
            sf_database      : Snowflake database name
            pg_database      : PostgreSQL database name
            explicit_mappings: Optional {src_col: tgt_col} override dict
            exclude_columns  : Column names to skip (case-insensitive)
            source_db_type   : Source database type (e.g. 'postgresql', 'mssql')
                               (default: SOURCE_TYPE env var)
            candidate_keys   : Optional list of candidate composite-key column
                               lists (e.g. [["facility_key", "report_date"]]),
                               passed straight through to
                               CanonicalValidationPlan.candidate_keys. Additive —
                               default None produces the exact same plan as
                               before this parameter existed.

        Returns:
            (GenerationResult, CanonicalValidationPlan) tuple
        """
        _sf_db = sf_database or os.getenv("SNOWFLAKE_DATABASE", "")
        _pg_db = pg_database or os.getenv("SOURCE_DATABASE", "")
        _src_db_type = source_db_type or os.getenv("SOURCE_TYPE", "postgresql")

        # Cheap probe (no API call) — resolves backend/model the same way
        # RulePlanner will when ambiguous columns actually need AI, so the
        # header and the plan metadata below always agree with what
        # _match_columns() does.
        _ai_probe = RulePlanner(model=self._model)

        _print_header_v2(
            pg_schema, pg_table, _sf_db, sf_schema, sf_table,
            _ai_probe.model, _ai_probe._ai_active, _pg_db,
        )

        (
            src_columns, tgt_columns, final_decisions, has_fivetran_active,
            src_pk, tgt_pk, ai_calls_made, ai_decision_map,
        ) = self._match_columns(
            pg_schema=pg_schema, pg_table=pg_table, sf_schema=sf_schema, sf_table=sf_table,
            sf_database=_sf_db, pg_database=_pg_db,
            explicit_mappings=explicit_mappings, exclude_columns=exclude_columns,
            step_label="2-4/7", ai_step_label="5/7",
        )

        # ── Build CanonicalValidationPlan ─────────────────────────────────────
        from rules import get_rule_for_type as _get_rule
        from matching.normalizer import normalize_column_name as _norm

        plan_mappings: List[ColumnMappingEntry] = []
        unmatched_src: List[str] = []
        ambiguities:   List[str] = []

        for dec in final_decisions:
            src = dec.source_col

            if dec.skip_validation:
                plan_mappings.append(ColumnMappingEntry(
                    source_column=src.column_name,
                    source_type=src.data_type,
                    source_normalized=_norm(src.column_name),
                    target_column=src.column_name,
                    target_type="",
                    target_normalized="",
                    match_method=dec.method or "skip",
                    confidence=dec.final_score,
                    transformation_rule="text",
                    reason=dec.skip_reason or "Skipped",
                    skip_validation=True,
                    skip_reason=dec.skip_reason,
                ))
                if not src.column_name.upper().startswith("_FIVETRAN_"):
                    unmatched_src.append(src.column_name)
                continue

            if dec.target_col is None:
                unmatched_src.append(src.column_name)
                continue

            tgt = dec.target_col

            # Get AI decision for rule/reason if available
            ai_dec     = ai_decision_map.get(src.column_name)
            rule_id    = ai_dec.transformation_rule if ai_dec else "text"
            reason_txt = ai_dec.reason if ai_dec else _default_reason(dec)
            ai_resolved = dec.method == "fuzzy_ai"

            # Confidence breakdown as dict
            breakdown_dict = None
            if dec.confidence:
                bd = dec.confidence
                breakdown_dict = {
                    "name_similarity":  round(bd.name_similarity, 3),
                    "type_compatibility": round(bd.type_compatibility, 3),
                    "position_proximity": round(bd.position_proximity, 3),
                    "learned_example":  round(bd.learned_example, 3),
                    "final_score":      round(bd.final_score, 3),
                }

            plan_mappings.append(ColumnMappingEntry(
                source_column=src.column_name,
                source_type=src.data_type,
                source_normalized=_norm(src.column_name),
                target_column=tgt.column_name,
                target_type=tgt.data_type,
                target_normalized=_norm(tgt.column_name),
                match_method=dec.method or "static",
                fuzzy_score=dec.fuzzy_score,
                confidence=dec.final_score,
                confidence_breakdown=breakdown_dict,
                transformation_rule=rule_id,
                reason=reason_txt,
                ai_resolved=ai_resolved,
                skip_validation=False,
            ))

        # Determine plan status
        if ambiguities:
            plan_status = PlanStatus.AMBIGUOUS.value
        elif unmatched_src:
            plan_status = PlanStatus.PARTIAL.value
        else:
            plan_status = PlanStatus.COMPLETE.value

        # ── PK mismatch detection ─────────────────────────────────────────────
        from matching.normalizer import normalize_column_name as _norm
        src_pk_cols = src_pk.columns if src_pk.has_pk else []
        tgt_pk_cols = tgt_pk.columns if tgt_pk.has_pk else []
        pk_mismatch = False
        pk_mismatch_reason = ""

        if src_pk_cols and tgt_pk_cols:
            src_normalized = [_norm(c) for c in src_pk_cols]
            tgt_normalized = [_norm(c) for c in tgt_pk_cols]
            if src_normalized != tgt_normalized:
                pk_mismatch = True
                pk_mismatch_reason = (
                    f"Source PK {src_pk_cols} does not match target PK {tgt_pk_cols} "
                    f"(normalized: {src_normalized} vs {tgt_normalized}). "
                    f"Using fuzzy/AI matching — verify manually."
                )
                plan_mappings_warnings = getattr(plan_mappings, 'warnings', [])
                print(f"  ⚠ PK mismatch: {pk_mismatch_reason}")

        # Mark PK columns in mappings
        src_pk_upper = {c.upper() for c in src_pk_cols}
        for i, m in enumerate(plan_mappings):
            if m.source_column.upper() in src_pk_upper:
                # rebuild with is_primary_key=True (dataclass is not frozen)
                from dataclasses import replace as _dc_replace
                plan_mappings[i] = _dc_replace(m, is_primary_key=True)

        plan_warnings = []
        if not src_pk_cols:
            plan_warnings.append(
                f"No PK detected on source {pg_schema}.{pg_table} — "
                "duplicate/missing row checks skipped."
            )
        if pk_mismatch:
            plan_warnings.append(f"PK name mismatch (WARNING): {pk_mismatch_reason}")

        plan = CanonicalValidationPlan(
            source_database=_pg_db,
            source_db_type=_src_db_type,
            source_schema=pg_schema,
            source_table=pg_table,
            target_database=_sf_db,
            target_schema=sf_schema,
            target_table=sf_table,
            mappings=plan_mappings,
            has_fivetran_active=has_fivetran_active,
            source_primary_keys=src_pk_cols,
            target_primary_keys=tgt_pk_cols,
            pk_mismatch=pk_mismatch,
            pk_mismatch_reason=pk_mismatch_reason,
            status=plan_status,
            warnings=plan_warnings,
            unmatched_source_columns=unmatched_src,
            ai_calls_made=ai_calls_made,
            model_used=_ai_probe.model if ai_calls_made else "N/A",
            generated_by="ai" if ai_calls_made else "fuzzy",
            source_filter=source_filter,
            target_filter=target_filter or source_filter,  # mirror source when target not set
            candidate_keys=candidate_keys or [],
        )

        # ── Step 6: Validate plan ─────────────────────────────────────────────
        print("\n[6/7] Validating canonical plan...")
        validator = PlanValidator()
        val_result = validator.validate(plan)
        if val_result.issues:
            for issue in val_result.issues:
                print(f"  ✗ {issue}")
        if val_result.warnings:
            for w in val_result.warnings:
                print(f"  ⚠ {w}")

        if not val_result:
            print("  Plan validation FAILED — SQL generation blocked.")
            raise PlanValidationError(val_result.issues)

        print(f"  Plan status: {plan.status.upper()}")
        for line in plan.summary_lines()[:6]:
            print(f"  {line}")

        # ── Step 7: Generate SQL + YAML ───────────────────────────────────────
        print("\n[7/7] Generating SQL validation queries and YAML config file...")
        result = self._output_mgr.generate_from_plan(plan, output_dir=output_dir, layer=layer)

        return result, plan

    def preview_mapping(
        self,
        pg_schema: str,
        pg_table: str,
        sf_schema: str,
        sf_table: str,
        sf_database: Optional[str] = None,
        pg_database: Optional[str] = None,
        explicit_mappings: Optional[dict] = None,
        exclude_columns: Optional[List[str]] = None,
    ) -> List[dict]:
        """
        Run schema extraction + matching (exact/fuzzy/AI) WITHOUT generating
        any SQL/YAML output, so the resulting source→target column mapping
        can be reviewed and corrected by a human before anything is written.

        This exists because the AI mapper can flag a column as "unmapped" or
        low-confidence even when a rename is actually correct (e.g. source
        'customer' -> target 'user'), and conversely can auto-accept a wrong
        guess. The UI shows every column with how it was matched so a human
        can override before generation runs.

        Args:
            (same as run_with_plan)

        Returns:
            List of dicts, one per source column, each with:
              source_column, source_type, target_column, target_type,
              match_method, confidence, status, skip_validation, skip_reason
        """
        _sf_db = sf_database or os.getenv("SNOWFLAKE_DATABASE", "")
        _pg_db = pg_database or os.getenv("SOURCE_DATABASE", "")

        src_columns, tgt_columns, final_decisions, *_ = self._match_columns(
            pg_schema=pg_schema, pg_table=pg_table, sf_schema=sf_schema, sf_table=sf_table,
            sf_database=_sf_db, pg_database=_pg_db,
            explicit_mappings=explicit_mappings, exclude_columns=exclude_columns,
            step_label="preview", ai_step_label="preview",
        )

        rows: List[dict] = []
        for dec in final_decisions:
            rows.append({
                "source_column": dec.source_col.column_name,
                "source_type": dec.source_col.data_type,
                "target_column": dec.target_col.column_name if dec.target_col else "",
                "target_type": dec.target_col.data_type if dec.target_col else "",
                "match_method": dec.method or ("skip" if dec.skip_validation else "unmatched"),
                "confidence": round(dec.final_score, 3),
                "status": dec.status,
                "skip_validation": dec.skip_validation,
                "skip_reason": dec.skip_reason,
            })
        return rows

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _match_columns(
        self,
        pg_schema: str,
        pg_table: str,
        sf_schema: str,
        sf_table: str,
        sf_database: str,
        pg_database: str,
        explicit_mappings: Optional[dict],
        exclude_columns: Optional[List[str]],
        step_label: str = "2-4",
        ai_step_label: str = "5",
    ):
        """
        Shared matching core used by both run_with_plan() and preview_mapping():
        extract schemas, run exact/fuzzy/confidence matching, then send only
        the ambiguous columns to AI. Kept in one place so preview and actual
        generation can never disagree about how a column was matched.

        Returns:
            (src_columns, tgt_columns, final_decisions, has_fivetran_active,
             src_pk, tgt_pk, ai_calls_made, ai_decision_map)
        """
        src_columns = self._extract_source(pg_schema, pg_table, pg_database)
        tgt_columns = self._extract_target(sf_schema, sf_table, sf_database)

        if exclude_columns:
            excl_upper = {c.upper() for c in exclude_columns}
            before = len(src_columns)
            src_columns = [c for c in src_columns if c.column_name.upper() not in excl_upper]
            dropped = before - len(src_columns)
            if dropped:
                print(f"  ℹ  Column exclusion: removed {dropped} column(s) — {', '.join(exclude_columns)}")

        has_fivetran_active = SnowflakeExtractor.has_fivetran_active(tgt_columns)
        if has_fivetran_active:
            print(f"  ℹ  Fivetran _FIVETRAN_ACTIVE detected — Snowflake queries will filter active records.")
        src_type_lbl = os.getenv("SOURCE_TYPE", "source").upper()
        print(f"  {src_type_lbl}: {len(src_columns)} columns  |  Snowflake: {len(tgt_columns)} columns")

        src_pk = self._src_extractor.detect_primary_key(pg_schema, pg_table)
        tgt_pk = self._sf_extractor.detect_primary_key(sf_schema, sf_table, database=sf_database or None)

        if not src_pk.has_pk:
            print(f"  ⚠ No PK found on source {pg_schema}.{pg_table} — duplicate/missing row checks will be skipped.")

        print(f"\n[{step_label}] Running exact + fuzzy + confidence-scoring matching pipeline...")
        retriever = LearnedRuleRetriever()
        learned   = retriever.all_examples()
        learned_dicts = [ex.to_dict() for ex in learned]

        matcher   = CandidateMatcher()
        decisions = matcher.match(
            source_columns=src_columns,
            target_columns=tgt_columns,
            explicit_mappings=explicit_mappings,
            learned_examples=learned_dicts,
        )

        resolved  = [d for d in decisions if d.is_resolved and not d.skip_validation]
        ai_needed = [d for d in decisions if d.needs_ai]
        skipped   = [d for d in decisions if d.skip_validation]

        print(f"  Exact/fuzzy resolved : {len(resolved)}")
        print(f"  Need AI review       : {len(ai_needed)}")
        print(f"  Skipped              : {len(skipped)}")

        ai_calls_made    = 0
        ai_decision_map  = {}

        planner = RulePlanner(model=self._model) if ai_needed else None

        if ai_needed and planner._ai_active:
            print(f"\n[{ai_step_label}] Sending {len(ai_needed)} ambiguous column(s) to AI...")
            planner_result = planner.resolve(
                ai_needed_decisions=ai_needed,
                table_name=pg_table,
                learned_examples=learned_dicts,
                source_label=src_type_lbl,
            )
            ai_resolved_map = {d.source_col.column_name: d for d in planner_result.decisions}

            final_decisions = []
            for orig in matcher.match(src_columns, tgt_columns, explicit_mappings, learned_dicts):
                name = orig.source_col.column_name
                final_decisions.append(ai_resolved_map.get(name, orig))

            ai_calls_made   = planner_result.ai_calls_made
            ai_decision_map = planner_result.ai_decisions
            print(f"  AI calls made: {ai_calls_made}")
        elif ai_needed:
            print(f"\n[{ai_step_label}] No DIAL_API_KEY / CLAUDE_API_KEY — accepting best fuzzy for {len(ai_needed)} ambiguous column(s).")
            final_decisions = decisions
        else:
            print(f"\n[{ai_step_label}] No ambiguous columns — AI not needed.")
            final_decisions = decisions

        return (
            src_columns, tgt_columns, final_decisions, has_fivetran_active,
            src_pk, tgt_pk, ai_calls_made, ai_decision_map,
        )

    def _extract_source(self, schema: str, table: str, database: str = ""):
        """Extract source column metadata. Raises on failure."""
        try:
            return self._src_extractor.extract_columns(schema, table, database=database or None)
        except Exception as exc:
            print(f"  ✗ Source extraction failed for '{schema}.{table}': {exc}")
            raise

    def _extract_target(self, schema: str, table: str, database: str = ""):
        """Extract Snowflake column metadata. Raises on failure."""
        try:
            return self._sf_extractor.extract_columns(schema, table, database=database or None)
        except Exception as exc:
            print(f"  ✗ Snowflake extraction failed for '{schema}.{table}': {exc}")
            raise


# ---------------------------------------------------------------------------
# Internal print helpers
# ---------------------------------------------------------------------------

def _default_reason(dec) -> str:
    """Generate a default reason string from a MatchDecision."""
    method = dec.method or "unknown"
    score  = dec.final_score
    return f"Matched by {method} (confidence={score:.2f})"


def _print_header_v2(
    pg_schema: str,
    pg_table: str,
    sf_db: str,
    sf_schema: str,
    sf_table: str,
    active_model: str,
    is_ai_active: bool,
    pg_db: str = "",
) -> None:
    sep = "=" * 65
    ai_line = (
        f"AI ({active_model})"
        if is_ai_active
        else "Static + Fuzzy (no DIAL_API_KEY / CLAUDE_API_KEY)"
    )
    pg_label = f"{pg_db}.{pg_schema}.{pg_table}" if pg_db else f"{pg_schema}.{pg_table}"
    print(f"\n{sep}")
    print(f"  MIGRATION VALIDATOR — Plan-Driven Pipeline (v2)")
    print(f"  Source  : {pg_label}")
    print(f"  Target  : Snowflake   → {sf_db}.{sf_schema}.{sf_table}")
    print(f"  Mode    : {ai_line}")
    print(f"  Steps   : Extract → Exact → Fuzzy → Score → AI(ambiguous only) → Validate → Generate")
    print(sep)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migration Validator — PostgreSQL→Snowflake Validation SQL + YAML Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Available AI Models (via DIAL):
  {', '.join(AVAILABLE_MODELS)}

Examples:
  python src/validation_pipeline.py --pg-table events --sf-table EVENTS
  python src/validation_pipeline.py --pg-table events --sf-table EVENTS --model gpt-4o-mini
  python src/validation_pipeline.py \\
      --pg-schema public --pg-table users \\
      --sf-schema MY_SCHEMA --sf-table USERS \\
      --sf-database MY_DB --model gpt-4-turbo
        """,
    )
    parser.add_argument(
        "--pg-schema",
        default=os.getenv("SOURCE_SCHEMA", "public"),
        help="PostgreSQL schema (default: SOURCE_SCHEMA env or 'public')",
    )
    parser.add_argument(
        "--pg-table",
        required=True,
        help="PostgreSQL source table name",
    )
    parser.add_argument(
        "--sf-schema",
        default=os.getenv("SNOWFLAKE_SCHEMA", ""),
        help="Snowflake schema (default: SNOWFLAKE_SCHEMA env)",
    )
    parser.add_argument(
        "--sf-table",
        required=True,
        help="Snowflake target table name",
    )
    parser.add_argument(
        "--sf-database",
        default=os.getenv("SNOWFLAKE_DATABASE", ""),
        help="Snowflake database (default: SNOWFLAKE_DATABASE env)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("DIAL_MODEL", "gpt-4o"),
        choices=AVAILABLE_MODELS + ["gpt-4o"],  # allow any string too
        help=f"AI model to use. Choices: {', '.join(AVAILABLE_MODELS)}. Default: gpt-4o",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List all available AI models and exit",
    )
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    if args.list_models:
        print("\nAvailable AI Models:")
        for m in AVAILABLE_MODELS:
            print(f"  • {m}")
        sys.exit(0)

    pipeline = ValidationPipeline(model=args.model)
    try:
        pipeline.run_with_plan(
            pg_schema=args.pg_schema,
            pg_table=args.pg_table,
            sf_schema=args.sf_schema,
            sf_table=args.sf_table,
            sf_database=args.sf_database,
        )
    except Exception as err:
        print(f"\n✗ Pipeline failed: {err}", file=sys.stderr)
        sys.exit(1)
