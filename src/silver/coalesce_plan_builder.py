"""
Coalesce metadata -> CanonicalValidationPlan builder for Silver-layer
validation. Implements the extraction rules in
docs/decisions/0013-silver-layer-validation-strategy.md,
docs/decisions/0014-coalesce-metadata-extraction-rules.md, and the
multisource-node support in docs/decisions/0019-silver-multisource-nodes-and-batch-node-ui.md.

Only entrypoint: build_plan(). Everything else here is a private helper.
SQL emission (verbatim transforms, no base_rules.py wrapper -- ADR 0018)
lives in silver.silver_sql_emitter, not here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from connector import coalesce_client
from core.validation_plan import CanonicalValidationPlan, ColumnMappingEntry, MatchMethod
from silver.silver_sql_emitter import resolve_ref_macro
from sql_extractor.extractors import ExtractorFactory
import validate_cli

_NON_DETERMINISTIC_TOKENS = (
    "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME",
    "RANDOM(", "UUID_STRING(", "SYSDATE(",
)

_NULL_CHECK_RULE = "null_check"


class UnsupportedNodeShapeError(Exception):
    """Node shape has no declared column-level metadata to classify against
    (overrideSQL/customSQL) or no dependencies at all -- see ADR 0014 section
    6 and ADR 0019 (which dropped the isMultisource / single-sourceMapping /
    single-dependency hard-stops that used to also raise this). Hard-stop
    rather than guess at a wrong plan."""


@dataclass
class SchemaDiff:
    """Declared-vs-live column-name diff, computed before plan generation so
    the UI can gate on it (exclude a stray column or raise a JIRA bug).

    unavailable_reason (ADR 0021): set instead of raising when the live
    Snowflake schema-diff query itself fails (e.g. no grant on the Bronze
    database yet) -- the three lists stay empty (nothing was actually
    compared) and the UI must show this as "not checked", not as "no drift
    found"."""
    only_in_metadata:    List[str] = field(default_factory=list)
    only_in_bronze_live: List[str] = field(default_factory=list)
    only_in_silver_live: List[str] = field(default_factory=list)
    unavailable_reason: Optional[str] = None


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
    """overrideSQL/customSQL are still a hard-stop (no declared column-level
    metadata to classify against). isMultisource / multiple sourceMapping
    entries / multiple dependencies per entry are now supported (ADR 0019 1) --
    only "no sourceMapping at all" or "a sourceMapping entry with zero
    dependencies" remain unsupported, since there's nothing to build a
    Bronze reference from."""
    if metadata.get("overrideSQL"):
        raise UnsupportedNodeShapeError("overrideSQL nodes are unsupported (ADR 0014 6).")
    if metadata.get("customSQL"):
        raise UnsupportedNodeShapeError("customSQL nodes are unsupported (ADR 0014 6).")
    source_mappings = metadata.get("sourceMapping") or []
    if not source_mappings:
        raise UnsupportedNodeShapeError("Expected at least 1 sourceMapping entry, found 0 (ADR 0014 6).")
    for i, sm in enumerate(source_mappings):
        if not (sm.get("dependencies") or []):
            raise UnsupportedNodeShapeError(
                "Expected at least 1 dependency in sourceMapping[%d], found 0 (ADR 0014 6)." % i
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
    metadata_columns: List[str], bronze_tables: List[Tuple[str, str, str]],
    silver_db: str, silver_schema: str, silver_table: str,
) -> SchemaDiff:
    """bronze_tables is (database, schema, table) per dependency -- unioned
    so a multisource node's diff checks against every Bronze table's live
    columns, not just the first one (ADR 0019 5)."""
    extractor = ExtractorFactory.create("snowflake")
    bronze_live: set = set()
    for bronze_db, bronze_schema, bronze_table in bronze_tables:
        bronze_live |= {
            c.column_name.upper()
            for c in extractor.extract_columns(bronze_schema, bronze_table, database=bronze_db)
        }
    silver_live = {c.column_name.upper() for c in extractor.extract_columns(silver_schema, silver_table, database=silver_db)}
    metadata_set = {c.upper() for c in metadata_columns}
    return SchemaDiff(
        only_in_metadata=sorted(metadata_set - bronze_live - silver_live),
        only_in_bronze_live=sorted(bronze_live - metadata_set),
        only_in_silver_live=sorted(silver_live - metadata_set),
    )


def build_plan(node_id: str, workspace_id: Optional[str] = None) -> Tuple[CanonicalValidationPlan, SchemaDiff]:
    """Fetch a Coalesce node's metadata (live API call) and build its plan.
    Thin wrapper around build_plan_from_metadata() -- see that function for
    the actual extraction logic and full docstring. Use
    build_plan_from_metadata() directly when the node JSON was already
    obtained some other way (ADR 0021 -- pasted metadata, no Coalesce API
    round-trip needed).

    Args:
        node_id: Coalesce node UUID for the Silver table to validate.
        workspace_id: overrides COALESCE_WORKSPACE_ID when given.

    Raises:
        UnsupportedNodeShapeError, coalesce_client.CoalesceNotConfiguredError,
        coalesce_client.CoalesceError -- see build_plan_from_metadata().
    """
    ws = workspace_id or coalesce_client.COALESCE_WORKSPACE_ID
    node = coalesce_client.get_node(ws, node_id)
    return build_plan_from_metadata(node, workspace_id=ws)


