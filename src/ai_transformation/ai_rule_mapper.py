"""
AI Rule Mapper
===============
Uses DIAL/GPT-4o (or Claude direct) to intelligently assign the correct
validation rules to each PostgreSQL → Snowflake column pair.

Why AI over static matching:
  - Handles renamed columns (customer_id → cust_id) via semantic understanding.
  - Detects primary keys from context and naming conventions.
  - Produces a reasoning explanation for audit/review.

AI-only by design:
  There is NO static fallback. If the model is unreachable or unconfigured,
  mapping raises AIRuleMappingError.

Backend Selection (automatic — priority order):
  1. EPAM DIAL  — if DIAL_API_KEY is set in .env (current build/test backend)
       Uses AzureOpenAI client → proxies to GPT, Claude, Llama, Mistral, etc.
       via https://ai-proxy.lab.epam.com  (requires EPAM VPN)

  2. Claude Direct — if DIAL_API_KEY is NOT set but CLAUDE_API_KEY IS set
       Uses Anthropic SDK → calls api.anthropic.com directly (no VPN needed)
       Models: claude-3-5-sonnet-20241022, claude-3-opus-20240229, etc.

  3. Neither set → AIRuleMappingError (fails loudly, no silent degradation)

Model Selection:
  1. Pass model= parameter to AIRuleMapper(model="gpt-4o-mini")
  2. DIAL_MODEL env var  (for DIAL backend)
     CLAUDE_MODEL env var (for Claude direct backend)
  3. Select interactively from the CLI (validate_cli.py) → option [8]

Environment Variables:
  DIAL_API_KEY      — EPAM DIAL API key  (priority 1)
  DIAL_API_BASE     — defaults to https://ai-proxy.lab.epam.com
  DIAL_API_VERSION  — defaults to 2025-04-01-preview
  DIAL_MODEL        — model for DIAL backend (default: gpt-4o)

  CLAUDE_API_KEY    — Anthropic direct API key (priority 2, used if no DIAL key)
  CLAUDE_MODEL      — model for Claude backend (default: claude-3-5-sonnet-20241022)
"""

import json
import os
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_API_BASE     = "https://ai-proxy.lab.epam.com"
_DEFAULT_API_VERSION  = "2025-04-01-preview"
_DEFAULT_DIAL_MODEL   = "gpt-4o"
_DEFAULT_CLAUDE_MODEL = "claude-3-5-sonnet-20241022"

# Backend identifiers
_BACKEND_DIAL   = "dial"    # EPAM DIAL (AzureOpenAI proxy)
_BACKEND_CLAUDE = "claude"  # Anthropic direct API


def _is_claude_model(model_name: str) -> bool:
    """
    Return True if a model name belongs to the Anthropic Claude direct API.
    Used to prevent DIAL model names (gpt-4o, etc.) from being
    sent to the Anthropic API when Claude backend is active.
    """
    name = model_name.lower()
    return (
        name.startswith("claude-")
        or name.startswith("anthropic.claude-")  # DIAL bridge names excluded
        and not name.startswith("anthropic.")
    )


class AIRuleMappingError(RuntimeError):
    """Raised when AI column mapping cannot be performed."""


# Fivetran metadata column prefix — skip these in validation
_FIVETRAN_PREFIX = "_FIVETRAN_"

# ---------------------------------------------------------------------------
# Available DIAL models — shown to user in CLI model selection
# ---------------------------------------------------------------------------

