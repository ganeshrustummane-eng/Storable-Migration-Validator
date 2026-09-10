"""
Canonical Validation Plan
==========================
The CanonicalValidationPlan is the SINGLE SOURCE OF TRUTH for validation.

Both the SQL generator and the YAML generator consume this plan.
They never receive inputs from different sources — everything flows from
the same plan object. This guarantees SQL and YAML are always in sync.

Architecture flow:
  metadata (PG + SF)
    → matching pipeline
    → AI (for ambiguous only)
    → CanonicalValidationPlan   ← this module
    → plan_validator            validates the plan
    → sql_generator             deterministic SQL from plan
    → yaml_generator            deterministic YAML from plan

Design principles:
  - Plain dataclasses (no Pydantic dependency)
  - Immutable-friendly (no mutation after construction)
  - Serializable to dict/JSON for storage and display
  - Explicit about what was resolved vs what is ambiguous or unmatched
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

# Bumped whenever the persisted plan JSON changes shape incompatibly.
PLAN_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class PlanStatus(Enum):
    """Overall status of the canonical validation plan."""
    COMPLETE   = "complete"    # All columns matched, plan is valid
    PARTIAL    = "partial"     # Some columns unmatched, plan usable with warnings
    AMBIGUOUS  = "ambiguous"   # Unresolved ambiguities remain
    INVALID    = "invalid"     # Plan validation failed — do NOT generate SQL/YAML


class MatchMethod(Enum):
    """How the source→target column match was established."""
    CONFIGURED      = "configured"       # User-specified explicit mapping
    EXACT           = "exact"            # Case-insensitive original name match
    NORMALIZED_EXACT= "normalized_exact" # Normalized name match (e.g. created_at == CREATEDAT)
    FUZZY           = "fuzzy"            # High-confidence fuzzy match (no AI)
    FUZZY_AI        = "fuzzy_ai"         # Fuzzy candidates resolved by AI
    AI              = "ai"               # AI direct match (rare)
    STATIC          = "static"           # Legacy static mapper (backward compat)
    SKIP            = "skip"             # Column intentionally skipped


# ---------------------------------------------------------------------------
# Column mapping entry — one per matched source column
# ---------------------------------------------------------------------------

@dataclass
class ColumnMappingEntry:
    """
    Describes how one source column maps to one target column.

    This is the core element of the CanonicalValidationPlan.
    The SQL generator and YAML generator read these entries to build output.

    SQL generation rules:
      - Always use source_column (original name) as the SQL identifier on PG side
      - Always use target_column (original name) as the SQL identifier on SF side
      - Never use normalized names in SQL
      - Apply the transformation_rule from the rules/ package
    """
    # ── Identity ──────────────────────────────────────────────────────────
    source_column:       str   # Original PG column name (use this in SQL)
    source_type:         str   # PostgreSQL data type
    source_normalized:   str   # Normalized name (for display/explanation only)
    target_column:       str   # Original SF column name (use this in SQL)
    target_type:         str   # Snowflake data type
    target_normalized:   str   # Normalized name (for display/explanation only)

    # ── Matching ──────────────────────────────────────────────────────────
    match_method:        str   # MatchMethod.value string
    fuzzy_score:         float = 0.0   # Raw name similarity (0.0 for exact)
    confidence:          float = 1.0   # Final confidence score [0.0, 1.0]
    confidence_breakdown: Optional[Dict[str, Any]] = None  # Factor breakdown

    # ── Transformation ────────────────────────────────────────────────────
    transformation_rule: str = "text"   # Rule ID from rules_catalog.json
    validation_rules:    List[str] = field(default_factory=list)  # e.g. ["null_check"]

    # ── Explainability ────────────────────────────────────────────────────
    reason:              str = ""   # Human-readable reason for the mapping decision
    ai_resolved:         bool = False   # True if AI was involved in this decision
    learned_example_used: Optional[str] = None  # ID of learned example used, if any

    # ── Validation ────────────────────────────────────────────────────────
    skip_validation:     bool = False   # True for Fivetran columns, unmatched, etc.
    skip_reason:         str = ""       # Why skipped

    # ── Primary key (informational) ───────────────────────────────────────
    is_primary_key:      bool = False
    pk_ordinal:          Optional[int] = None          # position in composite PK
    composite_pk_group:  Optional[List[str]] = None   # all PK columns when composite

    def to_dict(self) -> Dict[str, Any]:
        """Serialize losslessly — this dict is the persisted contract."""
        d: Dict[str, Any] = {
            "source_column":       self.source_column,
            "source_type":         self.source_type,
            "source_normalized":   self.source_normalized,
            "target_column":       self.target_column,
            "target_type":         self.target_type,
            "target_normalized":   self.target_normalized,
            "match_method":        self.match_method,
            "confidence":          round(self.confidence, 3),
            "transformation_rule": self.transformation_rule,
            "validation_rules":    list(self.validation_rules),
            "reason":              self.reason,
        }
        if self.fuzzy_score > 0.0:
            d["fuzzy_score"] = round(self.fuzzy_score, 3)
        if self.confidence_breakdown:
            d["confidence_breakdown"] = self.confidence_breakdown
        if self.ai_resolved:
            d["ai_resolved"] = True
        if self.learned_example_used:
            d["learned_example_used"] = self.learned_example_used
        if self.skip_validation:
            d["skip_validation"] = True
            d["skip_reason"] = self.skip_reason
        if self.is_primary_key:
            d["is_primary_key"] = True
            if self.pk_ordinal is not None:
                d["pk_ordinal"] = self.pk_ordinal
            if self.composite_pk_group:
                d["composite_pk_group"] = list(self.composite_pk_group)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ColumnMappingEntry":
        """Rebuild an entry from its ``to_dict()`` form."""
        source_column = d["source_column"]
        target_column = d.get("target_column", "")
        return cls(
            source_column=source_column,
            source_type=d.get("source_type", ""),
            source_normalized=d.get("source_normalized", source_column.lower()),
            target_column=target_column,
            target_type=d.get("target_type", ""),
            target_normalized=d.get("target_normalized", target_column.lower()),
            match_method=d.get("match_method", MatchMethod.EXACT.value),
            fuzzy_score=float(d.get("fuzzy_score", 0.0)),
            confidence=float(d.get("confidence", 1.0)),
            confidence_breakdown=d.get("confidence_breakdown"),
            transformation_rule=d.get("transformation_rule", "text"),
            validation_rules=list(d.get("validation_rules", [])),
            reason=d.get("reason", ""),
            ai_resolved=bool(d.get("ai_resolved", False)),
            learned_example_used=d.get("learned_example_used"),
            skip_validation=bool(d.get("skip_validation", False)),
            skip_reason=d.get("skip_reason", ""),
            is_primary_key=bool(d.get("is_primary_key", False)),
            pk_ordinal=d.get("pk_ordinal"),
            composite_pk_group=d.get("composite_pk_group"),
        )


# ---------------------------------------------------------------------------
# The plan itself
# ---------------------------------------------------------------------------

@dataclass
class CanonicalValidationPlan:
    """
    Single source of truth for one source→target table validation.

    Both SQL and YAML generators receive this object and produce output
    deterministically from it. The two generators never have different
    information — they always see the same plan.

    Attributes:
        source_database : PostgreSQL database name (for display only)
        source_schema   : PostgreSQL schema
        source_table    : PostgreSQL table name
        target_database : Snowflake database name
        target_schema   : Snowflake schema
        target_table    : Snowflake table name
        mappings        : Column mappings (one per source column)
        has_fivetran_active : Whether Snowflake table has _FIVETRAN_ACTIVE column
        status          : Overall plan status
        warnings        : Non-fatal warnings (e.g. low-confidence mappings)
        ambiguities     : Columns that couldn't be resolved even after AI
        unmatched_source_columns : Source columns with no target match
        unmatched_target_columns : Target columns that were never matched to
        ai_calls_made   : Number of AI API calls made during plan construction
        model_used      : AI model name used (empty if static-only)
        generated_by    : 'ai' | 'static' | 'fuzzy' | 'mixed'
        generated_at    : ISO timestamp of plan creation

    Stats (computed from mappings):
        total_source_columns : len(mappings) including skipped
        active_mappings      : mappings where skip_validation=False
        skipped_mappings     : mappings where skip_validation=True
        exact_matches        : mappings with method in [exact, normalized_exact, configured]
        fuzzy_matches        : mappings with method=fuzzy
        ai_resolved_matches  : mappings with method=fuzzy_ai or ai
    """

    # ── Table identity ─────────────────────────────────────────────────────
    source_database: str = ""
    source_db_type:  str = ""   # e.g. 'postgresql', 'mssql', 'mysql'
    source_schema:   str = ""
    source_table:    str = ""
    target_database: str = ""
    target_schema:   str = ""
    target_table:    str = ""

    # ── Mappings ───────────────────────────────────────────────────────────
    mappings: List[ColumnMappingEntry] = field(default_factory=list)

    # ── Fivetran ───────────────────────────────────────────────────────────
    has_fivetran_active: bool = False

    # ── Primary keys ───────────────────────────────────────────────────────
    source_primary_keys: List[str] = field(default_factory=list)
    target_primary_keys: List[str] = field(default_factory=list)
    pk_mismatch: bool = False
    pk_mismatch_reason: str = ""

    # ── Status and issues ──────────────────────────────────────────────────
    status:      str = PlanStatus.COMPLETE.value
    warnings:    List[str] = field(default_factory=list)
    ambiguities: List[str] = field(default_factory=list)
    unmatched_source_columns: List[str] = field(default_factory=list)
    unmatched_target_columns: List[str] = field(default_factory=list)

    # ── Migration filters ──────────────────────────────────────────────────
    # WHERE predicate (no "WHERE" keyword) applied during SQL generation.
    # source_filter scopes the source query; target_filter scopes the target.
    # Empty string = no filter (full-table scan, original behaviour).
    # Example: source_filter="created_at >= '2024-01-01'"
    source_filter: str = ""
    target_filter: str = ""  # if blank, mirrors source_filter at generate time

    # ── Generation metadata ────────────────────────────────────────────────
    ai_calls_made:   int = 0
    model_used:      str = "N/A"
    generated_by:    str = "static"
    generated_at:    str = field(default_factory=lambda: datetime.now().isoformat())

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def active_mappings(self) -> List[ColumnMappingEntry]:
        """Mappings that should be validated (skip_validation=False)."""
        return [m for m in self.mappings if not m.skip_validation]

    @property
    def skipped_mappings(self) -> List[ColumnMappingEntry]:
        """Mappings excluded from validation."""
        return [m for m in self.mappings if m.skip_validation]

    @property
    def exact_matches(self) -> List[ColumnMappingEntry]:
        """Deterministically matched columns."""
        exact_methods = {"exact", "normalized_exact", "configured"}
        return [m for m in self.active_mappings if m.match_method in exact_methods]

    @property
    def fuzzy_matches(self) -> List[ColumnMappingEntry]:
        """High-confidence fuzzy matches (no AI involved)."""
        return [m for m in self.active_mappings if m.match_method == "fuzzy"]

    @property
    def ai_resolved_matches(self) -> List[ColumnMappingEntry]:
        """Mappings where AI was involved in the decision."""
        return [m for m in self.active_mappings if m.ai_resolved]

    @property
    def total_source_columns(self) -> int:
        return len(self.mappings)

    @property
    def active_count(self) -> int:
        return len(self.active_mappings)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_mappings)

    @property
    def is_valid(self) -> bool:
        return self.status != PlanStatus.INVALID.value

    def skipped_column_names(self) -> List[str]:
        return [m.source_column for m in self.skipped_mappings]

    def exclusion_summary(self) -> Dict[str, Any]:
        """
        Structured account of every column NOT validated.

        Consumed by ExclusionReport so no run can report a pass rate without
        also declaring what it declined to check.
        """
        excluded = [
            {
                "column": m.source_column,
                "type":   m.source_type,
                "reason": m.skip_reason or "no reason recorded",
            }
            for m in self.skipped_mappings
        ]
        for name in self.unmatched_source_columns:
            if any(e["column"] == name for e in excluded):
                continue
            excluded.append(
                {"column": name, "type": "", "reason": "no matching target column"}
            )
        return {
            "total_source_columns": self.total_source_columns,
            "validated":            self.active_count,
            "excluded":             excluded,
            "excluded_count":       len(excluded),
            "coverage_pct": (
                round(100.0 * self.active_count / self.total_source_columns, 1)
                if self.total_source_columns
                else 0.0
            ),
        }

    # ── Serialization ──────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize the full plan.

        This dict IS the contract. SQL and YAML are rendered from it, never
        the other way round, so it must round-trip losslessly via from_dict().
        """
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "source": {
                "database": self.source_database,
                "db_type":  self.source_db_type,
                "schema":   self.source_schema,
                "table":    self.source_table,
            },
            "target": {
                "database": self.target_database,
                "db_type":  "snowflake",
                "schema":   self.target_schema,
                "table":    self.target_table,
            },
            # Flat display strings — convenience for humans and old readers.
            "source_table":   f"{self.source_database}.{self.source_schema}.{self.source_table}",
            "target_table":   f"{self.target_database}.{self.target_schema}.{self.target_table}",
            "status":         self.status,
            "generated_at":   self.generated_at,
            "generated_by":   self.generated_by,
            "model_used":     self.model_used,
            "ai_calls_made":  self.ai_calls_made,
            "has_fivetran_active": self.has_fivetran_active,
            "source_filter": self.source_filter,
            "target_filter": self.target_filter,
            "primary_keys": {
                "source": self.source_primary_keys,
                "target": self.target_primary_keys,
                "mismatch": self.pk_mismatch,
                "mismatch_reason": self.pk_mismatch_reason,
            },
            "stats": {
                "total_source_columns": self.total_source_columns,
                "active_mappings":      self.active_count,
                "skipped_mappings":     self.skipped_count,
                "exact_matches":        len(self.exact_matches),
                "fuzzy_matches":        len(self.fuzzy_matches),
                "ai_resolved":          len(self.ai_resolved_matches),
                "unmatched_source":     len(self.unmatched_source_columns),
            },
            "exclusions": self.exclusion_summary(),
            "warnings":    self.warnings,
            "ambiguities": self.ambiguities,
            "unmatched_source_columns": self.unmatched_source_columns,
            "unmatched_target_columns": self.unmatched_target_columns,
            "mappings": [m.to_dict() for m in self.mappings],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CanonicalValidationPlan":
        """Rebuild a plan from its persisted JSON form."""
        version = d.get("schema_version")
        if version is not None and int(version) > PLAN_SCHEMA_VERSION:
            raise ValueError(
                f"Plan schema_version {version} is newer than this build "
                f"supports ({PLAN_SCHEMA_VERSION}). Upgrade the validator."
            )

        source = d.get("source") or {}
        target = d.get("target") or {}
        pks    = d.get("primary_keys") or {}

        return cls(
            source_database=source.get("database", ""),
            source_db_type=source.get("db_type", ""),
            source_schema=source.get("schema", ""),
            source_table=source.get("table", ""),
            target_database=target.get("database", ""),
            target_schema=target.get("schema", ""),
            target_table=target.get("table", ""),
            mappings=[ColumnMappingEntry.from_dict(m) for m in d.get("mappings", [])],
            has_fivetran_active=bool(d.get("has_fivetran_active", False)),
            source_filter=d.get("source_filter", ""),
            target_filter=d.get("target_filter", ""),
            source_primary_keys=list(pks.get("source", [])),
            target_primary_keys=list(pks.get("target", [])),
            pk_mismatch=bool(pks.get("mismatch", False)),
            pk_mismatch_reason=pks.get("mismatch_reason", ""),
            status=d.get("status", PlanStatus.COMPLETE.value),
            warnings=list(d.get("warnings", [])),
            ambiguities=list(d.get("ambiguities", [])),
            unmatched_source_columns=list(d.get("unmatched_source_columns", [])),
            unmatched_target_columns=list(d.get("unmatched_target_columns", [])),
            ai_calls_made=int(d.get("ai_calls_made", 0)),
            model_used=d.get("model_used", "N/A"),
            generated_by=d.get("generated_by", "ai"),
            generated_at=d.get("generated_at", datetime.now().isoformat()),
        )

    def summary_lines(self) -> List[str]:
        """Return lines for the CLI summary display."""
        excl = self.exclusion_summary()
        lines = [
            f"Source table      : {self.source_schema}.{self.source_table}",
            f"Target table      : {self.target_table}",
            f"Columns validated : {excl['validated']} / {excl['total_source_columns']}"
            f"  ({excl['coverage_pct']}% coverage)",
            f"Exact matches     : {len(self.exact_matches)}",
            f"Fuzzy matches     : {len(self.fuzzy_matches)}",
            f"AI-resolved       : {len(self.ai_resolved_matches)}",
            f"Fivetran filter   : {self.has_fivetran_active}",
            *(
                [f"Source filter     : {self.source_filter}"]
                if self.source_filter else []
            ),
            *(
                [f"Target filter     : {self.target_filter}"]
                if self.target_filter else []
            ),
            f"AI calls made     : {self.ai_calls_made}",
            f"Model used        : {self.model_used}",
            f"Status            : {self.status.upper()}",
        ]
        if excl["excluded"]:
            lines.append(f"EXCLUDED ({excl['excluded_count']}) — not validated:")
            for item in excl["excluded"]:
                lines.append(f"  ✗ {item['column']} — {item['reason']}")
        if self.warnings:
            lines.append(f"Warnings ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"  ⚠ {w}")
        if self.ambiguities:
            lines.append(f"Ambiguities ({len(self.ambiguities)}):")
            for a in self.ambiguities:
                lines.append(f"  ? {a}")
        return lines
