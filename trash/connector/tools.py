"""
Migration Intelligence Connector — Tool Implementations
========================================================
Purpose-built tools that Gemini Enterprise calls to perform governed
migration-validation workflows.

Every tool:
  - Has a clear description and strict I/O schema
  - Returns compact structured responses (never raw SQL dumps)
  - Records audit events for write actions
  - Tracks metrics via the @_observe decorator
  - Never exposes secrets / raw credentials
  - Uses structured enterprise error codes (ConnectorErrorCode)

Tool catalogue:
  discover_connections       list_databases         list_schemas
  list_tables                get_table_schema       get_table_mapping
  get_column_mappings        get_pending_reviews    get_rule
  get_applicable_rules       get_validation_plan    generate_validation_plan
  generate_validation_sql    execute_validation     get_validation_result
  get_validation_failures    get_migration_summary  get_coverage
  approve_mapping            batch_approve_mappings  reject_mapping
  modify_mapping             approve_rule            approve_plan
  get_business_metrics
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("migration_connector")

# Ensure src/ is on path when called from webapp or tests
_SRC = Path(__file__).resolve().parents[1]
_ROOT = _SRC.parent
for _p in (str(_SRC), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from connector.audit import audit_logger, AuditRecord
from connector.approval_store import (
    approval_store, ApprovalRecord, ApprovalStatus,
    AUTO_ACCEPT_THRESHOLD, REVIEW_THRESHOLD,
)
from connector.metrics import metrics_tracker


# ---------------------------------------------------------------------------
# Enterprise error codes
# ---------------------------------------------------------------------------

class ConnectorErrorCode(str, Enum):
    AUTHENTICATION_ERROR  = "AUTHENTICATION_ERROR"
    AUTHORIZATION_ERROR   = "AUTHORIZATION_ERROR"
    SOURCE_UNAVAILABLE    = "SOURCE_UNAVAILABLE"
    TARGET_UNAVAILABLE    = "TARGET_UNAVAILABLE"
    OBJECT_NOT_FOUND      = "OBJECT_NOT_FOUND"
    INVALID_REQUEST       = "INVALID_REQUEST"
    PLAN_INVALID          = "PLAN_INVALID"
    RULE_NOT_FOUND        = "RULE_NOT_FOUND"
    VALIDATION_FAILED     = "VALIDATION_FAILED"
    EXECUTION_TIMEOUT     = "EXECUTION_TIMEOUT"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(msg: str, code: Optional[ConnectorErrorCode] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"status": "error", "message": msg}
    if code is not None:
        result["error_code"] = code.value
    return result


def _ok(**kwargs) -> Dict[str, Any]:
    return {"status": "ok", **kwargs}


def _confidence_band(conf: float) -> str:
    if conf >= AUTO_ACCEPT_THRESHOLD:
        return "auto_accepted"
    if conf >= REVIEW_THRESHOLD:
        return "ai_assisted_review"
    return "mandatory_review"


# ---------------------------------------------------------------------------
# Observability decorator
# ---------------------------------------------------------------------------

def _observe(fn):
    """
    Wraps every tool function to emit a structured observability log line on
    each call: tool_name, request_id, execution_time_ms, status.

    Does NOT log: passwords, connection strings, raw data payloads.
    """
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        request_id = str(uuid.uuid4())[:8]
        t0 = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            status = result.get("status", "ok")
            # Structured log — safe fields only
            logger.info(
                "tool=%s request_id=%s status=%s latency_ms=%s",
                fn.__name__, request_id, status, elapsed_ms,
            )
            return result
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            logger.error(
                "tool=%s request_id=%s status=error latency_ms=%s error=%s",
                fn.__name__, request_id, elapsed_ms, type(exc).__name__,
            )
            raise
    return _wrapper


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------
_DEFAULT_PAGE_SIZE = 50


def _paginate(items: List[Any], page: int, page_size: int) -> Dict[str, Any]:
    """Return a page slice with pagination metadata."""
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "items":       items[start:end],
        "page":        page,
        "page_size":   page_size,
        "total":       total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "has_more":    end < total,
    }


# ---------------------------------------------------------------------------
# 1. discover_connections
# ---------------------------------------------------------------------------

def discover_connections() -> Dict[str, Any]:
    """
    List all configured source connections and the Snowflake target.

    Returns a compact summary — no passwords or host details are included.
    """
    try:
        from validate_cli import _normalize_db_type, _DB_TYPE_LABELS, _apply_database_registry
        from setup_wizard import print_connection_registry

        registry = print_connection_registry(_ROOT / ".env")
        connections = []
        for rec in registry:
            rec = dict(rec)
            rec["db_type"] = _normalize_db_type(rec["db_type"])
            rec = _apply_database_registry(rec)
            connections.append({
                "slot":     f"SRC_{rec['index']}",
                "type":     _DB_TYPE_LABELS.get(rec["db_type"], rec["db_type"]),
                "database": rec["database"],
                "schema":   rec["schema"],
                "host":     rec["host"],
            })

        sf = {
            "account":  os.getenv("SNOWFLAKE_ACCOUNT", ""),
            "database": os.getenv("SNOWFLAKE_DATABASE", ""),
            "schema":   os.getenv("SNOWFLAKE_SCHEMA", ""),
        }
        return _ok(source_connections=connections, snowflake_target=sf)
    except Exception as exc:
        return _err(f"discover_connections failed: {exc}")


# ---------------------------------------------------------------------------
# 2. list_databases
# ---------------------------------------------------------------------------

def list_databases(source_slot: str) -> Dict[str, Any]:
    """
    List databases available on a source connection.

    Args:
        source_slot: e.g. "SRC_1"
    """
    try:
        from validate_cli import _normalize_db_type, _apply_database_registry
        from setup_wizard import print_connection_registry, _discover_postgres_databases, _discover_mssql_databases

        registry = print_connection_registry(_ROOT / ".env")
        slot_idx = int(source_slot.replace("SRC_", "").strip())
        rec = next((r for r in registry if int(r.get("index", 0)) == slot_idx), None)
        if rec is None:
            return _err(f"No connection found for {source_slot}")

        rec = dict(rec)
        rec["db_type"] = _normalize_db_type(rec["db_type"])
        pw = os.getenv(f"{rec.get('prefix', '')}PASSWORD", "")

        if rec["db_type"] == "postgresql":
            dbs = _discover_postgres_databases(rec["host"], int(rec.get("port", 5432)), rec["username"], pw)
        elif rec["db_type"] == "mssql":
            dbs = _discover_mssql_databases(rec["host"], int(rec.get("port", 1433)), rec["username"], pw, rec.get("auth", ""))
        else:
            dbs = [rec.get("database", "")]

        return _ok(source_slot=source_slot, databases=dbs)
    except Exception as exc:
        return _err(f"list_databases failed: {exc}")


# ---------------------------------------------------------------------------
# 3. list_schemas
# ---------------------------------------------------------------------------

def list_schemas(source_slot: str, database: str) -> Dict[str, Any]:
    """List schemas in a source database."""
    try:
        from validate_cli import _normalize_db_type, _apply_database_registry
        from setup_wizard import print_connection_registry, _discover_postgres_schemas, _discover_mssql_schemas

        registry = print_connection_registry(_ROOT / ".env")
        slot_idx = int(source_slot.replace("SRC_", "").strip())
        rec = next((r for r in registry if int(r.get("index", 0)) == slot_idx), None)
        if rec is None:
            return _err(f"No connection for {source_slot}")

        rec = dict(rec)
        rec["db_type"] = _normalize_db_type(rec["db_type"])
        pw = os.getenv(f"{rec.get('prefix', '')}PASSWORD", "")

        if rec["db_type"] == "postgresql":
            schemas = _discover_postgres_schemas(rec["host"], int(rec.get("port", 5432)), database, rec["username"], pw)
        elif rec["db_type"] == "mssql":
            schemas = _discover_mssql_schemas(rec["host"], int(rec.get("port", 1433)), database, rec["username"], pw, rec.get("auth", ""))
        else:
            # Athena (and other schema-less systems) have no separate schema concept —
            # the database itself is the schema. rec["schema"] is "" (present, not missing)
            # when SRC_N_SCHEMA is unset, so `.get("schema", "public")` never falls back;
            # "public" wouldn't be the right default here anyway.
            schemas = [rec.get("schema") or database]

        return _ok(source_slot=source_slot, database=database, schemas=schemas)
    except Exception as exc:
        return _err(f"list_schemas failed: {exc}")


# ---------------------------------------------------------------------------
# 4. list_tables
# ---------------------------------------------------------------------------

def list_tables(source_slot: str, database: str, schema: str) -> Dict[str, Any]:
    """List tables in a source schema."""
    try:
        from validate_cli import _normalize_db_type, _apply_database_registry
        from setup_wizard import print_connection_registry
        from sql_extractor import ExtractorFactory

        registry = print_connection_registry(_ROOT / ".env")
        slot_idx = int(source_slot.replace("SRC_", "").strip())
        rec = next((r for r in registry if int(r.get("index", 0)) == slot_idx), None)
        if rec is None:
            return _err(f"No connection for {source_slot}")

        rec = dict(rec)
        rec["db_type"] = _normalize_db_type(rec["db_type"])
        pw = os.getenv(f"{rec.get('prefix', '')}PASSWORD", "")

        extractor = ExtractorFactory.create(
            rec["db_type"], host=rec["host"], port=int(rec.get("port") or 0),
            database=database, username=rec["username"], password=pw,
            auth=rec.get("auth", ""), s3_output=rec.get("s3_output", ""),
        )
        tables = extractor.list_tables(schema)
        return _ok(source_slot=source_slot, database=database, schema=schema, tables=tables, count=len(tables))
    except Exception as exc:
        return _err(f"list_tables failed: {exc}")


# ---------------------------------------------------------------------------
# 5. get_table_schema
# ---------------------------------------------------------------------------

def get_table_schema(
    source_slot: str, database: str, schema: str, table: str
) -> Dict[str, Any]:
    """
    Return column metadata for a source table.
    Returns compact column list — no raw SQL, no PII data.
    """
    try:
        from validate_cli import _normalize_db_type, _apply_database_registry
        from setup_wizard import print_connection_registry
        from sql_extractor import ExtractorFactory

        registry = print_connection_registry(_ROOT / ".env")
        slot_idx = int(source_slot.replace("SRC_", "").strip())
        rec = next((r for r in registry if int(r.get("index", 0)) == slot_idx), None)
        if rec is None:
            return _err(f"No connection for {source_slot}")

        rec = dict(rec)
        rec["db_type"] = _normalize_db_type(rec["db_type"])
        pw = os.getenv(f"{rec.get('prefix', '')}PASSWORD", "")

        extractor = ExtractorFactory.create(
            rec["db_type"], host=rec["host"], port=int(rec.get("port") or 0),
            database=database, username=rec["username"], password=pw,
            auth=rec.get("auth", ""), s3_output=rec.get("s3_output", ""),
        )
        cols = extractor.extract_columns(schema, table)
        column_list = [
            {"name": c.column_name, "type": c.data_type,
             "nullable": getattr(c, "is_nullable", True)}
            for c in cols
        ]
        return _ok(
            source_slot=source_slot, database=database, schema=schema, table=table,
            column_count=len(column_list), columns=column_list,
        )
    except Exception as exc:
        return _err(f"get_table_schema failed: {exc}")


# ---------------------------------------------------------------------------
# 6. get_table_mapping
# ---------------------------------------------------------------------------

def get_table_mapping(source_table: str, layer: str = "bronze") -> Dict[str, Any]:
    """
    Return the canonical validation plan summary for a table (if it exists).
    Compact view — stats, status, warnings, not raw SQL.
    """
    try:
        from core.plan_store import PlanStore
        store = PlanStore()
        plan = store.load_for_table(source_table, layer)
        if plan is None:
            return _ok(
                table=source_table, layer=layer,
                exists=False,
                message=f"No plan found for '{source_table}' in layer '{layer}'. Run generate_validation_plan first.",
            )

        excl = plan.exclusion_summary()
        return _ok(
            table=source_table,
            layer=layer,
            exists=True,
            status=plan.status,
            source=f"{plan.source_schema}.{plan.source_table}",
            target=f"{plan.target_schema}.{plan.target_table}",
            source_db_type=plan.source_db_type,
            generated_at=plan.generated_at,
            generated_by=plan.generated_by,
            model_used=plan.model_used,
            column_coverage_pct=excl["coverage_pct"],
            total_source_columns=excl["total_source_columns"],
            validated_columns=excl["validated"],
            excluded_columns=excl["excluded_count"],
            exact_matches=len(plan.exact_matches),
            fuzzy_matches=len(plan.fuzzy_matches),
            ai_resolved=len(plan.ai_resolved_matches),
            warnings=plan.warnings,
            ambiguities=plan.ambiguities,
            unmatched_source=plan.unmatched_source_columns,
            unmatched_target=plan.unmatched_target_columns,
            has_fivetran_active=plan.has_fivetran_active,
        )
    except Exception as exc:
        return _err(f"get_table_mapping failed: {exc}")


# ---------------------------------------------------------------------------
# 7. get_column_mappings
# ---------------------------------------------------------------------------

def get_column_mappings(
    source_table: str, layer: str = "bronze",
    filter_status: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return detailed column mappings for a table.

    Args:
        source_table: Source table name
        layer: Medallion layer (bronze/silver/gold)
        filter_status: Optional filter — "needs_review", "auto_accepted", "all"
    """
    try:
        from core.plan_store import PlanStore
        store = PlanStore()
        plan = store.load_for_table(source_table, layer)
        if plan is None:
            return _ok(table=source_table, exists=False, mappings=[])

        mappings = []
        for m in plan.mappings:
            band = _confidence_band(m.confidence)
            if filter_status == "needs_review" and band == "auto_accepted":
                continue
            if filter_status == "auto_accepted" and band != "auto_accepted":
                continue

            # Check if there's a pending approval decision
            rec_id = f"{source_table}.{m.source_column}"
            approval_rec = approval_store.get(rec_id)
            approval_status = approval_rec.status if approval_rec else (
                ApprovalStatus.AUTO_ACCEPTED.value if band == "auto_accepted"
                else ApprovalStatus.PENDING.value
            )

            mappings.append({
                "source_column":       m.source_column,
                "source_type":         m.source_type,
                "target_column":       m.target_column,
                "target_type":         m.target_type,
                "confidence":          round(m.confidence, 3),
                "confidence_band":     band,
                "match_method":        m.match_method,
                "transformation_rule": m.transformation_rule,
                "ai_resolved":         m.ai_resolved,
                "reason":              m.reason,
                "skip_validation":     m.skip_validation,
                "skip_reason":         m.skip_reason,
                "is_primary_key":      m.is_primary_key,
                "approval_status":     approval_status,
            })

        needs_review = sum(1 for m in mappings if m["confidence_band"] != "auto_accepted" and not m["skip_validation"])
        return _ok(
            table=source_table,
            layer=layer,
            total_mappings=len(mappings),
            needs_review_count=needs_review,
            mappings=mappings,
        )
    except Exception as exc:
        return _err(f"get_column_mappings failed: {exc}")


