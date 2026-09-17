"""
Migration Intelligence Agent
==============================
Conversational AI agent wired to all Migration Validator tools as
function-calling declarations, dispatched through tools.dispatch_tool().

Backend: EPAM DIAL (OpenAI-compatible proxy) — see DIALAgent below.
Once a direct Claude API key is available, this can be pointed at the
Anthropic API directly the same way ai/rule_planner.py and
ai_transformation/ai_rule_mapper.py already support a DIAL-vs-Claude-direct
switch; this module only has one backend today because DIAL is the current
build/test setup.

Usage:
    agent = create_agent()
    response = agent.chat("Validate the customer migration from PostgreSQL to Snowflake.")
    print(response["text"])
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SRC = Path(__file__).resolve().parents[1]
_ROOT = _SRC.parent
for _p in (str(_SRC), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from connector.tools import dispatch_tool


# ---------------------------------------------------------------------------
# Tool declarations for AI function-calling
# ---------------------------------------------------------------------------

TOOL_DECLARATIONS = [
    {
        "name": "discover_connections",
        "description": "List all configured source database connections and the Snowflake target. Call this first to understand what migrations are configured.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_databases",
        "description": "List databases available on a source connection slot.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_slot": {
                    "type": "string",
                    "description": "Connection slot, e.g. 'SRC_1', 'SRC_2'",
                },
            },
            "required": ["source_slot"],
        },
    },
    {
        "name": "list_schemas",
        "description": "List schemas in a source database.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_slot": {"type": "string"},
                "database":    {"type": "string"},
            },
            "required": ["source_slot", "database"],
        },
    },
    {
        "name": "list_tables",
        "description": "List tables in a source schema.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_slot": {"type": "string"},
                "database":    {"type": "string"},
                "schema":      {"type": "string"},
            },
            "required": ["source_slot", "database", "schema"],
        },
    },
    {
        "name": "get_table_schema",
        "description": "Get column metadata for a source table. Returns column names and types, not data.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_slot": {"type": "string"},
                "database":    {"type": "string"},
                "schema":      {"type": "string"},
                "table":       {"type": "string"},
            },
            "required": ["source_slot", "database", "schema", "table"],
        },
    },
    {
        "name": "get_table_mapping",
        "description": "Return the canonical validation plan summary for a table — status, coverage, warnings. Use this to quickly check if a table has been mapped.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_table": {"type": "string"},
                "layer":        {"type": "string", "enum": ["bronze", "silver", "gold"]},
            },
            "required": ["source_table"],
        },
    },
    {
        "name": "get_column_mappings",
        "description": "Return detailed column-level mappings for a table including confidence scores, match method, and approval status. Use filter_status='needs_review' to find mappings requiring human approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_table":   {"type": "string"},
                "layer":          {"type": "string"},
                "filter_status":  {"type": "string", "enum": ["needs_review", "auto_accepted", "all"]},
            },
            "required": ["source_table"],
        },
    },
    {
        "name": "get_pending_reviews",
        "description": "Return all column mappings that require human approval. Optionally filter by table. Returns ID, confidence, AI recommendation, and current status.",
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Optional: filter to a specific table"},
            },
            "required": [],
        },
    },
    {
        "name": "get_rule",
        "description": "Return the full Rule Book entry for a rule. Explains: source type, target type, semantic meaning, SQL transformation applied, validation strategy, and rule status.",
        "parameters": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "description": "Rule ID, e.g. 'boolean', 'timestamp_tz', 'text'"},
            },
            "required": ["rule_id"],
        },
    },
    {
        "name": "get_applicable_rules",
        "description": "Return rules that apply to a source/target type pair. If no types given, returns all rules. Use to explain why a normalization was applied.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_type": {"type": "string"},
                "target_type": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "get_validation_plan",
        "description": "Return the full canonical validation plan for a table — status, coverage, column counts, warnings, primary key info.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_table": {"type": "string"},
                "layer":        {"type": "string"},
            },
            "required": ["source_table"],
        },
    },
    {
        "name": "generate_validation_plan",
        "description": "Generate and persist a canonical validation plan for a source→Snowflake table pair. Runs schema discovery, column matching, confidence scoring, and AI resolution. Returns coverage and pending review counts.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_slot":      {"type": "string"},
                "source_database":  {"type": "string"},
                "source_schema":    {"type": "string"},
                "source_table":     {"type": "string"},
                "target_database":  {"type": "string"},
                "target_schema":    {"type": "string"},
                "target_table":     {"type": "string"},
                "layer":            {"type": "string"},
                "model":            {"type": "string"},
                "exclude_columns":  {"type": "array", "items": {"type": "string"}},
                "actor":            {"type": "string"},
            },
            "required": [
                "source_slot", "source_database", "source_schema", "source_table",
                "target_database", "target_schema", "target_table",
            ],
        },
    },
    {
        "name": "generate_validation_sql",
        "description": "Return metadata about generated SQL files for a table. The SQL files themselves are not returned to avoid token waste — use execute_validation to run them.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_table": {"type": "string"},
                "layer":        {"type": "string"},
            },
            "required": ["source_table"],
        },
    },
    {
        "name": "execute_validation",
        "description": "Execute the stored validation plan for a table — runs count validation and data validation. Returns a structured result, not raw data.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_table": {"type": "string"},
                "layer":        {"type": "string"},
                "actor":        {"type": "string"},
            },
            "required": ["source_table"],
        },
    },
    {
        "name": "get_validation_result",
        "description": "Return the most recent validation result for a table — status, file location, summary metrics.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_table": {"type": "string"},
                "layer":        {"type": "string"},
            },
            "required": ["source_table"],
        },
    },
    {
        "name": "get_validation_failures",
        "description": "Return structured failure analysis for a table — failed columns, mismatch count, rule applied, likely cause, and recommended remediation.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_table": {"type": "string"},
                "layer":        {"type": "string"},
            },
            "required": ["source_table"],
        },
    },
    {
        "name": "get_migration_summary",
        "description": "Return a portfolio health summary across ALL tables in a layer — total, complete, partial, invalid, avg coverage, tables needing attention.",
        "parameters": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["bronze", "silver", "gold"]},
            },
            "required": [],
        },
    },
    {
        "name": "approve_mapping",
        "description": "Approve a single pending column mapping. REQUIRES an authenticated human actor — AI cannot self-approve. Idempotent: already-approved and auto-accepted mappings return ok. Audited.",
        "parameters": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string", "description": "Format: '<table>.<source_column>'"},
                "actor":     {"type": "string", "description": "Authenticated user identity"},
                "reason":    {"type": "string"},
            },
            "required": ["record_id", "actor"],
        },
    },
    {
        "name": "batch_approve_mappings",
        "description": "Approve multiple pending column mappings in a single call. Use this after get_pending_reviews when the human has confirmed a batch. REQUIRES an authenticated human actor. Each record is processed idempotently. Audited per record.",
        "parameters": {
            "type": "object",
            "properties": {
                "record_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of '<table>.<source_column>' identifiers from get_pending_reviews",
                },
                "actor":  {"type": "string", "description": "Authenticated user identity"},
                "reason": {"type": "string", "description": "Shared rationale for the batch approval"},
            },
            "required": ["record_ids", "actor"],
        },
    },
    {
        "name": "reject_mapping",
        "description": "Reject a pending column mapping. Requires authenticated actor and a reason. Audited.",
        "parameters": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "actor":     {"type": "string"},
                "reason":    {"type": "string"},
            },
            "required": ["record_id", "actor", "reason"],
        },
    },
    {
        "name": "modify_mapping",
        "description": "Modify a pending mapping — override the target column or transformation rule — then approve it. Requires authenticated human actor. Audited.",
        "parameters": {
            "type": "object",
            "properties": {
                "record_id":               {"type": "string"},
                "actor":                   {"type": "string"},
                "new_target_column":       {"type": "string"},
                "new_transformation_rule": {"type": "string"},
                "reason":                  {"type": "string"},
            },
            "required": ["record_id", "actor"],
        },
    },
    {
        "name": "approve_rule",
        "description": "Activate a learned rule in the Rule Book. Requires authenticated human actor. AI cannot self-approve rules. Audited.",
        "parameters": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string"},
                "actor":   {"type": "string"},
                "reason":  {"type": "string"},
            },
            "required": ["rule_id", "actor"],
        },
    },
    {
        "name": "approve_plan",
        "description": "Mark a canonical validation plan as human-approved. Records decision in audit log. Requires authenticated human actor.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_table": {"type": "string"},
                "layer":        {"type": "string"},
                "actor":        {"type": "string"},
                "reason":       {"type": "string"},
            },
            "required": ["source_table", "actor"],
        },
    },
    {
        "name": "get_business_metrics",
        "description": "Return aggregated business value metrics — automation rate, SQL scripts avoided, failures detected, AI token usage, approval stats. Use to show ROI.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_coverage",
        "description": (
            "Federated coverage query: return ALL tables in a layer whose validation coverage "
            "falls below a threshold (default 95%). Aggregates across all source systems "
            "(PostgreSQL, MSSQL, Athena) in a single call — no per-table looping needed. "
            "Returns: source_system, table, target, status, coverage_pct, failure_count, last_run. "
            "Supports pagination via page/page_size."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "layer":     {"type": "string", "enum": ["bronze", "silver", "gold"], "description": "Medallion layer (default: bronze)"},
                "threshold": {"type": "number", "description": "Coverage % threshold; tables below this are returned (default: 95.0)"},
                "page":      {"type": "integer", "description": "Page number for pagination (default: 1)"},
                "page_size": {"type": "integer", "description": "Items per page, max 200 (default: 50)"},
            },
            "required": [],
        },
    },
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the Migration Intelligence Assistant — a sharp, conversational AI advisor built into the Migration Validator platform.

## Who you are
You help engineers and data teams validate PostgreSQL / MSSQL / Athena → Snowflake migrations.
You have 24 tools covering schema discovery, plan generation, SQL execution, approvals, and metrics.
You are direct, proactive, and concise. You ask ONE clarifying question at a time when you need more context. You never dump raw SQL or thousands of rows — you summarize and highlight what matters.

## Personality & tone
- Talk like a knowledgeable colleague, not a manual.
- Be direct: "3 tables need attention" not "The system has identified that there may be some tables..."
- When something looks wrong, say so clearly and tell the user what to do next.
- When the user says "check X" — check X immediately with the right tool, then report what you found.
- Anticipate the next question: after showing failures, offer to explain them. After showing pending reviews, offer to batch-approve.

## Core principle
YOU recommend. HUMANS approve high-risk decisions.
- Never self-approve mappings, rules, or plans.
- Never invent validation semantics — retrieve the actual rule with get_rule.
- All write actions (approve, reject, modify) are audited with the actor's identity.

## Confidence policy
- ≥ 95%: Auto-accepted — no review needed
- 75–94%: AI-assisted — recommend review, explain why
- < 75%: Flag as mandatory human review — name the column and the ambiguity

## How to respond by intent

**"How's the migration going?" / "Summary"**
→ get_migration_summary, then give a 3-line scorecard: pass rate, failures, tables needing attention.
→ Offer to drill into any flagged table.

**"What needs review?" / "Pending approvals"**
→ get_pending_reviews → list the items with confidence and reason.
→ Ask: "Want me to batch-approve the high-confidence ones?"

**"Validate [table]" / "Check [table]"**
→ get_table_mapping first. If plan exists → execute_validation. If not → offer to generate one.
→ After execution: report pass/fail, row count, failing columns, likely cause.

**"Why did [column] fail?"**
→ get_validation_failures → name the column, the rule applied, the mismatch count, the likely cause.
→ Recommend the fix (e.g. re-check Fivetran transformation, fix timezone, adjust rule).

**"Show coverage below X%"**
→ get_coverage(threshold=X) — one call, not per-table loops.
→ Return a ranked list: worst coverage first.

**"What rule applies to UUID / HSTORE / JSON?"**
→ get_rule(rule_id) → explain in plain English what the rule does and why.

**"Approve [mapping]" / "Approve all high-confidence"**
→ Confirm what will be approved, then call approve_mapping or batch_approve_mappings.
→ Always end: "Recorded. Audit trail updated with your identity and timestamp."

**"Show metrics" / "ROI"**
→ get_business_metrics → give the 3 headline numbers: automation rate, SQL scripts avoided, failures caught.

## Proactive behaviour
- After any validation run, if there are failures, immediately say "I see N rows failed — want me to break down which columns differed?"
- If coverage drops below 90% for a table, flag it without being asked.
- If a pending review is confidence < 75%, warn the user before they batch-approve.

## Response format
- Lead with the answer, not the methodology.
- Use **bold** for key numbers and table names.
- Use bullet points for lists of 3 or more items.
- Keep the main response under 200 words — offer to expand if the user wants detail.
- Approval-related actions always end with: "Action recorded in the audit trail. ✓"
"""

