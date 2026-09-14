"""Natural-language requirement to metadata-checked canonical plan intent.

AI returns JSON intent. This module validates names and relationships against
provided schema metadata before attaching intent to CanonicalValidationPlan.
It never accepts or executes AI-generated SQL.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from .validation_plan import (
    CanonicalValidationPlan,
    RelationshipSpec,
    RowHashSpec,
    TransformationCheck,
    ValidationSpec,
)


class RequirementPlanError(ValueError):
    """Raised when AI intent cannot be proven against schema metadata."""


def build_planner_prompt(requirement: str, schema_metadata: Mapping[str, Any]) -> str:
    """Build constrained prompt for an AI plan response.

    ``schema_metadata`` must contain authoritative table/column/key metadata.
    The AI response must be JSON intent, never SQL.
    """
    return (
        "Convert migration validation requirement to JSON intent. Return JSON only. "
        "Do not return SQL. Use only tables, columns, and relationships in metadata.\n"
        "Required keys: population_scope, relationships, identity, row_hash, "
        "transformations, validations, requires_review, review_reasons.\n"
        f"Requirement:\n{requirement.strip()}\n"
        f"Schema metadata:\n{json.dumps(schema_metadata, default=str, sort_keys=True)}"
    )


def build_plan_from_requirement(
    base_plan: CanonicalValidationPlan,
    requirement: str,
    schema_metadata: Mapping[str, Any],
    ai_json: Callable[[str], str],
) -> CanonicalValidationPlan:
    """Apply AI JSON intent to plan after metadata and safety validation."""
    raw = ai_json(build_planner_prompt(requirement, schema_metadata))
    intent = _parse_json_object(raw)
    metadata = dict(schema_metadata)
    metadata.setdefault("source_table", base_plan.source_table)
    metadata.setdefault("target_table", base_plan.target_table)
    _validate_intent(intent, metadata)
    _apply_intent(base_plan, intent)
    base_plan.generated_by = "ai"
    base_plan.ai_calls_made += 1
    return base_plan


def _parse_json_object(raw: str) -> Dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RequirementPlanError(f"AI plan response is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RequirementPlanError("AI plan response must be a JSON object")
    return value


def _tables(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    value = metadata.get("tables", metadata)
    return value if isinstance(value, Mapping) else {}


def _columns(table_data: Any) -> set[str]:
    if isinstance(table_data, Mapping):
        value = table_data.get("columns", [])
    else:
        value = table_data
    if isinstance(value, Mapping):
        return {str(name).lower() for name in value}
    return {str(name).lower() for name in value} if isinstance(value, Iterable) and not isinstance(value, str) else set()


def _validate_intent(intent: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
    tables = _tables(metadata)
    for relationship in intent.get("relationships", []) or []:
        left = str(relationship.get("left_table", ""))
        right = str(relationship.get("right_table", ""))
        if left not in tables or right not in tables:
            raise RequirementPlanError(f"Unknown relationship table: {left}->{right}")
        left_columns = relationship.get("left_columns", [])
        right_columns = relationship.get("right_columns", [])
        if len(left_columns) != len(right_columns) or not left_columns:
            raise RequirementPlanError(f"Invalid relationship keys: {left}->{right}")
        if not set(map(str.lower, left_columns)) <= _columns(tables[left]):
            raise RequirementPlanError(f"Unknown columns in relationship left table: {left}")
        if not set(map(str.lower, right_columns)) <= _columns(tables[right]):
            raise RequirementPlanError(f"Unknown columns in relationship right table: {right}")
        if relationship.get("cardinality", "unknown") in {"one_to_many", "many_to_many", "unknown"} and relationship.get("purpose") == "comparison":
            raise RequirementPlanError(f"Unsafe comparison fan-out requires review: {left}->{right}")

    identity = intent.get("identity") or {}
    for key in identity.get("source_primary_keys", []) or []:
        if key.lower() not in _columns(tables.get(metadata.get("source_table", ""), {})):
            raise RequirementPlanError(f"Unknown source identity column: {key}")

    for transform in intent.get("transformations", []) or []:
        if not transform.get("source_expression") or not transform.get("target_expression"):
            raise RequirementPlanError("Transformation requires source_expression and target_expression")


def _apply_intent(plan: CanonicalValidationPlan, intent: Mapping[str, Any]) -> None:
    plan.population_scope = dict(intent.get("population_scope") or {})
    plan.relationships = [RelationshipSpec.from_dict(item) for item in intent.get("relationships", []) or []]
    identity = intent.get("identity") or {}
    plan.identity_type = identity.get("type", plan.identity_type)
    plan.candidate_keys = [list(key) for key in identity.get("candidate_keys", []) or []]
    plan.row_hash = RowHashSpec.from_dict(intent.get("row_hash"))
    plan.transformations = [TransformationCheck.from_dict(item) for item in intent.get("transformations", []) or []]
    plan.validations = [ValidationSpec.from_dict(item) for item in intent.get("validations", []) or []]
    plan.requires_review = bool(intent.get("requires_review", False))
    plan.review_reasons = list(intent.get("review_reasons", []) or [])
