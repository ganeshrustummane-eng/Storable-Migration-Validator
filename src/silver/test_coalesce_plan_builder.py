import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import coalesce_client  # noqa: E402
from silver.coalesce_plan_builder import (  # noqa: E402
    build_plan, build_plan_from_metadata, UnsupportedNodeShapeError,
)
from silver.silver_sql_emitter import emit_query_set, resolve_ref_macro  # noqa: E402

UPSTREAM_NODE_ID = "323522c8-a433-4859-bb89-4ddcb48298f7"
SILVER_NODE_ID = "int-facilities-node-id"

UPSTREAM_NODE = {
    "database": "BRONZE_EDGE",
    "name": "FACILITIES",
    "metadata": {
        "columns": [
            {"columnID": "col-facility-id", "name": "ID"},
            {"columnID": "col-updated-at", "name": "UPDATED_AT"},
            {"columnID": "col-fivetran-start", "name": "_FIVETRAN_START"},
        ]
    },
}

SILVER_NODE = {
    "database": "SILVER_EDGE",
    "schema": "CONFORMED_RRADHAKR",
    "name": "INT_FACILITIES",
    "metadata": {
        "isMultisource": False,
        "overrideSQL": False,
        "customSQL": False,
        "sourceMapping": [
            {
                "aliases": {"FACILITIES": UPSTREAM_NODE_ID},
                "dependencies": [
                    {"nodeID": UPSTREAM_NODE_ID, "nodeName": "FACILITIES", "locationName": "BRONZE_EDGE"}
                ],
            }
        ],
        "columns": [
            {
                "name": "FACILITY_ID", "dataType": "NUMBER", "isBusinessKey": False,
                "sources": [{"transform": "", "columnReferences": [
                    {"nodeID": UPSTREAM_NODE_ID, "columnID": "col-facility-id"}
                ]}],
            },
            {
                "name": "SYS_VERSION", "dataType": "NUMBER", "isBusinessKey": False,
                "sources": [{
                    "transform": 'ROW_NUMBER() OVER (PARTITION BY "FACILITIES"."ID" ORDER BY "FACILITIES"."UPDATED_AT")',
                    "columnReferences": [{"nodeID": UPSTREAM_NODE_ID, "columnID": "col-updated-at"}],
                }],
            },
            {
                "name": "FACILITY_KEY", "dataType": "VARCHAR", "isBusinessKey": True,
                "sources": [{
                    "transform": "{{ ids_to_surrogate_key('EDGE', ['\"FACILITIES\".\"ID\"']) }}",
                    "columnReferences": [{"nodeID": UPSTREAM_NODE_ID, "columnID": "col-facility-id"}],
                }],
            },
            {
                "name": "SYS_CREATE_DATE", "dataType": "TIMESTAMP_NTZ", "isBusinessKey": False,
                "sources": [{"transform": "CAST(CURRENT_TIMESTAMP() AS TIMESTAMP)", "columnReferences": []}],
            },
        ],
    },
}

UPSTREAM_NODE_ID_2 = "companies-node-id"
MULTI_NODE_ID = "int-multi-node-id"

UPSTREAM_NODE_2 = {
    "database": "BRONZE_EDGE",
    "name": "COMPANIES",
    "metadata": {
        "columns": [
            {"columnID": "col-company-name", "name": "NAME"},
        ]
    },
}

# Multisource node -- two sourceMapping entries, each with its own alias and
# joinCondition, per ADR 0019 (the isMultisource / multi-dependency shape).
MULTI_NODE = {
    "database": "SILVER_EDGE",
    "schema": "CONFORMED_RRADHAKR",
    "name": "INT_MULTI",
    "metadata": {
        "isMultisource": True,
        "overrideSQL": False,
        "customSQL": False,
        "sourceMapping": [
            {
                "aliases": {"FACILITIES": UPSTREAM_NODE_ID},
                "dependencies": [
                    {"nodeID": UPSTREAM_NODE_ID, "nodeName": "FACILITIES", "locationName": "BRONZE_EDGE"}
                ],
                "join": {"joinCondition": "FROM {{ ref('BRONZE_EDGE', 'FACILITIES') }} \"FACILITIES\""},
            },
            {
                "aliases": {"COMPANIES": UPSTREAM_NODE_ID_2},
                "dependencies": [
                    {"nodeID": UPSTREAM_NODE_ID_2, "nodeName": "COMPANIES", "locationName": "BRONZE_EDGE"}
                ],
                "join": {
                    "joinCondition": (
                        "JOIN {{ ref('BRONZE_EDGE', 'COMPANIES') }} \"COMPANIES\" "
                        "ON \"FACILITIES\".\"COMPANY_ID\" = \"COMPANIES\".\"ID\""
                    )
                },
            },
        ],
        "columns": [
            {
                "name": "FACILITY_ID", "dataType": "NUMBER", "isBusinessKey": False,
                "sources": [{"transform": "", "columnReferences": [
                    {"nodeID": UPSTREAM_NODE_ID, "columnID": "col-facility-id"}
                ]}],
            },
            {
                "name": "COMPANY_NAME", "dataType": "VARCHAR", "isBusinessKey": False,
                "sources": [{"transform": "", "columnReferences": [
                    {"nodeID": UPSTREAM_NODE_ID_2, "columnID": "col-company-name"}
                ]}],
            },
        ],
    },
}