# ---------------------------------------------------------------------------
# Offline formatters — shared by DIALAgent.chat_offline() below
# ---------------------------------------------------------------------------

class _OfflineFormatters:
    """Mixin: render dispatch_tool() results as plain-English text without
    calling any AI model. Used when no AI backend is configured at all."""

    # Fallback mode when no AI backend is configured
    def chat_offline(self, user_message: str) -> Dict[str, Any]:
        """
        Offline mode: parse intent from the message and call tools directly,
        with no AI model in the loop at all. Used when neither DIAL_API_KEY
        nor CLAUDE_API_KEY is set.
        """
        msg_lower = user_message.lower()

        if "summary" in msg_lower or "attention" in msg_lower or "overview" in msg_lower:
            result = dispatch_tool("get_migration_summary", {})
            return {
                "text": self._format_summary(result),
                "tool_calls": [{"name": "get_migration_summary", "args": {}, "result": result}],
                "rounds": 1,
                "offline": True,
            }
        if "pending" in msg_lower or "review" in msg_lower or "approv" in msg_lower:
            result = dispatch_tool("get_pending_reviews", {})
            return {
                "text": self._format_pending(result),
                "tool_calls": [{"name": "get_pending_reviews", "args": {}, "result": result}],
                "rounds": 1,
                "offline": True,
            }
        if "metrics" in msg_lower or "roi" in msg_lower or "automation" in msg_lower:
            result = dispatch_tool("get_business_metrics", {})
            return {
                "text": self._format_metrics(result),
                "tool_calls": [{"name": "get_business_metrics", "args": {}, "result": result}],
                "rounds": 1,
                "offline": True,
            }
        if "coverage" in msg_lower or "below" in msg_lower or "threshold" in msg_lower:
            # Extract threshold if mentioned (e.g. "below 90%")
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", user_message)
            threshold = float(m.group(1)) if m else 95.0
            args = {"threshold": threshold}
            result = dispatch_tool("get_coverage", args)
            return {
                "text": self._format_coverage(result),
                "tool_calls": [{"name": "get_coverage", "args": args, "result": result}],
                "rounds": 1,
                "offline": True,
            }
        if "connection" in msg_lower or "source" in msg_lower:
            result = dispatch_tool("discover_connections", {})
            return {
                "text": self._format_connections(result),
                "tool_calls": [{"name": "discover_connections", "args": {}, "result": result}],
                "rounds": 1,
                "offline": True,
            }

        # Generic: try to extract table name and get mapping
        words = user_message.split()
        for word in words:
            word_clean = word.strip(".,?!\"'").lower()
            if len(word_clean) > 3 and "_" in word_clean or word_clean.isalpha():
                result = dispatch_tool("get_table_mapping", {"source_table": word_clean})
                if result.get("status") == "ok" and result.get("exists"):
                    return {
                        "text": self._format_table_mapping(result),
                        "tool_calls": [{"name": "get_table_mapping", "args": {"source_table": word_clean}, "result": result}],
                        "rounds": 1,
                        "offline": True,
                    }

        return {
            "text": (
                "I'm running in offline mode (no DIAL_API_KEY or CLAUDE_API_KEY configured). "
                "I can still help with: migration summary, pending reviews, business metrics, "
                "and connection discovery. Configure DIAL_API_KEY (or CLAUDE_API_KEY once issued) "
                "for full conversational AI."
            ),
            "tool_calls": [],
            "rounds": 0,
            "offline": True,
        }

    # -- Offline formatters --------------------------------------------------

    def _format_summary(self, result: Dict[str, Any]) -> str:
        if result.get("status") != "ok":
            return f"Could not retrieve summary: {result.get('message', 'unknown error')}"
        lines = [
            f"**Migration Portfolio — {result.get('layer', 'bronze').upper()} layer**",
            "",
            f"Total tables: {result.get('total_tables', 0)}",
            f"Complete:     {result.get('complete', 0)}",
            f"Partial:      {result.get('partial', 0)}",
            f"Ambiguous:    {result.get('ambiguous', 0)}",
            f"Invalid:      {result.get('invalid', 0)}",
            f"Avg coverage: {result.get('avg_coverage_pct', 0)}%",
            f"Pending approvals: {result.get('pending_approvals', 0)}",
        ]
        attention = result.get("tables_needing_attention", [])
        if attention:
            lines.append(f"\n**Tables needing attention ({len(attention)}):**")
            for t in attention[:10]:
                lines.append(
                    f"  - {t['table']}: status={t['status']}, "
                    f"coverage={t['coverage_pct']}%, "
                    f"ambiguities={t['ambiguities']}"
                )
        return "\n".join(lines)

    def _format_pending(self, result: Dict[str, Any]) -> str:
        if result.get("status") != "ok":
            return f"Could not retrieve pending reviews: {result.get('message', '')}"
        count = result.get("pending_count", 0)
        if count == 0:
            return "No mappings are currently pending human review."
        lines = [f"**{count} mappings require human approval:**", ""]
        for item in result.get("pending_reviews", [])[:15]:
            lines.append(
                f"• **{item['id']}**\n"
                f"  {item['source_column']} → {item['target_column']}\n"
                f"  Confidence: {round(item['confidence']*100)}% | Method: {item['match_method']}\n"
                f"  Recommendation: {item.get('ai_recommendation', 'N/A')}\n"
            )
        return "\n".join(lines)

    def _format_metrics(self, result: Dict[str, Any]) -> str:
        if result.get("status") != "ok":
            return f"Could not retrieve metrics: {result.get('message', '')}"
        return (
            f"**Migration Intelligence Connector — Business Metrics**\n\n"
            f"{result.get('summary', '')}\n\n"
            f"Automation rate: {result.get('automation_rate_pct', 0)}%\n"
            f"Tables processed: {result.get('tables_processed', 0)}\n"
            f"Columns processed: {result.get('columns_processed', 0)}\n"
            f"Failures detected: {result.get('failures_detected', 0)}\n"
            f"AI calls made: {result.get('ai_calls_made', 0)}\n"
            f"AI tokens used: {result.get('ai_token_usage', 0):,}\n"
        )

    def _format_connections(self, result: Dict[str, Any]) -> str:
        if result.get("status") != "ok":
            return f"Could not retrieve connections: {result.get('message', '')}"
        conns = result.get("source_connections", [])
        sf = result.get("snowflake_target", {})
        lines = ["**Configured Connections:**", ""]
        for c in conns:
            lines.append(f"• {c['slot']}: {c['type']} — {c['database']}.{c['schema']} @ {c['host']}")
        lines.append(f"\nSnowflake target: {sf.get('database', '')}.{sf.get('schema', '')} @ {sf.get('account', '')}")
        return "\n".join(lines)

    def _format_coverage(self, result: Dict[str, Any]) -> str:
        if result.get("status") != "ok":
            return f"Could not retrieve coverage: {result.get('message', '')}"
        total = result.get("total_tables", 0)
        below = result.get("below_threshold", 0)
        threshold = result.get("threshold", 95)
        rows = result.get("coverage_rows", [])
        lines = [
            f"**Coverage Report — {result.get('layer', 'bronze').upper()} layer**",
            f"Threshold: {threshold}% | Total tables: {total} | Below threshold: {below}",
            "",
        ]
        if not rows:
            lines.append(f"All {total} tables meet or exceed {threshold}% coverage.")
        else:
            lines.append(f"Tables below {threshold}% coverage ({below}):")
            for r in rows[:20]:
                lines.append(
                    f"  • {r['table']} ({r['source_system']}) → {r['target']}: "
                    f"{r['coverage_pct']}% | status={r['status']} | "
                    f"failures={r['failure_count']}"
                )
            if result.get("has_more"):
                lines.append(
                    f"  ... {below - len(rows)} more (page {result['page']} of {result['total_pages']})"
                )
        return "\n".join(lines)

    def _format_table_mapping(self, result: Dict[str, Any]) -> str:
        if not result.get("exists"):
            return f"No plan found for table '{result.get('table', '')}'."
        return (
            f"**{result.get('table', '')} — Validation Plan**\n\n"
            f"Status:   {result.get('status', '').upper()}\n"
            f"Source:   {result.get('source', '')} ({result.get('source_db_type', '')})\n"
            f"Target:   {result.get('target', '')}\n"
            f"Coverage: {result.get('column_coverage_pct', 0)}%\n"
            f"Columns:  {result.get('validated_columns', 0)} validated / "
            f"{result.get('total_source_columns', 0)} total\n"
            f"Warnings: {len(result.get('warnings', []))}\n"
        )