# ---------------------------------------------------------------------------
# 8. get_pending_reviews
# ---------------------------------------------------------------------------

def get_pending_reviews(table: Optional[str] = None) -> Dict[str, Any]:
    """
    Return all mappings awaiting human approval.

    Args:
        table: Optional — filter to a specific table. None returns all pending.
    """
    try:
        pending = approval_store.pending()
        if table:
            pending = [r for r in pending if r.table == table]

        items = []
        for r in pending:
            items.append({
                "id":                   r.id,
                "table":                r.table,
                "source_column":        r.source_column,
                "target_column":        r.target_column,
                "confidence":           round(r.confidence, 3),
                "match_method":         r.match_method,
                "transformation_rule":  r.transformation_rule,
                "ai_recommendation":    r.ai_recommendation,
                "reason":               r.reason,
                "entity_type":          r.entity_type,
                "created_at":           r.created_at,
            })

        return _ok(
            pending_count=len(items),
            filter_table=table,
            pending_reviews=items,
        )
    except Exception as exc:
        return _err(f"get_pending_reviews failed: {exc}")


# ---------------------------------------------------------------------------
# 9. get_rule
# ---------------------------------------------------------------------------

def get_rule(rule_id: str) -> Dict[str, Any]:
    """
    Return full Rule Book entry for a given rule ID.

    Explains: source type, target type, semantic meaning, SQL transformation,
    validation strategy, status, and scope.
    """
    try:
        from rule_book import rule_book
        entry = rule_book.get_rule_by_id(rule_id)
        if entry is None:
            return _err(f"Rule '{rule_id}' not found. Use get_applicable_rules to browse available rules.")

        rule_status = "base" if not entry.is_learned else getattr(entry, "status", "draft")
        return _ok(
            rule_id=entry.id,
            display_name=entry.display_name,
            description=entry.description,
            source_type=entry.source_type,
            target_type=entry.target_type,
            when_to_apply=entry.when_to_apply,
            source_sql_template=entry.pg_sql_template,
            snowflake_sql_template=entry.sf_sql_template,
            is_learned=entry.is_learned,
            status=rule_status,
            reuses_rule=getattr(entry, "reuses_rule", None),
            example=entry.example,
        )
    except Exception as exc:
        return _err(f"get_rule failed: {exc}")


