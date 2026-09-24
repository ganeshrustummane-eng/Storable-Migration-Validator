"""Focused tests for the hybrid_v1 "front door": the config/schema surface
that lets a table opt into execution_strategy: hybrid_v1 without hand-editing
generated YAML. See docs/decisions/0010-hybrid-v1-tiered-runner-audit-no-front-door.md
and 0011-hybrid-v1-front-door.md.

This does NOT test Project/tiered_runner.py's runtime dispatch (that's
Project/test_hybrid_dispatch.py, unchanged) -- it tests the generation-time
plan/schema layer that produces the execution_strategy key in the first place.

Run:  python -m pytest src/validation/test_execution_strategy.py -q
  or: python src/validation/test_execution_strategy.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.validation_plan import CanonicalValidationPlan, RowHashSpec  # noqa: E402
from validation.plan_validator import PlanValidator  # noqa: E402
from validation.config_schema import ValidationPlanBlock  # noqa: E402
from pydantic import ValidationError  # noqa: E402


def _base_plan(**overrides) -> CanonicalValidationPlan:
    defaults = dict(
        source_database="pg", source_schema="public", source_table="t",
        target_database="sf", target_schema="public", target_table="T",
        source_primary_keys=["id"], target_primary_keys=["id"],
    )
    defaults.update(overrides)
    plan = CanonicalValidationPlan(**defaults)
    # Give it one active mapping so the unrelated "no active mappings" check
    # doesn't drown out the execution_strategy assertions below.
    from core.validation_plan import ColumnMappingEntry
    plan.mappings = [ColumnMappingEntry(
        source_column="id", source_type="int", source_normalized="id",
        target_column="id", target_type="number", target_normalized="id",
        match_method="exact", is_primary_key=True,
    )]
    return plan


def test_default_execution_strategy_is_standard():
    plan = _base_plan()
    assert plan.execution_strategy == "standard"
    result = PlanValidator().validate(plan)
    assert not any("execution_strategy" in i for i in result.issues)


def test_to_dict_from_dict_round_trips_execution_strategy():
    plan = _base_plan(execution_strategy="hybrid_v1", row_hash=RowHashSpec(columns=["id"]))
    restored = CanonicalValidationPlan.from_dict(plan.to_dict())
    assert restored.execution_strategy == "hybrid_v1"


def test_hybrid_v1_with_single_pk_and_row_hash_is_eligible():
    plan = _base_plan(execution_strategy="hybrid_v1", row_hash=RowHashSpec(columns=["id"]))
    result = PlanValidator().validate(plan)
    assert not any("execution_strategy" in i or "hybrid_v1" in i for i in result.issues)


def test_hybrid_v1_without_row_hash_is_rejected():
    plan = _base_plan(execution_strategy="hybrid_v1")
    result = PlanValidator().validate(plan)
    assert any("row_hash" in i and "hybrid_v1" in i for i in result.issues)


def test_hybrid_v1_with_composite_pk_is_rejected():
    plan = _base_plan(
        execution_strategy="hybrid_v1",
        source_primary_keys=["id", "region"],
        target_primary_keys=["id", "region"],
        row_hash=RowHashSpec(columns=["id", "region"]),
    )
    result = PlanValidator().validate(plan)
    assert any("composite" in i for i in result.issues)


def test_unsupported_execution_strategy_value_is_rejected():
    plan = _base_plan(execution_strategy="hybrid_v2")
    result = PlanValidator().validate(plan)
    assert any("Unsupported execution_strategy" in i for i in result.issues)


def test_schema_accepts_standard_and_hybrid_v1():
    assert ValidationPlanBlock(execution_strategy="standard").execution_strategy == "standard"
    assert ValidationPlanBlock(execution_strategy="hybrid_v1").execution_strategy == "hybrid_v1"
    assert ValidationPlanBlock().execution_strategy == "standard"


def test_schema_rejects_typo_instead_of_silently_ignoring_it():
    try:
        ValidationPlanBlock(execution_strategy="Hybrid_V1")
        raised = False
    except ValidationError:
        raised = True
    assert raised, "a typo'd execution_strategy must fail config validation, not silently no-op"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