AVAILABLE_MODELS = [
    # ── OpenAI GPT-5 tier ───────────────────────────────────────────────────
    "gpt-5",
    "gpt-5.6-terra-2026-07-09",
    # ── OpenAI GPT-4o family ────────────────────────────────────────────────
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4o-2024-11-20",
    # ── OpenAI GPT-4 Turbo ──────────────────────────────────────────────────
    "gpt-4-turbo",
    # ── OpenAI o-series (reasoning) ─────────────────────────────────────────
    "o3",
    "o3-mini",
    "o4-mini",
    # ── Anthropic Claude (via DIAL bridge) ──────────────────────────────────
    "anthropic.claude-sonnet-5",
    "anthropic.claude-opus-4",
    "anthropic.claude-sonnet-4",
    "anthropic.claude-haiku-4-5",
    "claude-3-5-sonnet",
    "claude-3-7-sonnet",
    # ── Meta Llama (via DIAL bridge) ────────────────────────────────────────
    "meta-llama-3-70b-instruct",
    "meta-llama-3-1-405b-instruct",
    # ── Mistral (via DIAL bridge) ───────────────────────────────────────────
    "mistral-large",
    "mistral-large-2",
]

# Claude direct models — EXACT names accepted by api.anthropic.com
# Reference: https://docs.anthropic.com/en/docs/about-claude/models
CLAUDE_DIRECT_MODELS = [
    "claude-opus-4-5",              # Most powerful (2025)
    "claude-sonnet-4-5",            # Best balance quality/speed (2025)
    "claude-haiku-4-5",             # Fastest, lowest cost (2025)
    "claude-opus-4-0",              # Claude 4 Opus
    "claude-sonnet-4-0",            # Claude 4 Sonnet
    "claude-3-7-sonnet-20250219",   # Extended thinking
    "claude-3-5-sonnet-20241022",   # Claude 3.5 Sonnet (stable)
    "claude-3-5-haiku-20241022",    # Claude 3.5 Haiku (stable)
    "claude-3-opus-20240229",       # Claude 3 Opus (stable)
    "claude-3-haiku-20240307",      # Claude 3 Haiku (stable)
]

# Human-readable descriptions and tier info for each model
MODEL_DESCRIPTIONS = {
    # GPT-5 tier
    "gpt-5":                        ("OpenAI",    "GPT-5",                     "Frontier reasoning — highest quality"),
    "gpt-5.6-terra-2026-07-09":     ("OpenAI",    "GPT-5 Terra",               "Latest GPT-5 snapshot (2026-07-09)"),
    # GPT-4o family
    "gpt-4o":                       ("OpenAI",    "GPT-4o",                    "Best balance accuracy/speed (default)"),
    "gpt-4o-mini":                  ("OpenAI",    "GPT-4o Mini",               "Fast, low-cost — good for simple tables"),
    "gpt-4o-2024-11-20":            ("OpenAI",    "GPT-4o Nov-20",             "Specific dated snapshot for reproducibility"),
    # GPT-4 Turbo
    "gpt-4-turbo":                  ("OpenAI",    "GPT-4 Turbo",               "128k context — large schema tables"),
    # O-series
    "o3":                           ("OpenAI",    "o3",                        "Advanced reasoning — complex type mappings"),
    "o3-mini":                      ("OpenAI",    "o3-mini",                   "Fast reasoning model"),
    "o4-mini":                      ("OpenAI",    "o4-mini",                   "Latest mini reasoning model"),
    # Anthropic Claude via DIAL
    "anthropic.claude-sonnet-5":    ("Anthropic", "Claude Sonnet 5",           "Top Anthropic model — best quality"),
    "anthropic.claude-opus-4":      ("Anthropic", "Claude Opus 4",             "Most powerful Claude — complex reasoning"),
    "anthropic.claude-sonnet-4":    ("Anthropic", "Claude Sonnet 4",           "Balanced Claude 4 model"),
    "anthropic.claude-haiku-4-5":   ("Anthropic", "Claude Haiku 4.5",          "Fastest Claude — simple rule assignment"),
    "claude-3-5-sonnet":            ("Anthropic", "Claude 3.5 Sonnet",         "Claude 3.5 via DIAL bridge"),
    "claude-3-7-sonnet":            ("Anthropic", "Claude 3.7 Sonnet",         "Claude 3.7 extended thinking"),
    # Meta Llama
    "meta-llama-3-70b-instruct":    ("Meta",      "Llama 3 70B",               "Open-weight — good for offline/on-prem"),
    "meta-llama-3-1-405b-instruct": ("Meta",      "Llama 3.1 405B",            "Largest Llama — near-frontier quality"),
    # Mistral
    "mistral-large":                ("Mistral",   "Mistral Large",             "European flagship LLM"),
    "mistral-large-2":              ("Mistral",   "Mistral Large 2",           "Latest Mistral flagship"),
    # Claude direct models (api.anthropic.com)
    "claude-opus-4-5":              ("Anthropic", "Claude Opus 4.5",           "Direct API — most powerful (2025)"),
    "claude-sonnet-4-5":            ("Anthropic", "Claude Sonnet 4.5",         "Direct API — best balance quality/speed (recommended)"),
    "claude-haiku-4-5":             ("Anthropic", "Claude Haiku 4.5",          "Direct API — fastest, lowest cost"),
    "claude-opus-4-0":              ("Anthropic", "Claude Opus 4.0",           "Direct API — Claude 4 Opus"),
    "claude-sonnet-4-0":            ("Anthropic", "Claude Sonnet 4.0",         "Direct API — Claude 4 Sonnet"),
    "claude-3-7-sonnet-20250219":   ("Anthropic", "Claude 3.7 Sonnet",         "Direct API — extended thinking"),
    "claude-3-5-sonnet-20241022":   ("Anthropic", "Claude 3.5 Sonnet",         "Direct API — stable, proven quality"),
    "claude-3-5-haiku-20241022":    ("Anthropic", "Claude 3.5 Haiku",          "Direct API — stable, fast"),
    "claude-3-opus-20240229":       ("Anthropic", "Claude 3 Opus",             "Direct API — Claude 3 most powerful"),
    "claude-3-haiku-20240307":      ("Anthropic", "Claude 3 Haiku",            "Direct API — Claude 3 fast"),
}


