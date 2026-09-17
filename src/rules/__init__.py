"""
Rules Package — Source → Snowflake Validation Transformation Rules
===================================================================
All rule logic lives in base_rules.py (the canonical implementation).
Every source system's extractor (MSSQL, Athena, Redshift, ...) normalizes its
native type names to this shared vocabulary before rule lookup — there is no
per-database rule file. (Earlier per-DB files — mssql_rules.py, athena_rules.py,
snowflake_rules.py, redshift_rules.py — were never wired up and were moved to
trash/src/rules/; see CLAUDE.md for why.)

Usage:
    from rules import get_rule_for_type, RuleRegistry
    rule = get_rule_for_type("boolean", "boolean")
    pg_expr  = rule.apply_postgresql("is_active")
    sf_expr  = rule.apply_snowflake("IS_ACTIVE")
"""

from .base_rules import (
    BaseValidationRule, RuleRegistry, NULL_PLACEHOLDER,
    BooleanRule, NumericRule, TimestampTZRule, TimestampNTZRule,
    DateRule, TextRule, UUIDRule, IntegerRule, JSONRule,
    ByteaRule, HStoreRule, ArrayRule, NullPlaceholderRule,
)

# ── Global registry (registration ORDER matters — TextRule wildcard must be LAST) ──
_registry = RuleRegistry()
_registry.register(BooleanRule())
_registry.register(NumericRule())
_registry.register(TimestampTZRule())   # TZ before NTZ (more specific)
_registry.register(TimestampNTZRule())
_registry.register(DateRule())
_registry.register(UUIDRule())
_registry.register(IntegerRule())
_registry.register(JSONRule())
_registry.register(ByteaRule())
_registry.register(HStoreRule())
_registry.register(ArrayRule())          # before TextRule, else ARRAY gets TRIM()
_registry.register(NullPlaceholderRule())
_registry.register(TextRule())          # MUST be last — wildcard ('*', '*') catch-all


def get_rule_for_type(pg_type: str, sf_type: str) -> BaseValidationRule:
    """Look up the correct validation rule for a source→Snowflake type pair."""
    return _registry.lookup(pg_type, sf_type)


def get_rule_for_type_specific(pg_type: str, sf_type: str):
    """
    Same as get_rule_for_type(), but returns None instead of falling back to
    the TextRule wildcard. Used by RuleBook as the "did a BASE rule already
    own this pair" check before ever consulting learned rules — see
    RuleRegistry.lookup_specific() for why this is the safety seam.
    """
    return _registry.lookup_specific(pg_type, sf_type)


def get_rule_by_name(rule_name: str):
    """Look up a registered base rule by its rule_name (e.g. 'text', 'boolean')."""
    return _registry.get_by_name(rule_name)


def get_registry() -> RuleRegistry:
    """Return the global rule registry for introspection or extension."""
    return _registry


__all__ = [
    "BaseValidationRule", "RuleRegistry", "NULL_PLACEHOLDER",
    "BooleanRule", "NumericRule", "TimestampNTZRule", "TimestampTZRule",
    "DateRule", "TextRule", "UUIDRule", "IntegerRule", "JSONRule",
    "ByteaRule", "HStoreRule", "ArrayRule", "NullPlaceholderRule",
    "get_rule_for_type", "get_rule_for_type_specific", "get_rule_by_name", "get_registry",
]
