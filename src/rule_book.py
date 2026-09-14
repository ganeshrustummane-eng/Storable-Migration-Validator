"""
Efficient Rule Book — PostgreSQL → Snowflake Validation Rules Manager
=======================================================================
Single source of truth for all transformation rules.

How It Works
-------------
  1. At startup loads the base rules from the rules/ package (strongly-typed
     Python classes) AND from rules_catalog.json (for AI prompt text).
  2. Loads any LEARNED rules from rule_book_learned.json (auto-created).
  3. The RuleBook is the entry point for:
       - AI prompt injection     → build_prompt_block()
       - Rule lookup by type     → get_rule_for_type(pg_type, sf_type)
       - CLI display             → print_all()
       - Persisting new rules    → save_learned_rule()

Rule Sources
-------------
  src/rules/          ← Strongly-typed Python rule classes (used for SQL generation)
  src/rules_catalog.json  ← AI prompt descriptions for base rules
  src/rule_book_learned.json ← YOUR learned rules (auto-created, safe to commit)

Design Principles
-----------------
  - Base rules are IMMUTABLE (defined in rules/ package, do not edit at runtime)
  - Learned rules are MUTABLE (persisted to rule_book_learned.json)
  - AI prompt injection always includes BOTH base + learned rules
  - Rule lookup always delegates to the rules/ package registry
  - No duplication: the rules/ package owns the SQL logic; this file owns the
    human-readable metadata and the AI prompt representation.

Usage
-----
    from rule_book import rule_book         # global singleton

    # Look up a rule for a column type pair:
    rule = rule_book.get_rule_for_type("boolean", "BOOLEAN")
    pg_sql = rule.apply_postgresql("is_active", alias="is_active_normalized")

    # Build the AI prompt block (all rules as text):
    prompt_text = rule_book.build_prompt_block()

    # Add a user-defined learned rule:
    from rule_book import RuleEntry
    entry = RuleEntry(id="phone_strip", ...)
    rule_book.save_learned_rule(entry)
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from rules import (
    get_rule_for_type as _registry_lookup,
    get_rule_for_type_specific as _registry_lookup_specific,
    get_rule_by_name as _registry_get_by_name,
    BaseValidationRule,
)
from rules.postgres_base_rules import _normalize_type, _type_matches

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SRC_DIR      = Path(__file__).parent
_CATALOG_PATH = _SRC_DIR / "rules_catalog.json"
_LEARNED_PATH = _SRC_DIR / "rule_book_learned.json"


# ---------------------------------------------------------------------------
# SQL template validation — learned rules are metadata/display text today
# (see module docstring: SQL generation always goes through the rules/
# package registry, never through a RuleEntry's templates), but nothing
# stops a future change from wiring them into real query generation. Reject
# anything that isn't a plausible single-expression `{col}` template now, so
# that day one of that wiring doesn't also become day one of a SQL
# injection hole.
# ---------------------------------------------------------------------------

class RuleValidationError(ValueError):
    """Raised when a learned rule's SQL template fails safety validation."""


_BANNED_SQL_KEYWORDS = (
    "DROP", "DELETE", "ALTER", "TRUNCATE", "INSERT", "UPDATE",
    "GRANT", "REVOKE", "EXEC", "EXECUTE", "CREATE", "MERGE", "CALL",
)
_BANNED_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(_BANNED_SQL_KEYWORDS) + r")\b", re.IGNORECASE
)


def _validate_sql_template(field_name: str, template: str) -> None:
    """Raise RuleValidationError if `template` looks like more than a single
    read-only `{col}` SQL expression fragment."""
    if not template:
        return
    if ";" in template:
        raise RuleValidationError(
            f"{field_name} may not contain ';' (no multi-statement SQL allowed)."
        )
    if "--" in template or "/*" in template or "*/" in template:
        raise RuleValidationError(
            f"{field_name} may not contain SQL comment markers ('--', '/*', '*/')."
        )
    match = _BANNED_KEYWORD_RE.search(template)
    if match:
        raise RuleValidationError(
            f"{field_name} contains the disallowed keyword '{match.group(1).upper()}' — "
            f"only a read-only expression using {{col}} is allowed."
        )