class AIRuleMapper:
    """
    Assigns validation rules to column pairs using DIAL or Claude direct.

    Backend is selected automatically based on environment variables:
      - DIAL_API_KEY set   → use EPAM DIAL (any model via DIAL proxy)
      - CLAUDE_API_KEY set → use Anthropic direct (Claude models only)
      - Neither set        → raises AIRuleMappingError on map_columns()

    There is NO static fallback. A silently-degraded mapping produces
    validation results that look authoritative but compared the wrong columns.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        api_version: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """
        Args:
            api_key    : DIAL API key OR Claude API key
                         (auto-detected from env if not provided)
            api_base   : DIAL endpoint base URL (DIAL backend only)
            api_version: API version (DIAL backend only)
            model      : Model name — DIAL model or Claude model
                         (auto-detected from env if not provided)
        """
        # ── Detect which backend to use ───────────────────────────────────
        # Priority 1: EPAM DIAL (if api_key explicitly passed, treat as DIAL)
        dial_key   = api_key or os.getenv("DIAL_API_KEY", "")
        # Priority 2: Claude direct (only checked when DIAL key is absent)
        claude_key = os.getenv("CLAUDE_API_KEY", "") if not dial_key else ""

        if dial_key:
            self._backend    = _BACKEND_DIAL
            self.api_key     = dial_key
            self.api_base    = api_base    or os.getenv("DIAL_API_BASE",    _DEFAULT_API_BASE)
            self.api_version = api_version or os.getenv("DIAL_API_VERSION", _DEFAULT_API_VERSION)
            self.model       = model       or os.getenv("DIAL_MODEL",        _DEFAULT_DIAL_MODEL)
        elif claude_key:
            self._backend    = _BACKEND_CLAUDE
            self.api_key     = claude_key
            self.api_base    = ""  # unused for Claude direct
            self.api_version = ""  # unused for Claude direct
            # IMPORTANT: ignore any model= passed in if it looks like a DIAL/GPT model
            # (prevents DIAL model names like 'gpt-4o' being sent to Anthropic API)
            claude_env_model = os.getenv("CLAUDE_MODEL", _DEFAULT_CLAUDE_MODEL)
            if model and _is_claude_model(model):
                self.model = model          # explicit Claude model name — use it
            else:
                self.model = claude_env_model  # fall back to env var always
        else:
            # No key — mark inactive; raise only when map_columns() is called
            self._backend    = _BACKEND_DIAL  # placeholder
            self.api_key     = ""
            self.api_base    = _DEFAULT_API_BASE
            self.api_version = _DEFAULT_API_VERSION
            self.model       = model or _DEFAULT_DIAL_MODEL

        self._ai_active = bool(self.api_key)

    # -----------------------------------------------------------------------
    # Backend: EPAM DIAL (AzureOpenAI proxy)
    # -----------------------------------------------------------------------
    #
    # NOTE: this class no longer has a map_columns() entry point — the only
    # caller (the old AI-only pipeline, RuleMapperOrchestrator) was removed.
    # _call_dial()/_call_claude() below are still live: rule_prompt_parser.py
    # calls them directly with its own prompt for the "paste a rule" feature.

    def _call_dial(
        self,
        system_prompt: str,
        user_prompt: str,
        table_name: str,
    ) -> str:
        """
        Call the EPAM DIAL endpoint using the AzureOpenAI SDK.
        Returns raw JSON string from the model.
        """
        try:
            from openai import AzureOpenAI  # type: ignore
        except ImportError as exc:
            raise AIRuleMappingError(
                "The 'openai' package is required for DIAL mapping. "
                "Install it with: pip install -r requirements.txt"
            ) from exc

        print(
            f"  [AIRuleMapper] Backend: EPAM DIAL  |  "
            f"Model: '{self.model}'  |  Table: '{table_name}'"
        )

        client = AzureOpenAI(
            api_key=self.api_key,
            api_version=self.api_version,
            azure_endpoint=self.api_base,
        )
        try:
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
        except Exception as exc:
            raise AIRuleMappingError(
                f"DIAL API error for '{table_name}': {exc}"
            ) from exc

        return response.choices[0].message.content

    # -----------------------------------------------------------------------
    # Backend: Anthropic Claude direct
    # -----------------------------------------------------------------------

    def _call_claude(
        self,
        system_prompt: str,
        user_prompt: str,
        table_name: str,
    ) -> str:
        """
        Call Anthropic Claude directly using the anthropic SDK.
        Returns raw JSON string from the model.

        Requires: pip install anthropic
        """
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise AIRuleMappingError(
                "The 'anthropic' package is required for Claude direct mapping.\n"
                "Install it with: pip install anthropic"
            ) from exc

        print(
            f"  [AIRuleMapper] Backend: Claude Direct  |  "
            f"Model: '{self.model}'  |  Table: '{table_name}'"
        )

        client = anthropic.Anthropic(api_key=self.api_key)

        # Claude API: system is a top-level param, not inside messages[]
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:
            raise AIRuleMappingError(
                f"Claude API error for '{table_name}': {exc}"
            ) from exc

        # Extract text — Claude returns a list of content blocks
        raw = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw += block.text

        # Strip markdown fences if Claude wrapped output in ```json ... ```
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw   = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            ).strip()

        return raw

    # NOTE: _build_system_prompt(), _build_user_prompt(), and _parse_response()
    # used to live here, supporting the old map_columns() entry point. Both were
    # removed together — the only caller (RuleMapperOrchestrator, the old AI-only
    # pipeline) is gone. Their prompt/rule knowledge is now live only in
    # src/ai/prompt_builder.py (column matching) and
    # src/generated_queries/ai_sql_generator.py (SQL generation) — see the
    # normalization-and-exclusions skill for the "one source of truth" note on
    # why these prompt-embedded rule tables should eventually read from
    # rules_catalog.json instead of being hand-copied.