BAD_OVERRIDE_SQL_NODE = {
    "database": "SILVER_EDGE", "schema": "S", "name": "T",
    "metadata": {"isMultisource": False, "overrideSQL": True, "sourceMapping": []},
}

NODES_BY_ID = {
    UPSTREAM_NODE_ID: UPSTREAM_NODE,
    SILVER_NODE_ID: SILVER_NODE,
    UPSTREAM_NODE_ID_2: UPSTREAM_NODE_2,
    MULTI_NODE_ID: MULTI_NODE,
}


class _FakeExtractor:
    def extract_columns(self, schema, table, database=None):
        return []


def _patch_env(monkeypatch=None):
    coalesce_client.COALESCE_API_TOKEN = "fake-token"
    coalesce_client.COALESCE_WORKSPACE_ID = "fake-ws"


def _run_build_plan(node_id=SILVER_NODE_ID):
    _patch_env()
    orig_get_node = coalesce_client.get_node
    orig_create = __import__("sql_extractor.extractors", fromlist=["ExtractorFactory"]).ExtractorFactory.create

    def fake_get_node(workspace_id, node_id):
        return NODES_BY_ID[node_id]

    def fake_create(db_type, **kwargs):
        return _FakeExtractor()

    import sql_extractor.extractors as extractors_mod

    coalesce_client.get_node = fake_get_node
    extractors_mod.ExtractorFactory.create = staticmethod(fake_create)
    try:
        return build_plan(node_id, workspace_id="fake-ws")
    finally:
        coalesce_client.get_node = orig_get_node
        extractors_mod.ExtractorFactory.create = staticmethod(orig_create)


def test_four_bucket_classification():
    plan, diff = _run_build_plan()
    by_name = {m.target_column: m for m in plan.mappings}

    passthrough = by_name["FACILITY_ID"]
    assert not passthrough.skip_validation
    assert passthrough.source_column == '"FACILITIES"."ID"'

    recomputable = by_name["SYS_VERSION"]
    assert not recomputable.skip_validation
    assert "ROW_NUMBER()" in recomputable.source_column

    macro = by_name["FACILITY_KEY"]
    assert macro.skip_validation
    assert "macro-expanded" in macro.skip_reason

    nondeterministic = by_name["SYS_CREATE_DATE"]
    assert nondeterministic.skip_validation
    assert "null_check" in nondeterministic.validation_rules


def test_macro_business_key_defers_to_review_with_natural_key_candidates():
    plan, diff = _run_build_plan()
    assert plan.requires_review
    assert any("macro-computed" in r for r in plan.review_reasons)
    assert plan.source_primary_keys == []
    assert plan.target_primary_keys == []
    assert "FACILITY_ID" in plan.population_scope["natural_key_candidates"]


def test_unsupported_node_shape_raises():
    """Empty sourceMapping ([]) is still a hard-stop -- there's nothing to
    build a Bronze reference from. isMultisource=True alone (ADR 0019) no
    longer causes this by itself; see test_multisource_node_builds_plan."""
    _patch_env()
    bad_node = {
        "database": "SILVER_EDGE", "schema": "S", "name": "T",
        "metadata": {"isMultisource": True, "sourceMapping": []},
    }
    orig_get_node = coalesce_client.get_node
    coalesce_client.get_node = lambda workspace_id, node_id: bad_node
    try:
        raised = False
        try:
            build_plan("bad-node", workspace_id="fake-ws")
        except UnsupportedNodeShapeError:
            raised = True
        assert raised
    finally:
        coalesce_client.get_node = orig_get_node


def test_overridesql_still_hardstops():
    _patch_env()
    orig_get_node = coalesce_client.get_node
    coalesce_client.get_node = lambda workspace_id, node_id: BAD_OVERRIDE_SQL_NODE
    try:
        raised = False
        try:
            build_plan("bad-override-node", workspace_id="fake-ws")
        except UnsupportedNodeShapeError:
            raised = True
        assert raised
    finally:
        coalesce_client.get_node = orig_get_node