# ---------------------------------------------------------------------------
# 10. get_applicable_rules
# ---------------------------------------------------------------------------

def get_applicable_rules(
    source_type: Optional[str] = None,
    target_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return rules applicable to a type pair, or all rules if no types given.

    Args:
        source_type: e.g. "boolean", "character varying"
        target_type: e.g. "BOOLEAN", "VARCHAR"
    """
    try:
        from rule_book import rule_book
        all_rules = rule_book.all_rules()

        if source_type or target_type:
            st_lower = (source_type or "").lower()
            tt_lower = (target_type or "").lower()
            all_rules = [
                r for r in all_rules
                if (not st_lower or st_lower in r.source_type.lower() or r.source_type == "*")
                and (not tt_lower or tt_lower in r.target_type.lower() or r.target_type == "*")
            ]

        return _ok(
            filter_source_type=source_type,
            filter_target_type=target_type,
            rule_count=len(all_rules),
            rules=[{
                "id":           r.id,
                "display_name": r.display_name,
                "source_type":  r.source_type,
                "target_type":  r.target_type,
                "description":  r.description,
                "is_learned":   r.is_learned,
                "status":       "base" if not r.is_learned else getattr(r, "status", "draft"),
            } for r in all_rules],
        )
    except Exception as exc:
        return _err(f"get_applicable_rules failed: {exc}")


# ---------------------------------------------------------------------------
# 11. get_validation_plan
# ---------------------------------------------------------------------------

def get_validation_plan(source_table: str, layer: str = "bronze") -> Dict[str, Any]:
    """
    Return the full serialized CanonicalValidationPlan for a table.

    Returns compact form optimized for token efficiency (not the full raw plan JSON).
    """
    try:
        from core.plan_store import PlanStore
        store = PlanStore()
        plan = store.load_for_table(source_table, layer)
        if plan is None:
            return _ok(
                exists=False, table=source_table, layer=layer,
                message="No plan exists yet. Run generate_validation_plan first.",
            )

        excl = plan.exclusion_summary()
        return _ok(
            exists=True,
            table=source_table,
            layer=layer,
            plan_status=plan.status,
            source_db_type=plan.source_db_type,
            source_fqn=f"{plan.source_database}.{plan.source_schema}.{plan.source_table}",
            target_fqn=f"{plan.target_database}.{plan.target_schema}.{plan.target_table}",
            generated_at=plan.generated_at,
            generated_by=plan.generated_by,
            model_used=plan.model_used,
            ai_calls_made=plan.ai_calls_made,
            coverage_pct=excl["coverage_pct"],
            total_columns=excl["total_source_columns"],
            validated=excl["validated"],
            excluded=excl["excluded_count"],
            warnings=plan.warnings[:5],   # cap at 5 for token efficiency
            ambiguities=plan.ambiguities[:5],
            pk_source=plan.source_primary_keys,
            pk_target=plan.target_primary_keys,
            pk_mismatch=plan.pk_mismatch,
            pk_mismatch_reason=plan.pk_mismatch_reason if plan.pk_mismatch else None,
        )
    except Exception as exc:
        return _err(f"get_validation_plan failed: {exc}")


# ---------------------------------------------------------------------------
# 12. generate_validation_plan
# ---------------------------------------------------------------------------

def generate_validation_plan(
    source_slot: str,
    source_database: str,
    source_schema: str,
    source_table: str,
    target_database: str,
    target_schema: str,
    target_table: str,
    layer: str = "bronze",
    model: Optional[str] = None,
    exclude_columns: Optional[List[str]] = None,
    actor: str = "gemini_ai",
) -> Dict[str, Any]:
    """
    Generate and persist a CanonicalValidationPlan for a table.

    This is the core planning operation — it runs schema discovery,
    deterministic matching, confidence scoring, and AI resolution for
    ambiguous columns, then stores the plan.

    Args:
        source_slot:     e.g. "SRC_1"
        source_database: Source database name
        source_schema:   Source schema name
        source_table:    Source table name
        target_database: Snowflake database
        target_schema:   Snowflake schema
        target_table:    Snowflake table name
        layer:           Medallion layer for output (bronze/silver/gold)
        model:           AI model to use (default from env)
        exclude_columns: Columns to skip
        actor:           Who triggered this (user or "gemini_ai")
    """
    try:
        from validate_cli import _normalize_db_type, _apply_database_registry, _override_source_env
        from setup_wizard import print_connection_registry
        from sql_extractor import ExtractorFactory
        from validation_pipeline import ValidationPipeline
        from core.plan_store import PlanStore

        registry = print_connection_registry(_ROOT / ".env")
        slot_idx = int(source_slot.replace("SRC_", "").strip())
        rec = next((r for r in registry if int(r.get("index", 0)) == slot_idx), None)
        if rec is None:
            return _err(f"No connection for {source_slot}")

        rec = dict(rec)
        rec["db_type"] = _normalize_db_type(rec["db_type"])
        rec = _apply_database_registry(rec)
        _override_source_env(rec)

        pw = os.getenv(f"{rec.get('prefix', '')}PASSWORD", "")
        extractor = ExtractorFactory.create(
            rec["db_type"], host=rec["host"], port=int(rec.get("port") or 0),
            database=source_database, username=rec["username"], password=pw,
            auth=rec.get("auth", ""), s3_output=rec.get("s3_output", ""),
        )

        use_model = model or os.getenv("DIAL_MODEL", "gpt-4o")
        pipeline = ValidationPipeline(model=use_model, source_extractor=extractor)

        output_dir = _ROOT / "Project" / "config" / layer
        result, plan = pipeline.run_with_plan(
            pg_schema=source_schema,
            pg_table=source_table,
            sf_schema=target_schema,
            sf_table=target_table,
            sf_database=target_database,
            pg_database=source_database,
            exclude_columns=exclude_columns or None,
            source_db_type=rec["db_type"],
            output_dir=output_dir,
            layer=layer,
        )

        # Register pending reviews for low-confidence mappings
        for m in plan.mappings:
            if not m.skip_validation and m.confidence < AUTO_ACCEPT_THRESHOLD:
                rec_id = f"{source_table}.{m.source_column}"
                existing = approval_store.get(rec_id)
                if existing is None:
                    approval_store.upsert(ApprovalRecord(
                        id=rec_id,
                        entity_type="mapping",
                        table=source_table,
                        source_column=m.source_column,
                        target_column=m.target_column,
                        confidence=m.confidence,
                        match_method=m.match_method,
                        transformation_rule=m.transformation_rule,
                        ai_recommendation=m.reason,
                        reason=m.reason,
                        status=ApprovalStatus.PENDING.value,
                    ))

        excl = plan.exclusion_summary()
        pending_count = sum(
            1 for m in plan.mappings
            if not m.skip_validation and m.confidence < AUTO_ACCEPT_THRESHOLD
        )

        # Audit
        audit_logger.log(AuditRecord(
            action="generate_validation_plan",
            entity_type="plan",
            entity_id=f"{layer}/{source_table}",
            actor=actor,
            new_state={"status": plan.status, "model": use_model},
            reason="Auto-generated by Gemini connector",
        ))

        # Metrics
        with metrics_tracker.time_operation("generate_validation_plan", table=source_table) as m_:
            m_.tables_processed = 1
            m_.columns_processed = excl["total_source_columns"]
            m_.mappings_automated = excl["validated"] - pending_count
            m_.mappings_reviewed = pending_count
            m_.manual_sql_avoided = 2  # source SQL + target SQL auto-generated
            m_.ai_calls_made = plan.ai_calls_made

        return _ok(
            table=source_table,
            layer=layer,
            plan_status=plan.status,
            source_db_type=rec["db_type"],
            source_fqn=f"{source_database}.{source_schema}.{source_table}",
            target_fqn=f"{target_database}.{target_schema}.{target_table}",
            coverage_pct=excl["coverage_pct"],
            total_columns=excl["total_source_columns"],
            validated=excl["validated"],
            excluded=excl["excluded_count"],
            mappings_requiring_review=pending_count,
            warnings=plan.warnings[:5],
            ambiguities=plan.ambiguities[:5],
            model_used=use_model,
            yaml_path=str(result.yaml_path) if result.yaml_path else None,
        )
    except Exception as exc:
        return _err(f"generate_validation_plan failed: {exc}")


# ---------------------------------------------------------------------------
# 13. generate_validation_sql
# ---------------------------------------------------------------------------

def generate_validation_sql(source_table: str, layer: str = "bronze") -> Dict[str, Any]:
    """
    Return the generated SQL queries for a table (from the existing plan).
    Does NOT return raw SQL to Gemini — returns metadata about the files.
    """
    try:
        from core.plan_store import PlanStore
        store = PlanStore()
        plan = store.load_for_table(source_table, layer)
        if plan is None:
            return _ok(
                exists=False, table=source_table,
                message="No plan found. Run generate_validation_plan first.",
            )

        # Find existing generated SQL files
        sql_dir = _ROOT / "Project" / "config" / layer / "data_validation"
        pattern = f"{source_table.lower()}*.sql"
        sql_files = list(sql_dir.glob(pattern)) if sql_dir.exists() else []

        return _ok(
            table=source_table,
            layer=layer,
            plan_status=plan.status,
            sql_files_found=len(sql_files),
            sql_file_paths=[str(f) for f in sql_files],
            note="SQL files are generated by generate_validation_plan. Use execute_validation to run them.",
        )
    except Exception as exc:
        return _err(f"generate_validation_sql failed: {exc}")


# ---------------------------------------------------------------------------
# 14. execute_validation
# ---------------------------------------------------------------------------

def execute_validation(
    source_table: str,
    layer: str = "bronze",
    actor: str = "gemini_ai",
) -> Dict[str, Any]:
    """
    Execute the validation for a table using the stored plan and YAML config.

    Returns a compact business-oriented result — not raw SQL output.
    """
    try:
        from validation.validation_executor import ValidationExecutor
        from core.plan_store import PlanStore

        store = PlanStore()
        plan = store.load_for_table(source_table, layer)
        if plan is None:
            return _err(f"No plan for '{source_table}' in layer '{layer}'. Run generate_validation_plan first.")

        # Find the YAML config
        yaml_dir = _ROOT / "Project" / "config" / layer / "data_validation"
        yaml_files = list(yaml_dir.glob(f"{source_table.lower()}*.yaml")) if yaml_dir.exists() else []
        if not yaml_files:
            return _err(f"No validation YAML found for '{source_table}'. Run generate_validation_plan first.")

        executor = ValidationExecutor(base_dir=str(_ROOT), environment="dev")
        results = []
        for yaml_file in yaml_files:
            import yaml
            with open(yaml_file, encoding="utf-8") as f:
                config = yaml.safe_load(f)
            if not isinstance(config, dict):
                continue
            result = executor._run_single_file(str(yaml_file), config) if hasattr(executor, "_run_single_file") else {}
            results.append({
                "yaml_file": yaml_file.name,
                "result":    result,
            })

        # Audit
        audit_logger.log(AuditRecord(
            action="execute_validation",
            entity_type="validation_run",
            entity_id=f"{layer}/{source_table}",
            actor=actor,
            reason="Triggered by Gemini connector",
        ))

        return _ok(
            table=source_table,
            layer=layer,
            validation_files_run=len(results),
            results=results,
        )
    except Exception as exc:
        return _err(f"execute_validation failed: {exc}")


# ---------------------------------------------------------------------------
# 15. get_validation_result
# ---------------------------------------------------------------------------

def get_validation_result(source_table: str, layer: str = "bronze") -> Dict[str, Any]:
    """
    Return the most recent validation result for a table.
    Searches output/ for result files.
    """
    try:
        output_dir = _ROOT / "output"
        # Look for result CSV or JSON files
        result_files = []
        for ext in ("*.json", "*.csv"):
            result_files.extend(output_dir.rglob(f"*{source_table.lower()}*{ext}"))
        result_files = sorted(result_files, key=lambda f: f.stat().st_mtime, reverse=True)

        if not result_files:
            return _ok(
                table=source_table, layer=layer, exists=False,
                message="No validation results found. Run execute_validation first.",
            )

        latest = result_files[0]
        size_kb = round(latest.stat().st_size / 1024, 1)

        # Try to parse if JSON
        summary = {}
        if latest.suffix == ".json":
            try:
                with open(latest, encoding="utf-8") as f:
                    data = json.load(f)
                # Extract top-level status/metrics only
                summary = {
                    k: v for k, v in data.items()
                    if k in ("status", "pass_count", "fail_count", "mismatch_count",
                             "source_count", "target_count", "coverage_pct")
                }
            except Exception:
                pass

        return _ok(
            table=source_table,
            layer=layer,
            latest_result_file=latest.name,
            last_modified=datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc).isoformat(),
            file_size_kb=size_kb,
            summary=summary,
        )
    except Exception as exc:
        return _err(f"get_validation_result failed: {exc}")


# ---------------------------------------------------------------------------
# 16. get_validation_failures
# ---------------------------------------------------------------------------

def get_validation_failures(source_table: str, layer: str = "bronze") -> Dict[str, Any]:
    """
    Return structured failure analysis for a table — not raw data rows.

    Provides:
    - failed columns
    - mismatch count
    - rule applied
    - likely cause
    - recommended action
    """
    try:
        from core.plan_store import PlanStore
        store = PlanStore()
        plan = store.load_for_table(source_table, layer)
        if plan is None:
            return _ok(table=source_table, exists=False, failures=[])

        # Find mismatch CSVs
        output_dir = _ROOT / "output"
        mismatch_files = sorted(
            output_dir.rglob(f"*{source_table.lower()}*mismatch*"),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )

        failures = []
        if mismatch_files:
            try:
                import pandas as pd
                df = pd.read_csv(mismatch_files[0], nrows=500)
                for col in df.columns:
                    if col.startswith("_") or col in ("pk", "source_pk", "target_pk"):
                        continue
                    mismatch_count = int(df[col].notna().sum()) if col in df.columns else 0
                    if mismatch_count > 0:
                        # Find rule for this column
                        mapping = next((m for m in plan.mappings if m.source_column.lower() == col.lower()), None)
                        rule_id = mapping.transformation_rule if mapping else "unknown"
                        failures.append({
                            "column":          col,
                            "mismatch_count":  mismatch_count,
                            "rule_applied":    rule_id,
                            "likely_cause":    _infer_likely_cause(col, rule_id),
                            "recommended_action": _infer_action(rule_id),
                        })
            except Exception:
                pass

        # Warnings from the plan
        plan_warnings = [
            {"type": "plan_warning", "message": w}
            for w in plan.warnings[:10]
        ]

        return _ok(
            table=source_table,
            layer=layer,
            plan_status=plan.status,
            failures_count=len(failures),
            failures=failures,
            plan_warnings=plan_warnings,
            ambiguities=plan.ambiguities[:5],
        )
    except Exception as exc:
        return _err(f"get_validation_failures failed: {exc}")


def _infer_likely_cause(column: str, rule_id: str) -> str:
    col_lower = column.lower()
    rule_lower = rule_id.lower()
    if "timestamp" in col_lower or "date" in col_lower or "time" in col_lower:
        return "Timezone or precision mismatch — check TIMESTAMP_UTC rule"
    if "boolean" in rule_lower or "flag" in col_lower or "active" in col_lower:
        return "Boolean representation mismatch (TRUE/FALSE vs 1/0)"
    if "numeric" in rule_lower or "amount" in col_lower or "price" in col_lower:
        return "Numeric precision or scale difference"
    if "uuid" in col_lower or "id" in col_lower:
        return "Case mismatch or padding difference"
    return "Type normalization difference — review the applied rule"


def _infer_action(rule_id: str) -> str:
    rule_lower = rule_id.lower()
    if "timestamp" in rule_lower:
        return "Review TIMESTAMP_UTC rule. Ensure both sides convert to UTC before comparison."
    if "boolean" in rule_lower:
        return "Verify BOOLEAN_TO_NUMBER rule applies. Source may store 'true'/'false' as text."
    if "numeric" in rule_lower:
        return "Check scale and precision. Cast both sides to DECIMAL(38, N) before compare."
    return f"Review rule '{rule_id}' in Rule Book for exact transformation applied."


# ---------------------------------------------------------------------------
# 17. get_migration_summary
# ---------------------------------------------------------------------------

def get_migration_summary(layer: str = "bronze") -> Dict[str, Any]:
    """
    Return a portfolio health summary across all plans in a layer.

    Compact, business-oriented — no raw SQL or large data dumps.
    """
    try:
        from core.plan_store import PlanStore
        from core.validation_plan import PlanStatus

        store = PlanStore()
        plan_paths = store.list_plans(layer)

        if not plan_paths:
            return _ok(
                layer=layer, total_tables=0,
                message=f"No plans found in layer '{layer}'. Run generate_validation_plan for each table.",
            )

        summary = {
            "layer": layer,
            "total_tables": len(plan_paths),
            "complete": 0, "partial": 0, "ambiguous": 0, "invalid": 0,
            "total_source_columns": 0, "total_validated": 0,
            "total_excluded": 0,
            "avg_coverage_pct": 0.0,
            "warnings_total": 0,
            "ambiguities_total": 0,
            "unmatched_source_total": 0,
            "tables_needing_attention": [],
        }

        coverages = []
        for path in plan_paths:
            try:
                plan = store.load(path)
            except Exception:
                continue

            excl = plan.exclusion_summary()
            summary[plan.status] = summary.get(plan.status, 0) + 1
            summary["total_source_columns"] += excl["total_source_columns"]
            summary["total_validated"] += excl["validated"]
            summary["total_excluded"] += excl["excluded_count"]
            summary["warnings_total"] += len(plan.warnings)
            summary["ambiguities_total"] += len(plan.ambiguities)
            summary["unmatched_source_total"] += len(plan.unmatched_source_columns)
            coverages.append(excl["coverage_pct"])

            needs_attention = (
                plan.status in (PlanStatus.AMBIGUOUS.value, PlanStatus.INVALID.value)
                or excl["coverage_pct"] < 95.0
                or plan.ambiguities
                or plan.unmatched_source_columns
            )
            if needs_attention:
                summary["tables_needing_attention"].append({
                    "table":        plan.source_table,
                    "status":       plan.status,
                    "coverage_pct": excl["coverage_pct"],
                    "ambiguities":  len(plan.ambiguities),
                    "unmatched":    len(plan.unmatched_source_columns),
                    "warnings":     len(plan.warnings),
                })

        summary["avg_coverage_pct"] = round(sum(coverages) / len(coverages), 1) if coverages else 0.0

        # Pending approval items
        pending = approval_store.pending()
        summary["pending_approvals"] = len(pending)

        return _ok(**summary)
    except Exception as exc:
        return _err(f"get_migration_summary failed: {exc}")


# ---------------------------------------------------------------------------
# 18. approve_mapping
# ---------------------------------------------------------------------------

def approve_mapping(
    record_id: str,
    actor: str,
    reason: str = "",
) -> Dict[str, Any]:
    """
    Approve a pending column mapping.

    Rule: AI recommends. Human approves. This action is audited.
    Idempotent: approving an already-approved mapping returns ok with action="already_approved".
    Auto-accepted mappings (confidence >= threshold, never stored) are acknowledged without error.

    Args:
        record_id: Format "<table>.<source_column>"
        actor: Authenticated user identity
        reason: Optional justification
    """
    if not actor or actor.lower() in ("", "gemini_ai", "ai"):
        return _err("approve_mapping requires an authenticated human actor. AI cannot self-approve.")

    try:
        existing = approval_store.get(record_id)

        # Already approved — idempotent
        if existing is not None and existing.status == ApprovalStatus.APPROVED.value:
            return _ok(
                record_id=record_id,
                action="already_approved",
                actor=existing.decided_by,
                decided_at=existing.decided_at,
                mapping=f"{existing.source_column} → {existing.target_column}",
                table=existing.table,
                note="Mapping was already approved — no change made.",
            )

        # Auto-accepted mapping (high-confidence, never written to store)
        if existing is None:
            return _ok(
                record_id=record_id,
                action="auto_accepted",
                actor="system",
                note=(
                    f"No pending record found for '{record_id}'. "
                    "This mapping was likely auto-accepted (confidence >= threshold) and needs no human action."
                ),
            )

        result = approval_store.approve(record_id, actor, reason)
        if result is None:
            return _err(f"No pending mapping found for '{record_id}'.")

        audit_logger.log(AuditRecord(
            action="approve_mapping",
            entity_type="mapping",
            entity_id=record_id,
            actor=actor,
            previous={"status": "pending"},
            new_state={"status": "approved"},
            reason=reason,
        ))

        return _ok(
            record_id=record_id,
            action="approved",
            actor=actor,
            decided_at=result.decided_at,
            mapping=f"{result.source_column} → {result.target_column}",
            table=result.table,
        )
    except Exception as exc:
        return _err(f"approve_mapping failed: {exc}")


# ---------------------------------------------------------------------------
# 18b. batch_approve_mappings
# ---------------------------------------------------------------------------

def batch_approve_mappings(
    record_ids: List[str],
    actor: str,
    reason: str = "",
) -> Dict[str, Any]:
    """
    Approve multiple pending column mappings in a single call.

    Designed for agent workflows where many mappings need human sign-off:
    the agent collects all IDs from get_pending_reviews, presents them to
    the human, receives approval, then calls this once.

    Args:
        record_ids: List of "<table>.<source_column>" identifiers
        actor:      Authenticated human user
        reason:     Shared rationale applied to every approval

    Returns:
        Summary with per-record outcome (approved / already_approved / auto_accepted / error).
    """
    if not actor or actor.lower() in ("", "gemini_ai", "ai"):
        return _err("batch_approve_mappings requires an authenticated human actor.")
    if not record_ids:
        return _err("record_ids must be a non-empty list.")

    results = []
    approved_count = 0
    skipped_count = 0
    error_count = 0

    for rec_id in record_ids:
        outcome = approve_mapping(rec_id, actor, reason)
        action = outcome.get("action", "error")
        if outcome.get("status") == "error":
            error_count += 1
            results.append({"id": rec_id, "outcome": "error", "message": outcome.get("message", "")})
        elif action in ("already_approved", "auto_accepted"):
            skipped_count += 1
            results.append({"id": rec_id, "outcome": action})
        else:
            approved_count += 1
            results.append({"id": rec_id, "outcome": "approved"})

    return _ok(
        total=len(record_ids),
        approved=approved_count,
        skipped=skipped_count,
        errors=error_count,
        actor=actor,
        results=results,
    )


# ---------------------------------------------------------------------------
# 19. reject_mapping
# ---------------------------------------------------------------------------

def reject_mapping(
    record_id: str,
    actor: str,
    reason: str,
    create_jira_ticket: bool = True,
) -> Dict[str, Any]:
    """
    Reject a pending column mapping.

    Args:
        record_id: Format "<table>.<source_column>"
        actor: Authenticated user identity
        reason: Required — why this mapping is rejected
        create_jira_ticket: Auto-create JIRA ticket (default True if JIRA configured)
    """
    if not actor or actor.lower() in ("", "gemini_ai", "ai"):
        return _err("reject_mapping requires an authenticated human actor.")
    if not reason:
        return _err("A reason is required to reject a mapping.")

    try:
        result = approval_store.reject(record_id, actor, reason)
        if result is None:
            return _err(f"No pending mapping found for '{record_id}'.")

        audit_logger.log(AuditRecord(
            action="reject_mapping",
            entity_type="mapping",
            entity_id=record_id,
            actor=actor,
            previous={"status": "pending"},
            new_state={"status": "rejected"},
            reason=reason,
        ))

        response = _ok(
            record_id=record_id,
            action="rejected",
            actor=actor,
            decided_at=result.decided_at,
            reason=reason,
        )

        # Optionally create JIRA ticket for rejected mapping
        if create_jira_ticket:
            from connector import jira_client
            if jira_client.is_configured():
                try:
                    table_name = record_id.split(".")[0] if "." in record_id else record_id
                    jira_result = jira_client.create_ticket(
                        summary=f"Rejected Mapping: {record_id}",
                        description=(
                            f"A column mapping was rejected during migration validation review.\n\n"
                            f"📋 Mapping Details:\n"
                            f"  - Record ID: {record_id}\n"
                            f"  - Source Column: {record_id.split('.')[-1] if '.' in record_id else 'N/A'}\n"
                            f"  - Confidence: {result.confidence if hasattr(result, 'confidence') else 'N/A'}\n\n"
                            f"❌ Rejection Reason:\n"
                            f"{reason}\n\n"
                            f"👤 Rejected By: {actor}\n"
                            f"📅 Rejected At: {result.decided_at}\n\n"
                            f"🔍 Next Steps:\n"
                            f"1. Review source and target column definitions\n"
                            f"2. Confirm correct target column in Snowflake\n"
                            f"3. Update mapping using modify_mapping tool\n"
                            f"4. Re-run validation"
                        ),
                        labels=["migration-validator", "rejected-mapping", table_name]
                    )
                    response["jira_ticket"] = jira_result
                    response["message"] = f"Mapping rejected and JIRA ticket {jira_result['key']} created."
                except jira_client.JiraError as je:
                    logger.warning(f"Failed to create JIRA ticket: {je}")
                    response["jira_warning"] = f"Mapping rejected but JIRA ticket creation failed: {je}"

        return response
    except Exception as exc:
        return _err(f"reject_mapping failed: {exc}")


# ---------------------------------------------------------------------------
# 20. modify_mapping
# ---------------------------------------------------------------------------

def modify_mapping(
    record_id: str,
    actor: str,
    new_target_column: Optional[str] = None,
    new_transformation_rule: Optional[str] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """
    Modify a pending column mapping (change target or rule) and approve it.

    Args:
        record_id: Format "<table>.<source_column>"
        actor: Authenticated user identity
        new_target_column: Override the AI-suggested target column
        new_transformation_rule: Override the AI-suggested rule
        reason: Why this modification was needed
    """
    if not actor or actor.lower() in ("", "gemini_ai", "ai"):
        return _err("modify_mapping requires an authenticated human actor.")

    try:
        existing = approval_store.get(record_id)
        previous_state = existing.to_dict() if existing else {}

        result = approval_store.modify(
            record_id, actor,
            new_target=new_target_column,
            new_rule=new_transformation_rule,
            reason=reason,
        )
        if result is None:
            return _err(f"No mapping found for '{record_id}'.")

        audit_logger.log(AuditRecord(
            action="modify_mapping",
            entity_type="mapping",
            entity_id=record_id,
            actor=actor,
            previous=previous_state,
            new_state={
                "target_column": new_target_column or (existing.target_column if existing else ""),
                "rule": new_transformation_rule,
                "status": "modified",
            },
            reason=reason,
        ))

        return _ok(
            record_id=record_id,
            action="modified",
            actor=actor,
            decided_at=result.decided_at,
            new_target=new_target_column,
            new_rule=new_transformation_rule,
            reason=reason,
        )
    except Exception as exc:
        return _err(f"modify_mapping failed: {exc}")


# ---------------------------------------------------------------------------
# 21. approve_rule
# ---------------------------------------------------------------------------

def approve_rule(
    rule_id: str,
    actor: str,
    reason: str = "",
) -> Dict[str, Any]:
    """
    Activate a learned rule (human approval gate).

    Only ACTIVE learned rules with a reuses_rule set affect SQL generation.
    AI cannot self-approve rules.

    Args:
        rule_id: Rule identifier in the Rule Book
        actor:   Authenticated human user
        reason:  Optional justification
    """
    if not actor or actor.lower() in ("", "gemini_ai", "ai"):
        return _err("approve_rule requires an authenticated human actor. AI cannot self-approve rules.")

    try:
        from rule_book import rule_book
        entry = rule_book.get_rule_by_id(rule_id)
        if entry is None:
            return _err(f"Rule '{rule_id}' not found.")
        if not entry.is_learned:
            return _err(f"Rule '{rule_id}' is a base rule — only learned rules need approval.")

        ok = rule_book.activate_learned_rule(rule_id)
        if not ok:
            return _err(f"Could not activate '{rule_id}'. It may have no reuses_rule set (advisory-only rules stay advisory).")

        audit_logger.log(AuditRecord(
            action="approve_rule",
            entity_type="rule",
            entity_id=rule_id,
            actor=actor,
            previous={"status": "draft"},
            new_state={"status": "active"},
            reason=reason,
        ))

        return _ok(
            rule_id=rule_id,
            action="activated",
            actor=actor,
            timestamp=_utcnow(),
            message=f"Rule '{rule_id}' is now active — it will be used as a gap filler for its type pair.",
        )
    except Exception as exc:
        return _err(f"approve_rule failed: {exc}")


# ---------------------------------------------------------------------------
# 22. approve_plan
# ---------------------------------------------------------------------------

def approve_plan(
    source_table: str,
    layer: str = "bronze",
    actor: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    """
    Mark a CanonicalValidationPlan as human-approved.

    Records the decision in the approval store and audit log.

    Args:
        source_table: The table whose plan to approve
        layer: Medallion layer
        actor: Authenticated human user
        reason: Approval rationale
    """
    if not actor or actor.lower() in ("", "gemini_ai", "ai"):
        return _err("approve_plan requires an authenticated human actor.")

    try:
        from core.plan_store import PlanStore
        store = PlanStore()
        plan = store.load_for_table(source_table, layer)
        if plan is None:
            return _err(f"No plan for '{source_table}' in layer '{layer}'.")

        plan_id = f"plan/{layer}/{source_table}"
        existing = approval_store.get(plan_id)
        if existing is None:
            approval_store.upsert(ApprovalRecord(
                id=plan_id,
                entity_type="plan",
                table=source_table,
                status=ApprovalStatus.PENDING.value,
                ai_recommendation=f"Plan status: {plan.status}, coverage: {plan.exclusion_summary()['coverage_pct']}%",
            ))

        approval_store.approve(plan_id, actor, reason)
        audit_logger.log(AuditRecord(
            action="approve_plan",
            entity_type="plan",
            entity_id=plan_id,
            actor=actor,
            previous={"status": plan.status},
            new_state={"approval_status": "approved", "approved_by": actor},
            reason=reason,
        ))

        return _ok(
            plan_id=plan_id,
            action="approved",
            actor=actor,
            timestamp=_utcnow(),
            table=source_table,
            layer=layer,
            plan_status=plan.status,
        )
    except Exception as exc:
        return _err(f"approve_plan failed: {exc}")


# ---------------------------------------------------------------------------
# 23. get_business_metrics
# ---------------------------------------------------------------------------

def get_business_metrics() -> Dict[str, Any]:
    """
    Return aggregated business value metrics for the Migration Intelligence Connector.

    Use this to demonstrate ROI: automation rate, SQL avoided, failures detected, etc.
    """
    try:
        agg = metrics_tracker.aggregate()
        approval_stats = approval_store.stats()

        return _ok(
            **agg,
            approval_stats=approval_stats,
            auto_accept_threshold=AUTO_ACCEPT_THRESHOLD,
            review_threshold=REVIEW_THRESHOLD,
            summary=(
                f"Processed {agg.get('tables_processed', 0)} tables, "
                f"{agg.get('columns_processed', 0)} columns. "
                f"Automation rate: {agg.get('automation_rate_pct', 0)}%. "
                f"Manual SQL avoided: {agg.get('manual_sql_avoided', 0)} scripts. "
                f"AI token usage: {agg.get('ai_token_usage', 0):,} tokens."
            ),
        )
    except Exception as exc:
        return _err(f"get_business_metrics failed: {exc}")


# ---------------------------------------------------------------------------
# 24. get_coverage — federated coverage query across all sources/layers
# ---------------------------------------------------------------------------

def get_coverage(
    layer: str = "bronze",
    threshold: float = 95.0,
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> Dict[str, Any]:
    """
    Return validation coverage for every table in a layer whose coverage
    falls below `threshold` (default 95%).

    Aggregates across all plans in one call — Gemini does NOT need to
    loop through tables individually.

    Args:
        layer:     Medallion layer (bronze/silver/gold)
        threshold: Coverage % below which a table is flagged (default 95)
        page:      Page number for pagination (default 1)
        page_size: Items per page (default 50, max 200)
    """
    try:
        from core.plan_store import PlanStore
        store = PlanStore()
        plan_paths = store.list_plans(layer)

        if not plan_paths:
            return _ok(
                layer=layer, threshold=threshold,
                total_tables=0, below_threshold=0, rows=[],
                message=f"No plans in layer '{layer}'.",
            )

        page_size = min(max(1, page_size), 200)
        rows: List[Dict[str, Any]] = []

        for path in plan_paths:
            try:
                plan = store.load(path)
            except Exception:
                continue
            excl = plan.exclusion_summary()
            cov = excl["coverage_pct"]
            rows.append({
                "source_system": plan.source_db_type or "unknown",
                "table":         plan.source_table,
                "target":        f"{plan.target_schema}.{plan.target_table}",
                "layer":         layer,
                "status":        plan.status,
                "coverage_pct":  cov,
                "validated":     excl["validated"],
                "excluded":      excl["excluded_count"],
                "total_columns": excl["total_source_columns"],
                "failure_count": len(plan.ambiguities) + len(plan.unmatched_source_columns),
                "last_run":      plan.generated_at,
                "below_threshold": cov < threshold,
            })

        below = [r for r in rows if r["below_threshold"]]
        page_result = _paginate(below, page, page_size)

        return _ok(
            layer=layer,
            threshold=threshold,
            total_tables=len(rows),
            below_threshold=len(below),
            **{k: v for k, v in page_result.items() if k != "items"},
            coverage_rows=page_result["items"],
        )
    except Exception as exc:
        return _err(f"get_coverage failed: {exc}")


# ---------------------------------------------------------------------------
# 24. create_jira_ticket
# ---------------------------------------------------------------------------

def create_jira_ticket(
    summary: str,
    description: str,
    table: str = "",
    labels: Optional[List[str]] = None,
    priority: str = "Medium",
) -> Dict[str, Any]:
    """
    Create a JIRA ticket for a migration issue.

    Use this when you need to escalate an issue, track a complex mapping decision,
    or create a follow-up task for the migration team.

    Args:
        summary: Brief description (becomes JIRA ticket title)
        description: Detailed explanation of the issue
        table: Optional table name (added to labels automatically)
        labels: Additional labels for the ticket
        priority: Ticket priority (Low, Medium, High, Highest)
    """
    try:
        from connector import jira_client

        if not jira_client.is_configured():
            return _ok(
                status="skipped",
                message=(
                    "JIRA is not configured. To enable ticket creation, set "
                    "JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, and JIRA_PROJECT_KEY "
                    "in your .env file. See docs/deployment/jira-integration.md for details."
                ),
            )

        # Build labels list
        ticket_labels = ["migration-validator"]
        if table:
            ticket_labels.append(table)
        if labels:
            ticket_labels.extend(labels)
        result = jira_client.create_ticket(
            summary=summary,
            description=description,
            labels=ticket_labels,
            priority=priority,
        )

        # Log audit event
        audit_logger.log(AuditRecord(
            action="create_jira_ticket",
            entity_type="jira_ticket",
            entity_id=result["key"],
            actor="system",
            previous={},
            new_state={"summary": summary, "url": result["url"]},
            reason=f"Manual ticket creation: {summary}",
        ))

        return _ok(
            jira_key=result["key"],
            jira_url=result["url"],
            message=f"JIRA ticket {result['key']} created successfully.",
            summary=summary,
            labels=ticket_labels,
        )

    except Exception as exc:
        return _err(f"create_jira_ticket failed: {exc}")


# ---------------------------------------------------------------------------
# 25. get_jira_ticket_status
# ---------------------------------------------------------------------------

def get_jira_ticket_status(
    ticket_key: str,
) -> Dict[str, Any]:
    """
    Get the current status of a JIRA ticket.

    Args:
        ticket_key: JIRA ticket key (e.g., "MIG-123")
    """
    try:
        from connector import jira_client

        if not jira_client.is_configured():
            return _err("JIRA is not configured.")

        ticket = jira_client.get_ticket(ticket_key)
        return _ok(**ticket)

    except jira_client.JiraError as exc:
        return _err(f"JIRA error: {exc}")
    except Exception as exc:
        return _err(f"get_jira_ticket_status failed: {exc}")


# ---------------------------------------------------------------------------
# Tool registry for Gemini function-calling
# ---------------------------------------------------------------------------

_RAW_TOOL_FUNCTIONS: Dict[str, Any] = {
    "discover_connections":      discover_connections,
    "list_databases":            list_databases,
    "list_schemas":              list_schemas,
    "list_tables":               list_tables,
    "get_table_schema":          get_table_schema,
    "get_table_mapping":         get_table_mapping,
    "get_column_mappings":       get_column_mappings,
    "get_pending_reviews":       get_pending_reviews,
    "get_rule":                  get_rule,
    "get_applicable_rules":      get_applicable_rules,
    "get_validation_plan":       get_validation_plan,
    "generate_validation_plan":  generate_validation_plan,
    "generate_validation_sql":   generate_validation_sql,
    "execute_validation":        execute_validation,
    "get_validation_result":     get_validation_result,
    "get_validation_failures":   get_validation_failures,
    "get_migration_summary":     get_migration_summary,
    "get_coverage":              get_coverage,
    "approve_mapping":           approve_mapping,
    "batch_approve_mappings":    batch_approve_mappings,
    "reject_mapping":            reject_mapping,
    "modify_mapping":            modify_mapping,
    "approve_rule":              approve_rule,
    "approve_plan":              approve_plan,
    "get_business_metrics":      get_business_metrics,
    "create_jira_ticket":        create_jira_ticket,
    "get_jira_ticket_status":    get_jira_ticket_status,
}

# Wrap every registered tool with the observability decorator
TOOL_FUNCTIONS: Dict[str, Any] = {
    name: _observe(fn)
    for name, fn in _RAW_TOOL_FUNCTIONS.items()
}


def dispatch_tool(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a tool call by name with the given arguments."""
    fn = TOOL_FUNCTIONS.get(tool_name)
    if fn is None:
        return _err(
            f"Unknown tool: '{tool_name}'. Available: {list(TOOL_FUNCTIONS.keys())}",
            code=ConnectorErrorCode.INVALID_REQUEST,
        )
    try:
        return fn(**arguments)
    except TypeError as exc:
        return _err(
            f"Tool '{tool_name}' called with wrong arguments: {exc}",
            code=ConnectorErrorCode.INVALID_REQUEST,
        )
    except Exception as exc:
        return _err(
            f"Tool '{tool_name}' raised an unexpected error: {exc}",
        )
