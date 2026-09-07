"""
Redshift → Snowflake Rules
============================
Redshift extractors normalize types to PostgreSQL-compatible names before
rule matching, so the same rule set applies. Re-exports from postgres_base_rules.
"""
from rules.postgres_base_rules import (
    BaseValidationRule, RuleRegistry, NULL_PLACEHOLDER,
    BooleanRule, IntegerRule, NumericRule,
    TimestampTZRule, TimestampNTZRule, DateRule,
    TextRule, UUIDRule, JSONRule, ByteaRule, HStoreRule,
    NullPlaceholderRule, DEFAULT_DECIMAL_PLACES,
)

__all__ = [
    "BaseValidationRule", "RuleRegistry", "NULL_PLACEHOLDER",
    "BooleanRule", "IntegerRule", "NumericRule",
    "TimestampTZRule", "TimestampNTZRule", "DateRule",
    "TextRule", "UUIDRule", "JSONRule", "ByteaRule",
    "HStoreRule", "NullPlaceholderRule", "DEFAULT_DECIMAL_PLACES",
]