def test_multisource_node_builds_plan_and_join_sql():
    """ADR 0019: isMultisource / multiple sourceMapping entries no longer
    hard-stop. Alias resolution must work across every sourceMapping entry
    (not just entry 0), and the Bronze join SQL must be the concatenation of
    each entry's joinCondition, verbatim, with {{ ref(...) }} resolved."""
    plan, diff = _run_build_plan(MULTI_NODE_ID)
    by_name = {m.target_column: m for m in plan.mappings}

    assert by_name["FACILITY_ID"].source_column == '"FACILITIES"."ID"'
    assert by_name["COMPANY_NAME"].source_column == '"COMPANIES"."NAME"'

    join_sql = plan.population_scope["bronze_join_sql"]
    assert 'FROM "BRONZE_EDGE"."CONFORMED_RRADHAKR"."FACILITIES" "FACILITIES"' in join_sql
    assert (
        'JOIN "BRONZE_EDGE"."CONFORMED_RRADHAKR"."COMPANIES" "COMPANIES" '
        'ON "FACILITIES"."COMPANY_ID" = "COMPANIES"."ID"'
    ) in join_sql


def test_build_plan_from_pasted_metadata_no_api_fetch():
    """ADR 0021: pasted node JSON builds the same plan as build_plan(), with
    no coalesce_client.get_node() call for the Silver node itself (only used,
    if at all, to resolve a cross-node columnReference)."""
    _patch_env()
    orig_get_node = coalesce_client.get_node
    coalesce_client.get_node = lambda workspace_id, node_id: NODES_BY_ID[node_id]
    try:
        plan, diff = build_plan_from_metadata(SILVER_NODE, workspace_id="fake-ws")
    finally:
        coalesce_client.get_node = orig_get_node
    by_name = {m.target_column: m for m in plan.mappings}
    assert by_name["FACILITY_ID"].source_column == '"FACILITIES"."ID"'


def test_schema_diff_unavailable_does_not_abort_plan():
    """ADR 0021: a live Snowflake schema-diff failure (e.g. no grant on the
    Bronze database) degrades to SchemaDiff.unavailable_reason instead of
    raising -- plan generation still succeeds."""
    _patch_env()
    orig_create = __import__("sql_extractor.extractors", fromlist=["ExtractorFactory"]).ExtractorFactory.create

    class _BoomExtractor:
        def extract_columns(self, schema, table, database=None):
            raise Exception("002003 (02000): Database 'BRONZE_EDGE' does not exist or not authorized.")

    import sql_extractor.extractors as extractors_mod
    orig_get_node = coalesce_client.get_node
    extractors_mod.ExtractorFactory.create = staticmethod(lambda db_type, **kwargs: _BoomExtractor())
    coalesce_client.get_node = lambda workspace_id, node_id: NODES_BY_ID[node_id]
    try:
        plan, diff = build_plan_from_metadata(SILVER_NODE, workspace_id="fake-ws")
    finally:
        extractors_mod.ExtractorFactory.create = staticmethod(orig_create)
        coalesce_client.get_node = orig_get_node

    assert diff.unavailable_reason is not None
    assert "BRONZE_EDGE" in diff.unavailable_reason
    assert diff.only_in_metadata == []
    assert diff.only_in_bronze_live == []
    assert diff.only_in_silver_live == []
    # Plan itself still built successfully despite the diff failure.
    assert plan.mappings


def test_resolve_ref_macro():
    resolved = resolve_ref_macro(
        "FROM {{ ref('BRONZE_EDGE', 'FACILITIES') }} \"FACILITIES\"", "CONFORMED_RRADHAKR"
    )
    assert resolved == 'FROM "BRONZE_EDGE"."CONFORMED_RRADHAKR"."FACILITIES" "FACILITIES"'


def test_sql_emitter_literal_text():
    """ADR 0018 + ADR 0022: literal generated-SQL assertion, not just
    plan-object shape -- passthrough/recomputable columns selected verbatim;
    macro-computed and non-deterministic columns both dropped entirely (no
    inline comment -- their exclusion is recorded in review_reasons /
    skip_reason instead, not in the SQL text)."""
    plan, diff = _run_build_plan()
    query_set = emit_query_set(plan)

    expected_source = (
        'SELECT\n'
        '    "FACILITIES"."ID" AS "FACILITY_ID",\n'
        '    ROW_NUMBER() OVER (PARTITION BY "FACILITIES"."ID" ORDER BY "FACILITIES"."UPDATED_AT") AS "SYS_VERSION"\n'
        'FROM "BRONZE_EDGE"."CONFORMED_RRADHAKR"."FACILITIES"'
    )
    expected_target = (
        'SELECT\n'
        '    "FACILITY_ID",\n'
        '    "SYS_VERSION"\n'
        'FROM "SILVER_EDGE"."CONFORMED_RRADHAKR"."INT_FACILITIES"'
    )

    assert query_set.main_validation_source == expected_source
    assert query_set.main_validation_target == expected_target
    # No blanket COALESCE/CAST/NULL-placeholder wrapper anywhere (ADR 0018 2).
    assert "COALESCE" not in query_set.main_validation_source
    assert "<<NULL>>" not in query_set.main_validation_source
    assert "SYS_CREATE_DATE" not in query_set.main_validation_source


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS  %s" % name)
            except AssertionError as exc:
                failures += 1
                print("FAIL  %s: %s" % (name, exc))
    print("\n%d failure(s)" % failures)
    sys.exit(1 if failures else 0)
