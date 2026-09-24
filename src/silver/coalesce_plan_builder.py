"""
Coalesce metadata -> CanonicalValidationPlan builder for Silver-layer
validation. Implements the extraction rules in
docs/decisions/0013-silver-layer-validation-strategy.md and
docs/decisions/0014-coalesce-metadata-extraction-rules.md.

Only entrypoint: build_plan(). Everything else here is a private helper.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from connector import coalesce_client
from core.validation_plan import CanonicalValidationPlan, ColumnMappingEntry, MatchMethod
from sql_extractor.extractors import ExtractorFactory
import validate_cli

_NON_DETERMINISTIC_TOKENS = (
    "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME",
    "RANDOM(", "UUID_STRING(", "SYSDATE(",
)

_NULL_CHECK_RULE = "null_check"


class UnsupportedNodeShapeError(Exception):
    """Node shape is not the one supported shape (single sourceMapping,
    single dependency, no overrideSQL/customSQL, not isMultisource) --
    see ADR 0014 section 6. Hard-stop rather than guess at a wrong plan."""


@dataclass
class SchemaDiff:
    """Declared-vs-live column-name diff, computed before plan generation so
    the UI can gate on it (exclude a stray column or raise a JIRA bug)."""
    only_in_metadata:    List[str] = field(default_factory=list)
    only_in_bronze_live: List[str] = field(default_factory=list)
    only_in_silver_live: List[str] = field(default_factory=list)


def _is_macro(transform: str) -> bool:
    return "{{" in (transform or "")


def _is_non_deterministic(expr: str) -> bool:
    upper = (expr or "").upper()
    return any(tok in upper for tok in _NON_DETERMINISTIC_TOKENS)


def _upstream_columns_by_id(node_id: str, workspace_id: str, cache: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    if node_id in cache:
        return cache[node_id]
    node = coalesce_client.get_node(workspace_id, node_id)
    columns = ((node.get("metadata") or {}).get("columns") or [])
    id_to_name = {c.get("columnID"): c.get("name") for c in columns if c.get("columnID")}
    cache[node_id] = id_to_name
    return id_to_name


def _check_supported_shape(metadata: Dict[str, Any]) -> None:
    if metadata.get("isMultisource"):
        raise UnsupportedNodeShapeError("isMultisource=true nodes are unsupported (ADR 0014 6).")
    if metadata.get("overrideSQL"):
        raise UnsupportedNodeShapeError("overrideSQL nodes are unsupported (ADR 0014 6).")
    if metadata.get("customSQL"):
        raise UnsupportedNodeShapeError("customSQL nodes are unsupported (ADR 0014 6).")
    source_mappings = metadata.get("sourceMapping") or []
    if len(source_mappings) != 1:
        raise UnsupportedNodeShapeError(
            "Expected exactly 1 sourceMapping entry, found %d (ADR 0014 6)." % len(source_mappings)
        )
    dependencies = source_mappings[0].get("dependencies") or []
    if len(dependencies) != 1:
        raise UnsupportedNodeShapeError(
            "Expected exactly 1 dependency in sourceMapping[0], found %d (ADR 0014 6)." % len(dependencies)
        )


def _resolve_reference(refs: List[Dict[str, Any]], alias_map: Dict[str, str],
                        workspace_id: str, upstream_cache: Dict[str, Dict[str, str]]) -> str:
    if len(refs) != 1:
        return ""
    ref = refs[0]
    upstream_node_id = ref.get("nodeID", "")
    upstream_col_id = ref.get("columnID", "")
    if not upstream_node_id or not upstream_col_id:
        return ""
    alias = upstream_node_id
    for a, nid in alias_map.items():
        if nid == upstream_node_id:
            alias = a
            break
    id_to_name = _upstream_columns_by_id(upstream_node_id, workspace_id, upstream_cache)
    upstream_col_name = id_to_name.get(upstream_col_id, "")
    if not upstream_col_name:
        return ""
    return "\"%s\".\"%s\"" % (alias, upstream_col_name)


def _classify_column(
    column: Dict[str, Any],
    alias_map: Dict[str, str],
    workspace_id: str,
    upstream_cache: Dict[str, Dict[str, str]],
) -> ColumnMappingEntry:
    name = column.get("name", "")
    sources = column.get("sources") or [{}]
    src0 = sources[0] if sources else {}
    transform = src0.get("transform", "") or ""
    refs = src0.get("columnReferences") or []

    entry_kwargs: Dict[str, Any] = dict(
        target_column=name, target_type=column.get("dataType", ""), target_normalized=name.lower(),
        source_type=column.get("dataType", ""), match_method=MatchMethod.CONFIGURED.value,
        is_primary_key=bool(column.get("isBusinessKey", False)),
    )

    resolved_ref = _resolve_reference(refs, alias_map, workspace_id, upstream_cache)

    check_expr = transform or resolved_ref
    if _is_non_deterministic(check_expr):
        return ColumnMappingEntry(
            source_column=resolved_ref or transform or name, source_normalized=name.lower(),
            skip_validation=True,
            skip_reason="non-deterministic expression (e.g. CURRENT_TIMESTAMP) -- "
                        "recomputing now can never equal the value written at ETL run time; "
                        "NOT NULL is checked instead of value equality (ADR 0014 3).",
            validation_rules=[_NULL_CHECK_RULE],
            **entry_kwargs,
        )

    if _is_macro(transform):
        return ColumnMappingEntry(
            source_column=transform, source_normalized=name.lower(),
            skip_validation=True,
            skip_reason="macro-expanded transform, not recomputable without "
                        "Coalesce macro engine (ADR 0014 3).",
            **entry_kwargs,
        )

    if not transform and len(refs) == 1 and resolved_ref:
        return ColumnMappingEntry(
            source_column=resolved_ref, source_normalized=name.lower(),
            **entry_kwargs,
        )

    if transform:
        return ColumnMappingEntry(
            source_column=transform, source_normalized=name.lower(),
            **entry_kwargs,
        )

    return ColumnMappingEntry(
        source_column=name, source_normalized=name.lower(),
        skip_validation=True,
        skip_reason="could not classify column (no transform, no single resolvable "
                    "columnReference) -- needs manual review.",
        **entry_kwargs,
    )


def write_schema_diff_exclusion(db_type: str, column: str, reason: str) -> bool:
    return validate_cli.save_global_user_exclusion(db_type, column, reason)


def _compute_schema_diff(
    metadata_columns: List[str], bronze_db: str, bronze_schema: str, bronze_table: str,
    silver_db: str, silver_schema: str, silver_table: str,
) -> SchemaDiff:
    extractor = ExtractorFactory.create("snowflake")
    bronze_live = {c.column_name.upper() for c in extractor.extract_columns(bronze_schema, bronze_table, database=bronze_db)}
    silver_live = {c.column_name.upper() for c in extractor.extract_columns(silver_schema, silver_table, database=silver_db)}
    metadata_set = {c.upper() for c in metadata_columns}
    return SchemaDiff(
        only_in_metadata=sorted(metadata_set - bronze_live - silver_live),
        only_in_bronze_live=sorted(bronze_live - metadata_set),
        only_in_silver_live=sorted(silver_live - metadata_set),
    )


def build_plan(node_id: str, workspace_id: Optional[str] = None) -> Tuple[CanonicalValidationPlan, SchemaDiff]:
    """Fetch a Coalesce node's metadata and build the CanonicalValidationPlan
    for validating its Silver table (recompute-vs-materialize, ADR 0013/0014).

    Args:
        node_id: Coalesce node UUID for the Silver table to validate.
        workspace_id: overrides COALESCE_WORKSPACE_ID when given.

    Returns:
        (plan, schema_diff) -- schema_diff compares the node's declared
        metadata.columns[] names against live Snowflake columns on both the
        Bronze and Silver tables (via sql_extractor.extractors.ExtractorFactory).

    Natural-key-candidate list (only relevant when the business key is
    macro-computed, ADR 0014 section 5): NOT a third return value. It is
    written to plan.population_scope["natural_key_candidates"] --
    CanonicalValidationPlan already has this free-form dict field for exactly
    this kind of extra derived metadata, so no new dataclass field was added
    (CLAUDE.md's no-speculative-abstraction rule). Only present when
    plan.requires_review is True for the surrogate-key reason.

    Raises:
        UnsupportedNodeShapeError: isMultisource, more than one sourceMapping
            or dependency, overrideSQL, or customSQL -- ADR 0014 section 6.
        coalesce_client.CoalesceNotConfiguredError / CoalesceError
    """
    ws = workspace_id or coalesce_client.COALESCE_WORKSPACE_ID
    node = coalesce_client.get_node(ws, node_id)
    metadata = node.get("metadata") or {}
    _check_supported_shape(metadata)

    dep = metadata["sourceMapping"][0]["dependencies"][0]
    bronze_database = dep.get("locationName", "")
    bronze_table = dep.get("nodeName", "")
    bronze_schema = node.get("schema", "")

    silver_database = node.get("database", "")
    silver_schema = node.get("schema", "")
    silver_table = node.get("name", "")

    alias_map: Dict[str, str] = metadata["sourceMapping"][0].get("aliases") or {}
    upstream_cache: Dict[str, Dict[str, str]] = {}
    columns = metadata.get("columns") or []

    mappings: List[ColumnMappingEntry] = []
    for column in columns:
        mappings.append(_classify_column(column, alias_map, ws, upstream_cache))

    plan = CanonicalValidationPlan(
        source_database=bronze_database, source_db_type="snowflake",
        source_schema=bronze_schema, source_table=bronze_table,
        target_database=silver_database, target_schema=silver_schema, target_table=silver_table,
        mappings=mappings, generated_by="coalesce_metadata",
    )

    business_key_cols = [m for m in mappings if m.is_primary_key]
    macro_business_keys = [
        m for m in business_key_cols
        if m.skip_validation and "macro-expanded transform" in m.skip_reason
    ]
    if business_key_cols and not macro_business_keys:
        key_names = [m.target_column for m in business_key_cols]
        plan.source_primary_keys = list(key_names)
        plan.target_primary_keys = list(key_names)
    elif macro_business_keys:
        plan.requires_review = True
        plan.review_reasons.append(
            "surrogate business key is macro-computed (%s); macro-argument parsing is "
            "deferred to a manual UI override for v1 (ADR 0014 5) -- confirm the correct "
            "join grain before trusting this plan."
            % ", ".join(m.target_column for m in macro_business_keys)
        )
        natural_key_candidates = [
            m.target_column for m in mappings
            if not m.skip_validation and m.source_column.startswith("\"")
        ]
        plan.population_scope["natural_key_candidates"] = natural_key_candidates

    schema_diff = _compute_schema_diff(
        [c.get("name", "") for c in columns],
        bronze_database, bronze_schema, bronze_table,
        silver_database, silver_schema, silver_table,
    )

    return plan, schema_diff