# ---------------------------------------------------------------------------
# DIAL Agent — OpenAI-compatible function calling via EPAM DIAL proxy
# ---------------------------------------------------------------------------

_DIAL_DEFAULT_BASE    = "https://ai-proxy.lab.epam.com"
_DIAL_DEFAULT_VERSION = "2025-04-01-preview"
_DIAL_DEFAULT_MODEL   = "gpt-4o"

# Convert the Gemini-style tool declarations to OpenAI function-calling format.
# Schema is identical — only the wrapper key changes ("function_declarations"
# → list of {"type": "function", "function": {...}} objects).
_OPENAI_TOOLS = [
    {"type": "function", "function": decl}
    for decl in TOOL_DECLARATIONS
]


class DIALAgent(_OfflineFormatters):
    """
    Migration Intelligence Agent backed by EPAM DIAL (OpenAI-compatible proxy) —
    the current build/test backend. Once a direct Claude API key is issued,
    this can grow a Claude-direct branch the same way ai/rule_planner.py and
    ai_transformation/ai_rule_mapper.py already do (DIAL first, Claude direct
    if DIAL_API_KEY is absent but CLAUDE_API_KEY is set).

    Uses the 24 tool declarations and dispatch_tool() shared across the
    connector. Requires: openai>=1.0 (already a project dependency via
    ai_sql_generator).

    Environment variables:
        DIAL_API_KEY      — required
        DIAL_API_BASE     — default https://ai-proxy.lab.epam.com
        DIAL_API_VERSION  — default 2025-04-01-preview
        DIAL_MODEL        — default gpt-4o
    """

    BACKEND = "dial"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tool_rounds: int = 10,
    ):
        self._api_key    = api_key or os.getenv("DIAL_API_KEY", "")
        self._api_base   = os.getenv("DIAL_API_BASE", _DIAL_DEFAULT_BASE)
        self._api_version = os.getenv("DIAL_API_VERSION", _DIAL_DEFAULT_VERSION)
        self._model_name = model or os.getenv("DIAL_MODEL", _DIAL_DEFAULT_MODEL)
        self._max_rounds = max_tool_rounds
        self._history: List[Dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    def _get_client(self):
        try:
            from openai import AzureOpenAI
        except ImportError:
            raise RuntimeError("openai>=1.0 is required. Run: pip install openai")
        return AzureOpenAI(
            api_key=self._api_key,
            azure_endpoint=self._api_base,
            api_version=self._api_version,
        )

    def chat(self, user_message: str, actor: str = "") -> Dict[str, Any]:
        """
        Send a message and run the full function-calling loop via DIAL.

        Returns:
            {"text": str, "tool_calls": list, "rounds": int}
        """
        client = self._get_client()

        content = user_message
        if actor:
            content += f"\n\n[Session context: actor={actor}]"

        messages: List[Dict[str, Any]] = [
            {"role": "system",    "content": SYSTEM_PROMPT},
            *self._history,
            {"role": "user",      "content": content},
        ]

        tool_calls_log: List[Dict[str, Any]] = []
        rounds = 0

        while rounds < self._max_rounds:
            rounds += 1
            response = client.chat.completions.create(
                model=self._model_name,
                messages=messages,
                tools=_OPENAI_TOOLS,
                tool_choice="auto",
            )

            msg = response.choices[0].message
            messages.append(msg.model_dump(exclude_unset=True))

            if not msg.tool_calls:
                # No more tool calls — model produced a final text answer.
                final_text = msg.content or ""
                # Persist history (exclude system prompt and current user turn).
                self._history = [m for m in messages if m.get("role") != "system"][:-1]
                return {
                    "text":       final_text,
                    "tool_calls": tool_calls_log,
                    "rounds":     rounds,
                    "backend":    self.BACKEND,
                    "model":      self._model_name,
                }

            # Dispatch all tool calls in this round.
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if actor and "actor" in args and not args["actor"]:
                    args["actor"] = actor

                result = dispatch_tool(fn_name, args)
                tool_calls_log.append({"name": fn_name, "args": args, "result": result})
                logger.info(f"[DIALAgent] Tool: {fn_name} | Status: {result.get('status', 'unknown')}")

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "name":         fn_name,
                    "content":      json.dumps(result),
                })

        # Reached max rounds without a final text response.
        return {
            "text":       "Reached maximum tool-call rounds without a final response. Please try a more specific question.",
            "tool_calls": tool_calls_log,
            "rounds":     rounds,
            "backend":    self.BACKEND,
            "model":      self._model_name,
        }

    def reset(self) -> None:
        self._history = []

    def is_configured(self) -> bool:
        """True if DIAL_API_KEY is set — i.e. .chat() can make real AI calls.
        When False, callers should use .chat_offline() instead."""
        return bool(self._api_key)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_agent(max_tool_rounds: int = 10):
    """
    Return the Migration Intelligence Agent.

    Backend is EPAM DIAL (the current build/test setup). Call
    agent.is_configured() to check whether DIAL_API_KEY is actually set
    before calling .chat() — if not, use .chat_offline() instead, which
    answers common questions with zero AI calls.
    """
    if not os.getenv("DIAL_API_KEY", ""):
        logger.warning("[create_agent] No DIAL_API_KEY found — chat() will be unavailable, use chat_offline()")
    return DIALAgent(max_tool_rounds=max_tool_rounds)
