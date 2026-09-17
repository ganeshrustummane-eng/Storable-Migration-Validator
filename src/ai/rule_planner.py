"""
AI Rule Planner
================
Sends ONLY ambiguous columns (with their top fuzzy candidates) to the AI.
Returns resolved MatchDecisions with transformation rule assignments.

Token-efficiency design:
  - Receives ONLY the columns with status="ai_needed" from CandidateMatcher
  - Sends ONE column per AI call (focused prompt, not the full schema)
  - AI receives only top N candidates — never the entire target schema
  - Falls back gracefully to the best fuzzy candidate on any API error

Integration:
  decisions = CandidateMatcher().match(source_cols, target_cols)
  ai_needed = [d for d in decisions if d.needs_ai]

  planner = RulePlanner(api_key=..., model=...)
  result  = planner.resolve(ai_needed, table_name="orders", learned_examples=[...])

  for decision in result.decisions:
      # decision.status is "resolved" or "unmatched" (never "ai_needed")
      # decision.method is "fuzzy_ai" if AI resolved it, "fuzzy" for fallback

Security notes:
  - No credentials are passed to the AI prompt (PromptBuilder enforces this)
  - No SQL is generated here — rule ID only
  - No data is sent to the AI — only column metadata (name, type, position)
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from matching.candidate_matcher import MatchDecision
from matching.fuzzy_matcher import FuzzyCandidate
from sql_extractor.extractors import ColumnMetadata
from ai.prompt_builder import PromptBuilder
from ai.response_parser import ResponseParser, AIColumnDecision

try:
    from token_usage_analysis.token_logger import log_usage, extract_openai_usage
except ImportError:
    def log_usage(*args, **kwargs):  # pragma: no cover - logging is best-effort
        pass

    def extract_openai_usage(response):  # pragma: no cover
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# ---------------------------------------------------------------------------
# Constants — reuse the same DIAL defaults as AIRuleMapper
# ---------------------------------------------------------------------------

_DEFAULT_API_BASE     = "https://ai-proxy.lab.epam.com"
_DEFAULT_API_VERSION  = "2025-04-01-preview"
_DEFAULT_MODEL        = "gpt-4o"
_DEFAULT_CLAUDE_MODEL = "claude-3-5-sonnet-20241022"

_BACKEND_DIAL   = "dial"
_BACKEND_CLAUDE = "claude"


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------

@dataclass
class PlannerResult:
    """
    Output of RulePlanner.resolve().

    Attributes:
        decisions      : Updated MatchDecision list (all resolved or unmatched).
                         No decision in this list has status="ai_needed".
        ai_decisions   : Map of source_column_name → AIColumnDecision.
                         Use this when building CanonicalValidationPlan to get
                         the transformation_rule, confidence, and reason from AI.
        ai_calls_made  : Number of successful AI API calls made.
        errors         : List of error strings (one per failed AI call, if any).
    """
    decisions:    List[MatchDecision]
    ai_decisions: Dict[str, AIColumnDecision]
    ai_calls_made: int = 0
    errors:       List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# RulePlanner
# ---------------------------------------------------------------------------

class RulePlanner:
    """
    Resolves ambiguous column mappings using an AI backend.

    Backend is selected automatically, same priority as AIRuleMapper:
      1. EPAM DIAL     — if DIAL_API_KEY is set (proxies to GPT, Claude, etc.)
      2. Claude Direct — if DIAL_API_KEY is NOT set but CLAUDE_API_KEY is set
                         (calls api.anthropic.com directly, no DIAL needed —
                         this is the path once a direct Claude API key is
                         issued and DIAL is retired)
      3. Neither set   — falls back to the best fuzzy candidate for every
                         ambiguous column (no AI calls)

    Sends ONLY ambiguous columns to the AI — one focused call per column.
    Falls back to the best fuzzy candidate when AI is unavailable or fails.

    Usage
    -----
        planner = RulePlanner()
        result  = planner.resolve(
            ai_needed_decisions=decisions,
            table_name="orders",
            learned_examples=rule_book.learned_examples,
        )
        # result.decisions: all resolved (no more "ai_needed")
        # result.ai_decisions: transformation rules from AI
        # result.ai_calls_made: how many AI calls were made
    """

    def __init__(
        self,
        api_key:     Optional[str] = None,
        api_base:    Optional[str] = None,
        api_version: Optional[str] = None,
        model:       Optional[str] = None,
        top_n:       int = 5,
    ):
        """
        Args:
            api_key     : DIAL or Claude direct API key
                          (default: DIAL_API_KEY, else CLAUDE_API_KEY env var)
            api_base    : DIAL base URL (DIAL backend only)
            api_version : Azure OpenAI API version (DIAL backend only)
            model       : Model deployment name (DIAL model or Claude model)
            top_n       : Max candidates to include per AI prompt
        """
        dial_key   = api_key or os.getenv("DIAL_API_KEY", "")
        claude_key = os.getenv("CLAUDE_API_KEY", "") if not dial_key else ""

        if dial_key:
            self._backend    = _BACKEND_DIAL
            self.api_key     = dial_key
            self.api_base    = api_base    or os.getenv("DIAL_API_BASE",    _DEFAULT_API_BASE)
            self.api_version = api_version or os.getenv("DIAL_API_VERSION", _DEFAULT_API_VERSION)
            self.model       = model       or os.getenv("DIAL_MODEL",       _DEFAULT_MODEL)
        elif claude_key:
            self._backend    = _BACKEND_CLAUDE
            self.api_key     = claude_key
            self.api_base    = ""
            self.api_version = ""
            self.model       = model or os.getenv("CLAUDE_MODEL", _DEFAULT_CLAUDE_MODEL)
        else:
            self._backend    = _BACKEND_DIAL  # placeholder — inactive either way
            self.api_key     = ""
            self.api_base    = api_base    or _DEFAULT_API_BASE
            self.api_version = api_version or _DEFAULT_API_VERSION
            self.model       = model       or _DEFAULT_MODEL

        self.top_n       = top_n
        self._ai_active  = bool(self.api_key)
        self._builder    = PromptBuilder()
        self._parser     = ResponseParser()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def resolve(
        self,
        ai_needed_decisions: List[MatchDecision],
        table_name:          str = "unknown",
        learned_examples:    Optional[List[dict]] = None,
        source_label:        str = "PostgreSQL",
    ) -> PlannerResult:
        """
        Resolve all ai_needed decisions.

        For each decision the planner:
          1. Builds a focused prompt (source col + top N candidates only)
          2. Calls the DIAL API
          3. Validates the response (target must be from the candidate list)
          4. Returns an updated MatchDecision with status="resolved"
             and method="fuzzy_ai"

        If AI is unavailable or the call fails, the best fuzzy candidate is
        accepted automatically with method="fuzzy" (graceful degradation).

        Args:
            ai_needed_decisions: Decisions with status="ai_needed"
            table_name          : Table name for AI context and logging
            learned_examples    : Optional learned correction examples
            source_label        : Source system name ("PostgreSQL", "MSSQL",
                                   "Athena", "Redshift") — passed through to
                                   the rule book so the system prompt's rules
                                   block is labeled correctly and filtered to
                                   the type pairs actually seen in this batch.

        Returns:
            PlannerResult with all decisions resolved.
        """
        if not ai_needed_decisions:
            return PlannerResult(decisions=[], ai_decisions={})

        if not self._ai_active:
            print(
                f"  [RulePlanner] No DIAL_API_KEY / CLAUDE_API_KEY — falling back to "
                f"best fuzzy for {len(ai_needed_decisions)} ambiguous column(s).",
                file=sys.stderr,
            )
            return self._fallback_all(ai_needed_decisions)

        client = None
        if self._backend == _BACKEND_CLAUDE:
            try:
                import anthropic  # type: ignore
                client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                print(
                    "  [RulePlanner] 'anthropic' not installed — falling back to fuzzy. "
                    "Install with: pip install anthropic",
                    file=sys.stderr,
                )
                return self._fallback_all(ai_needed_decisions)
        else:
            try:
                from openai import AzureOpenAI  # type: ignore
                client = AzureOpenAI(
                    api_key=self.api_key,
                    api_version=self.api_version,
                    azure_endpoint=self.api_base,
                )
            except ImportError:
                print(
                    "  [RulePlanner] 'openai' not installed — falling back to fuzzy.",
                    file=sys.stderr,
                )
                return self._fallback_all(ai_needed_decisions)

        # Only send the rule book entries relevant to the type pairs actually
        # present in this batch (source type → each candidate's target type,
        # capped to the same top_n candidates the user prompt shows) — keeps
        # the system prompt small instead of dumping the full rule catalog.
        type_pairs = sorted({
            (dec.source_col.data_type, cand.target_col.data_type)
            for dec in ai_needed_decisions
            for cand in dec.candidates[: self.top_n]
        })
        system_prompt = self._builder.build_system_prompt(
            type_pairs=type_pairs or None,
            source_label=source_label,
        )

        resolved_decisions: List[MatchDecision] = []
        ai_decisions:       Dict[str, AIColumnDecision] = {}
        ai_calls_made = 0
        errors:         List[str] = []

        for dec in ai_needed_decisions:
            src_col    = dec.source_col
            candidates = dec.candidates

            valid_target_names = [c.target_col.column_name for c in candidates[: self.top_n]]

            user_prompt = self._builder.build_user_prompt(
                source_col=src_col,
                candidates=candidates,
                learned_examples=learned_examples,
                table_name=table_name,
                top_n=self.top_n,
            )

            try:
                if self._backend == _BACKEND_CLAUDE:
                    response = client.messages.create(
                        model=self.model,
                        max_tokens=1024,
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_prompt}],
                    )
                    raw = "".join(
                        block.text for block in response.content if hasattr(block, "text")
                    ).strip()
                    if raw.startswith("```"):
                        raw = "\n".join(
                            line for line in raw.splitlines()
                            if not line.strip().startswith("```")
                        ).strip()
                    ai_calls_made += 1

                    usage_obj = getattr(response, "usage", None)
                    log_usage(
                        backend="claude",
                        model=self.model,
                        call_type="column_mapping",
                        context=f"{table_name}.{src_col.column_name}" if table_name else src_col.column_name,
                        prompt_tokens=getattr(usage_obj, "input_tokens", 0) or 0,
                        completion_tokens=getattr(usage_obj, "output_tokens", 0) or 0,
                        total_tokens=(getattr(usage_obj, "input_tokens", 0) or 0)
                        + (getattr(usage_obj, "output_tokens", 0) or 0),
                    )
                else:
                    response = client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": user_prompt},
                        ],
                        temperature=0,
                        response_format={"type": "json_object"},
                        extra_headers={"Api-Key": self.api_key},
                    )
                    raw = response.choices[0].message.content
                    ai_calls_made += 1

                    usage = extract_openai_usage(response)
                    log_usage(
                        backend="dial",
                        model=self.model,
                        call_type="column_mapping",
                        context=f"{table_name}.{src_col.column_name}" if table_name else src_col.column_name,
                        prompt_tokens=usage["prompt_tokens"],
                        completion_tokens=usage["completion_tokens"],
                        total_tokens=usage["total_tokens"],
                    )

                ai_dec = self._parser.parse(
                    raw_json=raw,
                    expected_source=src_col.column_name,
                    valid_target_names=valid_target_names,
                )

                if ai_dec.had_parse_error:
                    # Parse failed — fallback to best fuzzy
                    err_msg = (
                        f"[{src_col.column_name}] AI parse error: {ai_dec.parse_error}"
                    )
                    errors.append(err_msg)
                    print(f"  [RulePlanner] ⚠ {err_msg}", file=sys.stderr)
                    resolved_decisions.append(
                        _accept_best_fuzzy(dec, reason=f"AI parse error — fallback: {ai_dec.parse_error}")
                    )
                else:
                    # Find the target ColumnMetadata object for the chosen column
                    chosen_target = _find_target_col(ai_dec.target_column, candidates)

                    if chosen_target is None:
                        # Safety net — AI chose a column we can't find
                        err_msg = (
                            f"[{src_col.column_name}] AI target '{ai_dec.target_column}' "
                            "not found in candidate metadata — using best fuzzy"
                        )
                        errors.append(err_msg)
                        print(f"  [RulePlanner] ⚠ {err_msg}", file=sys.stderr)
                        resolved_decisions.append(_accept_best_fuzzy(dec, reason=err_msg))
                    else:
                        # AI successfully resolved the column
                        status = "resolved" if ai_dec.is_resolved else "ai_needed"
                        new_dec = MatchDecision(
                            source_col=dec.source_col,
                            target_col=chosen_target,
                            method="fuzzy_ai",
                            confidence=dec.confidence,
                            final_score=ai_dec.confidence,
                            fuzzy_score=dec.fuzzy_score,
                            candidates=dec.candidates,
                            status=status,
                            skip_validation=False,
                        )
                        resolved_decisions.append(new_dec)
                        ai_decisions[src_col.column_name] = ai_dec

                        print(
                            f"  [RulePlanner] ✓ {src_col.column_name} → "
                            f"{ai_dec.target_column}  "
                            f"rule={ai_dec.transformation_rule}  "
                            f"conf={ai_dec.confidence:.2f}  "
                            f"status={ai_dec.status}"
                        )

            except Exception as exc:
                backend_label = "Claude" if self._backend == _BACKEND_CLAUDE else "DIAL"
                err_msg = f"[{src_col.column_name}] {backend_label} API error: {exc}"
                errors.append(err_msg)
                print(f"  [RulePlanner] ✗ {err_msg} — using best fuzzy", file=sys.stderr)
                resolved_decisions.append(_accept_best_fuzzy(dec, reason=str(exc)))

        print(
            f"  [RulePlanner] Done: {ai_calls_made} AI call(s) made, "
            f"{len(errors)} error(s)."
        )
        return PlannerResult(
            decisions=resolved_decisions,
            ai_decisions=ai_decisions,
            ai_calls_made=ai_calls_made,
            errors=errors,
        )

    # -----------------------------------------------------------------------
    # Fallback helpers
    # -----------------------------------------------------------------------

    def _fallback_all(self, decisions: List[MatchDecision]) -> PlannerResult:
        """Accept best fuzzy candidate for every decision (no AI calls)."""
        return PlannerResult(
            decisions=[
                _accept_best_fuzzy(d, reason="No DIAL API key — accepted best fuzzy candidate")
                for d in decisions
            ],
            ai_decisions={},
            ai_calls_made=0,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _accept_best_fuzzy(dec: MatchDecision, reason: str = "") -> MatchDecision:
    """
    Return a new MatchDecision resolved to the top fuzzy candidate.
    Used when AI is unavailable or fails.
    """
    if dec.candidates:
        best_target = dec.candidates[0].target_col
        return MatchDecision(
            source_col=dec.source_col,
            target_col=best_target,
            method="fuzzy",
            confidence=dec.confidence,
            final_score=dec.final_score,
            fuzzy_score=dec.fuzzy_score,
            candidates=dec.candidates,
            status="resolved",
            skip_validation=False,
        )
    # No candidates at all — mark unmatched
    return MatchDecision(
        source_col=dec.source_col,
        target_col=None,
        method=None,
        confidence=dec.confidence,
        final_score=0.0,
        fuzzy_score=0.0,
        candidates=[],
        status="unmatched",
        skip_validation=True,
        skip_reason=f"No candidates and AI unavailable. {reason}".strip(),
    )


def _find_target_col(
    target_name: str,
    candidates: List[FuzzyCandidate],
) -> Optional[ColumnMetadata]:
    """Find the ColumnMetadata for target_name in the candidates list."""
    target_upper = target_name.upper()
    for cand in candidates:
        if cand.target_col.column_name.upper() == target_upper:
            return cand.target_col
    return None
