import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import coalesce_client  # noqa: E402
from silver.coalesce_plan_builder import (  # noqa: E402
    build_plan, UnsupportedNodeShapeError,
)

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

NODES_BY_ID = {UPSTREAM_NODE_ID: UPSTREAM_NODE, SILVER_NODE_ID: SILVER_NODE}


class _FakeExtractor:
    def extract_columns(self, schema, table, database=None):
        return []


def _patch_env(monkeypatch=None):
    coalesce_client.COALESCE_API_TOKEN = "fake-token"
    coalesce_client.COALESCE_WORKSPACE_ID = "fake-ws"


def _run_build_plan():
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
        return build_plan(SILVER_NODE_ID, workspace_id="fake-ws")
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