def build_plan_from_metadata(node: Dict[str, Any], workspace_id: Optional[str] = None) -> Tuple[CanonicalValidationPlan, SchemaDiff]:
    """Build the CanonicalValidationPlan for validating a Silver table
    (recompute-vs-materialize, ADR 0013/0014) from an already-fetched Coalesce
    node metadata dict -- the same shape coalesce_client.get_node() returns.

    This is build_plan()'s real logic, factored out (ADR 0021) so a caller
    who already has the node JSON (e.g. pasted from a manual curl call,
    because they don't yet have Snowflake access to run the live fetch/diff)
    can skip the Coalesce API round-trip entirely. build_plan() is now a
    2-line wrapper: fetch, then call this.

    Args:
        node: full node dict (must have "metadata", "database", "schema",
            "name" keys -- same shape as coalesce_client.get_node()'s return).
        workspace_id: used only to resolve cross-node columnReferences (calls
            coalesce_client.get_node() again for the referenced upstream
            node) -- overrides COALESCE_WORKSPACE_ID when given. If the
            pasted node has no columnReferences into other nodes, this is
            never actually used.

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

    Multisource nodes (isMultisource=true, or more than one sourceMapping
    entry, or more than one dependency per entry) are supported (ADR 0019) --
    the Bronze-side join SQL is Coalesce's own joinCondition strings
    concatenated verbatim (macro-resolved) into
    plan.population_scope["bronze_join_sql"], read by
    silver.silver_sql_emitter's Bronze query builder instead of the
    single-table FROM clause used for the common single-source case.

    schema_diff is never raised for a Snowflake/Coalesce failure -- a failed
    live diff query (e.g. no grant on the Bronze database) is caught and
    reported as SchemaDiff.unavailable_reason instead (ADR 0021), so plan/SQL/
    YAML generation can still proceed without live schema verification.

    Raises:
        UnsupportedNodeShapeError: overrideSQL, customSQL, no sourceMapping
            entries, or a sourceMapping entry with no dependencies --
            ADR 0014 section 6 / ADR 0019 1.
        coalesce_client.CoalesceNotConfiguredError / CoalesceError: only from
            resolving a cross-node columnReference, not from the schema diff.
    """
    ws = workspace_id or coalesce_client.COALESCE_WORKSPACE_ID
    metadata = node.get("metadata") or {}
    _check_supported_shape(metadata)

    source_mappings = metadata["sourceMapping"]
    all_deps: List[Dict[str, Any]] = [
        dep for sm in source_mappings for dep in (sm.get("dependencies") or [])
    ]
    is_single_source = len(source_mappings) == 1 and len(all_deps) == 1

    # First dependency's identity is used for the plan's own source_database/
    # source_table fields (display + YAML file naming) even for multisource
    # nodes -- the real multi-table FROM/JOIN lives in
    # population_scope["bronze_join_sql"] below, this is just "which name do
    # we call this validation run," ADR 0019 doesn't ask for anything fancier.
    first_dep = all_deps[0]
    bronze_database = first_dep.get("locationName", "")
    bronze_table = first_dep.get("nodeName", "")
    bronze_schema = node.get("schema", "")

    silver_database = node.get("database", "")
    silver_schema = node.get("schema", "")
    silver_table = node.get("name", "")

    alias_map: Dict[str, str] = {}
    for sm in source_mappings:
        alias_map.update(sm.get("aliases") or {})
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

    if not is_single_source:
        # Multisource: concatenate each sourceMapping[].join.joinCondition
        # VERBATIM (no RelationshipSpec, no ON-clause parsing -- ADR 0019 3),
        # resolving only the {{ ref(...) }} macro. silver_sql_emitter.py's
        # Bronze SQL builder reads this key instead of the single-table
        # FROM clause it falls back to otherwise (ADR 0019 4).
        join_fragments = [
            resolve_ref_macro((sm.get("join") or {}).get("joinCondition") or "", bronze_schema)
            for sm in source_mappings
        ]
        plan.population_scope["bronze_join_sql"] = "\n".join(f for f in join_fragments if f)

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

    # Every macro-computed column is left out of the emitted SELECT entirely
    # (ADR 0022 -- no Coalesce macro engine to resolve `{{ ... }}` into real
    # SQL yet, and inlining it unresolved breaks the whole query). Business
    # keys among them are already called out above; list the rest here too,
    # so the exclusion is visible in the YAML's review_reasons instead of
    # only in each mapping's skip_reason (which isn't rendered into the YAML).
    other_macro_skips = [
        m for m in mappings
        if m.skip_validation and "macro-expanded transform" in m.skip_reason
        and m not in macro_business_keys
    ]
    if other_macro_skips:
        plan.requires_review = True
        plan.review_reasons.append(
            "%d column(s) excluded from the SELECT pending Coalesce macro resolution "
            "(no macro engine yet -- ADR 0014 3, ADR 0022): %s"
            % (len(other_macro_skips), ", ".join(m.target_column for m in other_macro_skips))
        )

    bronze_tables = [
        (dep.get("locationName", ""), bronze_schema, dep.get("nodeName", ""))
        for dep in all_deps
    ]
    try:
        schema_diff = _compute_schema_diff(
            [c.get("name", "") for c in columns],
            bronze_tables,
            silver_database, silver_schema, silver_table,
        )
    except Exception as exc:
        # Live schema-diff verification is optional (ADR 0021) -- a Snowflake
        # auth/access failure (e.g. no grant yet on the Bronze database named
        # in the node metadata) must not abort plan/SQL/YAML generation. Still
        # surfaced explicitly via unavailable_reason so the UI never confuses
        # "not checked" with "no drift found".
        schema_diff = SchemaDiff(unavailable_reason=str(exc))

    return plan, schema_diff