# ---------------------------------------------------------------------------
# Data structure for a single rule entry (metadata + SQL templates)
# ---------------------------------------------------------------------------

@dataclass
class RuleEntry:
    """
    Metadata record for one transformation rule.

    SQL generation is handled by the rules/ package (strongly-typed classes).
    This dataclass is used for:
      - AI prompt injection (pg_sql_template / sf_sql_template are text descriptions)
      - CLI display
      - Storing learned rules to disk
    """
    id: str                           # snake_case identifier matching rule_name in rules/
    display_name: str                 # Human-readable name
    description: str                  # What the rule does in plain English
    when_to_apply: str                # "Apply when source=X maps to target=Y"
    pg_sql_template: str              # PostgreSQL SQL expression template (uses {col})
    sf_sql_template: str              # Snowflake SQL expression template  (uses {col})
    source_type: str                  # Primary trigger source DB type (e.g. "VARCHAR")
    target_type: str                  # Primary trigger target DB type (e.g. "STRING")
    is_learned: bool = False          # True = came from rule_book_learned.json
    learned_at: Optional[str] = None  # ISO timestamp when learned
    example: Optional[str] = None     # Optional example SQL or scenario
    pg_type_pairs: Optional[List[Dict[str, str]]] = field(default=None)

    # ── Gap-filler fields (learned rules only) ──────────────────────────────
    # status:      "draft" (default, advisory only — shown in Rule Book, never
    #              affects generated SQL) or "active" (a human clicked
    #              Activate — now consulted by get_rule_for_type() as a gap
    #              filler, but ONLY for type pairs no base rule already owns).
    # reuses_rule: Base rule id (e.g. "text", "boolean") whose already-tested
    #              SQL this learned rule reuses. This is the anti-hallucination
    #              guard: a learned rule can only replay an existing, known-
    #              good rule's behavior — it can never inject fresh SQL into
    #              real query generation. None = advisory-only (metadata/
    #              AI-prompt-context), same as pre-existing behavior.
    status: str = "draft"
    reuses_rule: Optional[str] = None
    activated_at: Optional[str] = None

    def to_prompt_line(self) -> str:
        """Format this rule for injection into an AI prompt."""
        learned_tag = " [LEARNED]" if self.is_learned else ""
        type_info   = f"source_type={self.source_type} → target_type={self.target_type}"
        return (
            f"  RULE: {self.id}{learned_tag}\n"
            f"    Types  : {type_info}\n"
            f"    When   : {self.when_to_apply}\n"
            f"    PG SQL : {self.pg_sql_template}\n"
            f"    SF SQL : {self.sf_sql_template}\n"
            f"    Note   : {self.description}"
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON persistence."""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "when_to_apply": self.when_to_apply,
            "pg_sql_template": self.pg_sql_template,
            "sf_sql_template": self.sf_sql_template,
            "source_type": self.source_type,
            "target_type": self.target_type,
            "is_learned": self.is_learned,
            "learned_at": self.learned_at,
            "example": self.example,
            "status": self.status,
            "reuses_rule": self.reuses_rule,
            "activated_at": self.activated_at,
        }


# ---------------------------------------------------------------------------
# Rule Book Manager
# ---------------------------------------------------------------------------

class RuleBook:
    """
    Central manager for all transformation rules.

    Architecture
    ------------
      Base rules     → loaded from rules_catalog.json (metadata) + rules/ (SQL logic)
      Learned rules  → loaded from rule_book_learned.json (user-defined additions)

    SQL Generation
    --------------
      Always delegates to the rules/ package registry via get_rule_for_type().
      Learned rules that define ONLY text templates (no matching rules/ class)
      fall back to TextRule with the template applied via TRIM().

    Thread Safety
    -------------
      Not thread-safe by design — single-process CLI tool.
    """

    def __init__(self):
        self._base_rules:    List[RuleEntry] = []
        self._learned_rules: List[RuleEntry] = []
        self._load_base()
        self._load_learned()

    # ──────────────────────────────────────────────────────────────────────
    # Loaders
    # ──────────────────────────────────────────────────────────────────────

    def _load_base(self):
        """
        Load base rule metadata from rules_catalog.json v3.0.
        SQL logic is owned by the rules/ package — this only loads metadata.
        """
        if not _CATALOG_PATH.exists():
            print(f"  [WARN] rules_catalog.json not found at {_CATALOG_PATH}")
            return

        try:
            with open(_CATALOG_PATH, encoding="utf-8") as f:
                data = json.load(f)

            for r in data.get("rules", []):
                rule_id   = r.get("enum_value", r.get("id", "unknown"))
                pairs     = r.get("pg_type_pairs", [{"source": "*", "target": "*"}])
                src_type  = pairs[0]["source"] if pairs else "*"
                tgt_type  = pairs[0]["target"] if pairs else "*"
                pg_expr   = r.get("pg_expression", "{col}")
                sf_expr   = r.get("sf_expression", "{col}")

                self._base_rules.append(RuleEntry(
                    id=rule_id,
                    display_name=r.get("display_name", rule_id),
                    description=r.get("description", ""),
                    when_to_apply=_build_when_text(src_type, tgt_type, pairs),
                    pg_sql_template=pg_expr,
                    sf_sql_template=sf_expr,
                    source_type=src_type,
                    target_type=tgt_type,
                    is_learned=False,
                    pg_type_pairs=pairs,
                ))

        except Exception as exc:
            print(f"  [WARN] Could not load rules_catalog.json: {exc}")

    def _load_learned(self):
        """Load user-learned rules from rule_book_learned.json."""
        if not _LEARNED_PATH.exists():
            return
        try:
            with open(_LEARNED_PATH, encoding="utf-8") as f:
                data = json.load(f)

            for r in data.get("learned_rules", []):
                self._learned_rules.append(RuleEntry(
                    id=r["id"],
                    display_name=r.get("display_name", r["id"]),
                    description=r.get("description", ""),
                    when_to_apply=r.get("when_to_apply", ""),
                    pg_sql_template=r.get("pg_sql_template", "{col}"),
                    sf_sql_template=r.get("sf_sql_template", "{col}"),
                    source_type=r.get("source_type", "*"),
                    target_type=r.get("target_type", "*"),
                    is_learned=True,
                    learned_at=r.get("learned_at"),
                    example=r.get("example"),
                    # Missing "status" (entries saved before this field existed)
                    # defaults to "draft" — safe by construction: nothing already
                    # sitting in the file starts affecting live SQL generation
                    # just because this code shipped.
                    status=r.get("status", "draft"),
                    reuses_rule=r.get("reuses_rule"),
                    activated_at=r.get("activated_at"),
                ))

            if self._learned_rules:
                print(
                    f"  📚 Loaded {len(self._learned_rules)} learned rule(s) "
                    f"from rule_book_learned.json"
                )
        except Exception as exc:
            print(f"  [WARN] Could not load rule_book_learned.json: {exc}")

    # ──────────────────────────────────────────────────────────────────────
    # Public API — Rule Lookup
    # ──────────────────────────────────────────────────────────────────────

    def get_rule_for_type(
        self,
        pg_type: str,
        sf_type: str,
    ) -> BaseValidationRule:
        """
        Return the best matching validation rule for a (pg_type, sf_type) pair.

        Two-layer lookup (base rules always win; learned rules only fill gaps):
          1. Base rules (rules/ package) — checked first, exactly as before
             this method existed. A learned rule can NEVER shadow a type pair
             a base rule already owns.
          2. Learned rules — consulted ONLY when no base rule has a SPECIFIC
             match (i.e. the pair would otherwise fall through to the TextRule
             wildcard). Only ACTIVE learned rules with a reuses_rule set are
             considered; the returned rule is the referenced BASE rule's
             already-tested implementation (never a custom SQL template) —
             see RuleEntry.reuses_rule for the anti-hallucination rationale.
          3. Base rules' own wildcard fallback (TextRule), as before.

        Args:
            pg_type: PostgreSQL column data type (e.g. 'boolean', 'character varying')
            sf_type: Snowflake column data type  (e.g. 'BOOLEAN', 'VARCHAR')

        Returns:
            BaseValidationRule instance ready to call apply_postgresql() / apply_snowflake()
        """
        specific = _registry_lookup_specific(pg_type, sf_type)
        if specific is not None:
            return specific

        gap_filler = self._find_active_gap_filler(pg_type, sf_type)
        if gap_filler is not None:
            base_rule = _registry_get_by_name(gap_filler.reuses_rule)
            if base_rule is not None:
                return base_rule

        return _registry_lookup(pg_type, sf_type)

    def _find_active_gap_filler(self, pg_type: str, sf_type: str) -> Optional["RuleEntry"]:
        """First ACTIVE learned rule (with reuses_rule set) matching this type
        pair, or None. Only called when no base rule has a specific match."""
        pg_norm = _normalize_type(pg_type)
        sf_norm = _normalize_type(sf_type)
        for r in self._learned_rules:
            if r.status != "active" or not r.reuses_rule:
                continue
            if _type_matches(pg_norm, _normalize_type(r.source_type)) and \
               _type_matches(sf_norm, _normalize_type(r.target_type)):
                return r
        return None

    def get_rule_by_id(self, rule_id: str) -> Optional[RuleEntry]:
        """Look up a rule entry by ID (case-insensitive). Returns None if not found."""
        key = rule_id.upper()
        for r in self.all_rules():
            if r.id.upper() == key:
                return r
        return None

    def rule_exists(self, rule_id: str) -> bool:
        """Return True if a rule with this ID exists in base or learned rules."""
        return self.get_rule_by_id(rule_id) is not None

    # ──────────────────────────────────────────────────────────────────────
    # Public API — Collections
    # ──────────────────────────────────────────────────────────────────────

    def all_rules(self) -> List[RuleEntry]:
        """Return all rules: base rules first, then learned rules."""
        return self._base_rules + self._learned_rules

    def base_rules(self) -> List[RuleEntry]:
        """Return only the built-in base rules."""
        return list(self._base_rules)

    def learned_rules(self) -> List[RuleEntry]:
        """Return only the user-learned rules."""
        return list(self._learned_rules)

    def stats(self) -> Dict[str, int]:
        """Return counts of base, learned, and total rules."""
        return {
            "base_rules":    len(self._base_rules),
            "learned_rules": len(self._learned_rules),
            "total_rules":   len(self.all_rules()),
        }

    # ──────────────────────────────────────────────────────────────────────
    # Public API — Persistence
    # ──────────────────────────────────────────────────────────────────────

    def save_learned_rule(self, entry: RuleEntry) -> bool:
        """
        Persist a new learned rule to rule_book_learned.json.

        If a rule with the same ID already exists in learned rules, it is UPDATED.
        Base rules cannot be overwritten — only learned rules can be updated.

        Returns:
            True on success, False on write failure.

        Raises:
            RuleValidationError: if pg_sql_template/sf_sql_template contain
                anything beyond a single read-only {col} expression.
        """
        _validate_sql_template("Source SQL template", entry.pg_sql_template)
        _validate_sql_template("Snowflake SQL template", entry.sf_sql_template)

        if entry.reuses_rule and entry.reuses_rule not in self.base_rule_ids():
            raise RuleValidationError(
                f"reuses_rule '{entry.reuses_rule}' is not a known base rule id. "
                f"Must be one of: {', '.join(self.base_rule_ids())}"
            )

        entry.is_learned = True
        if not entry.learned_at:
            entry.learned_at = datetime.now().isoformat()

        # Remove existing learned entry with same ID (allow update/overwrite)
        self._learned_rules = [
            r for r in self._learned_rules
            if r.id.upper() != entry.id.upper()
        ]
        self._learned_rules.append(entry)
        return self._flush_learned()

    def base_rule_ids(self) -> List[str]:
        """Return the closed set of base rule ids (e.g. ['boolean', 'numeric', ...])
        that a learned rule's reuses_rule field is allowed to reference."""
        seen: List[str] = []
        for r in self._base_rules:
            if r.id not in seen:
                seen.append(r.id)
        return seen

    def activate_learned_rule(self, rule_id: str) -> bool:
        """
        Flip a learned rule from draft to active — the human review gate.

        Only ACTIVE learned rules with reuses_rule set are ever consulted by
        get_rule_for_type() as a gap filler. Returns False if the rule id
        isn't found among learned rules, or if it has no reuses_rule (nothing
        safe to activate — advisory-only entries stay advisory-only).
        """
        for r in self._learned_rules:
            if r.id.upper() == rule_id.upper():
                if not r.reuses_rule:
                    return False
                r.status = "active"
                r.activated_at = datetime.now().isoformat()
                return self._flush_learned()
        return False

    def deactivate_learned_rule(self, rule_id: str) -> bool:
        """Flip a learned rule back to draft — instantly stops it from being
        consulted as a gap filler, without deleting it."""
        for r in self._learned_rules:
            if r.id.upper() == rule_id.upper():
                r.status = "draft"
                return self._flush_learned()
        return False

    def _flush_learned(self) -> bool:
        """
        Write current learned rules list to disk atomically.

        rule_book_learned.json is shared with learning/feedback.py, which
        writes a separate "learned_corrections" key (human column-mapping
        corrections) into the SAME file. This reads the file first and only
        replaces "learned_rules" — overwriting the whole dict from scratch
        would silently wipe out any corrections feedback.py already saved.
        """
        try:
            existing: Dict[str, Any] = {}
            if _LEARNED_PATH.exists():
                try:
                    with open(_LEARNED_PATH, encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = {}

            existing["_comment"] = (
                "Auto-generated by rule_book.py. "
                "Add new rules via the CLI (python validate_cli.py add-rule). "
                "This file is safe to commit to Git — it is your team's shared rule memory."
            )
            existing["version"] = existing.get("version", "1.0")
            existing["last_updated"] = datetime.now().isoformat()
            existing["learned_rules"] = [r.to_dict() for r in self._learned_rules]
            existing.setdefault("learned_corrections", [])

            with open(_LEARNED_PATH, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            return True
        except Exception as exc:
            print(f"  [ERROR] Could not save learned rules: {exc}")
            return False

    # ──────────────────────────────────────────────────────────────────────
    # Public API — AI Prompt Block
    # ──────────────────────────────────────────────────────────────────────

    def build_prompt_block(self) -> str:
        """
        Build a formatted text block of ALL rules (base + learned) for injection
        into an AI prompt (system prompt or user prompt).

        The block includes:
          - Complete rule catalog with SQL templates for each rule
          - Type-pair trigger conditions
          - Rule chaining order
          - NULL placeholder specification
          - Fivetran filter specification

        Returns:
            Multi-line string ready to inject into any LLM prompt.
        """
        sep = "=" * 65
        lines = [
            sep,
            "  TRANSFORMATION RULES CATALOG (PostgreSQL → Snowflake)",
            "  These rules MUST be applied when generating validation SQL.",
            sep,
            "",
            "  CORE NORMALIZATION PRINCIPLES:",
            "  ─────────────────────────────────────────────────────────",
            "  1. BOOLEAN    : TRUE/FALSE → '1'/'0'  (eliminates case differences)",
            "  2. NUMERIC    : cast to text at full native precision  (no rounding)",
            "  3. TIMESTAMP  : format to microsecond precision  (YYYY-MM-DD HH24:MI:SS.FF6)",
            "  4. TIMESTAMP_TZ: Convert to UTC first, then format to microsecond precision",
            "  5. DATE       : TO_CHAR 'YYYY-MM-DD'  (uniform date text)",
            "  6. TEXT/VARCHAR: TRIM leading/trailing spaces  (eliminates whitespace noise)",
            "  7. UUID       : TRIM() only, case preserved as stored",
            "  8. INTEGER    : CAST to text  (eliminates type-width differences)",
            "  9. JSON/JSONB : raw document text; canonicalized in Python after fetch  (recursive key sort, JSON-in-string recursion, number normalization; no TRIM)",
            "  9b. HSTORE    : hstore_to_json() text; canonicalized in Python after fetch  (same as JSON — never flattened in SQL)",
            "  10. BYTEA     : hex encoding  (binary to comparable text)",
            "  11. NULL      : COALESCE(…, '<<NULL>>')  (ALL columns — outermost wrapper)",
            "  12. FIVETRAN  : WHERE _FIVETRAN_ACTIVE = TRUE  (Snowflake side — latest record only)",
            "",
        ]

        lines.append(f"  {'─' * 62}")
        lines.append("  BASE RULES (Built-In)")
        lines.append(f"  {'─' * 62}")
        for r in self._base_rules:
            lines.append(r.to_prompt_line())
            lines.append("")

        if self._learned_rules:
            lines.append(f"  {'─' * 62}")
            lines.append("  LEARNED RULES (Your Custom Rules — Always Applied)")
            lines.append(f"  {'─' * 62}")
            for r in self._learned_rules:
                lines.append(r.to_prompt_line())
                lines.append("")

        lines += [
            sep,
            "  RULE APPLICATION ORDER (innermost → outermost):",
            "    integer / uuid / json (raw text) / hstore (raw json text) / bytea",
            "    → boolean",
            "    → timestamp_tz (UTC normalize) → timestamp_ntz → date",
            "    → numeric (round)",
            "    → text (trim) / uuid (UPPER+TRIM)",
            "    → NULL placeholder COALESCE  ← ALWAYS LAST",
            "",
            "  NULL RULE: COALESCE(CAST(expr AS TEXT/STRING), '<<NULL>>')",
            "    Applied to EVERY column. SQL NULL ≠ NULL — sentinel ensures equality works.",
            "",
            "  FIVETRAN FILTER: WHERE _FIVETRAN_ACTIVE = TRUE",
            "    Applied on Snowflake side ONLY when _FIVETRAN_ACTIVE column is detected.",
            "    Ensures only the LATEST active record is compared (not historical snapshots).",
            sep,
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────
    # Public API — Display
    # ──────────────────────────────────────────────────────────────────────

    def print_all(self):
        """Pretty-print the full rule book to stdout."""
        print(self.build_prompt_block())

    def print_compact(self):
        """Print a compact one-line-per-rule summary to stdout."""
        print("\n  Base Rules:")
        for r in self._base_rules:
            print(f"    {r.id:<30} {r.description[:60]}")
        if self._learned_rules:
            print("\n  Learned Rules:")
            for r in self._learned_rules:
                ts = r.learned_at[:10] if r.learned_at else "unknown"
                print(f"    {r.id:<30} [added {ts}] {r.description[:50]}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_when_text(
    src_type: str,
    tgt_type: str,
    pairs: List[Dict[str, str]],
) -> str:
    """Build a human-readable 'when to apply' description."""
    pair_str = ", ".join(
        f"{p['source']} → {p['target']}"
        for p in (pairs or [])[:4]
    )
    suffix = f" (and {len(pairs) - 4} more)" if len(pairs or []) > 4 else ""
    return f"source_type={src_type}, target_type={tgt_type}. Triggers: {pair_str}{suffix}"


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly
# ---------------------------------------------------------------------------

rule_book = RuleBook()
