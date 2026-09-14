"""
Rule Mapper Orchestrator
=========================
Single entry point the pipeline uses for column mapping.

AI-only
-------
This orchestrator previously fell back to StaticRuleMapper when DIAL_API_KEY
was missing or the API failed. That fallback was removed. A mapping produced
by type-pair guessing is indistinguishable downstream from a reviewed one, so
the fallback quietly converted "we could not determine this" into "validated".
Mapping now fails loudly instead.

Model Selection
---------------
  1. orchestrator = RuleMapperOrchestrator(model="gpt-4o-mini")
  2. DIAL_MODEL environment variable
  3. Interactive CLI selection (validate_cli.py)

Usage:
    from ai_transformation import RuleMapperOrchestrator

    orchestrator = RuleMapperOrchestrator(model="gpt-4o")
    mappings, explanation = orchestrator.map_columns(
        source_columns=pg_columns,
        target_columns=sf_columns,
        table_name="events",
    )
"""

from typing import List, Optional, Tuple

from sql_extractor.extractors import ColumnMetadata
from ai_transformation.column_mapping import ColumnRuleMapping
from ai_transformation.ai_rule_mapper import (
    AIRuleMapper,
    AIRuleMappingError,
    AVAILABLE_MODELS,
)


class RuleMapperOrchestrator:
    """Facade over AIRuleMapper — single entry point the pipeline uses for column mapping."""

    def __init__(self, model: Optional[str] = None):
        """
        Args:
            model: AI model name to use (e.g. 'gpt-4o', 'gpt-4o-mini').
                   Defaults to DIAL_MODEL env var, then 'gpt-4o'.
        """
        self._ai_mapper = AIRuleMapper(model=model)

    def set_model(self, model: str):
        """
        Switch the AI model at runtime.

        When Claude backend is active, model must be a Claude model name.
        set_model() re-reads env vars to preserve the correct backend.
        """
        import os
        from ai_transformation.ai_rule_mapper import _is_claude_model

        dial_key   = os.getenv("DIAL_API_KEY", "")
        claude_key = os.getenv("CLAUDE_API_KEY", "")

        # Guard: if Claude backend active but a non-Claude model was selected,
        # update CLAUDE_MODEL env var so AIRuleMapper picks the right model.
        if claude_key and not dial_key:
            if not _is_claude_model(model):
                print(
                    f"  [Orchestrator] ⚠️  '{model}' is not a Claude direct model name. "
                    f"Keeping current Claude model: {self._ai_mapper.model}"
                )
                return
            # Set env var so the new AIRuleMapper picks it up
            os.environ["CLAUDE_MODEL"] = model
        else:
            os.environ["DIAL_MODEL"] = model

        # Recreate mapper — it auto-detects backend from env vars
        self._ai_mapper = AIRuleMapper(model=model)

        print(f"  [Orchestrator] AI model set to '{self._ai_mapper.model}' [{self._ai_mapper._backend}].")

    @property
    def active_model(self) -> str:
        """Return the name of the currently active AI model."""
        return self._ai_mapper.model

    @property
    def is_ai_active(self) -> bool:
        """Return True if AI mapping is configured and available."""
        return self._ai_mapper._ai_active

    def map_columns(
        self,
        source_columns: List[ColumnMetadata],
        target_columns: List[ColumnMetadata],
        primary_key_hints: Optional[List[str]] = None,
        table_name: str = "unknown",
        source_database: str = "postgresql",
    ) -> Tuple[List[ColumnRuleMapping], str]:
        """
        Map source → target columns and assign validation rules.

        Args:
            source_columns    : Source column metadata
            target_columns    : Snowflake column metadata
            primary_key_hints : Known PK column names (informational)
            table_name        : Table name for logging and AI context
            source_database   : Source database type (default: postgresql)

        Returns:
            Tuple of (mappings, explanation).

        Raises:
            AIRuleMappingError: AI is unconfigured or the call failed.
        """
        if not self._ai_mapper._ai_active:
            raise AIRuleMappingError(
                f"No AI API key configured — cannot map columns for '{table_name}'.\n"
                "  Set one of the following in .env:\n"
                "    DIAL_API_KEY=...    (EPAM DIAL — access to GPT/Claude/Gemini)\n"
                "    CLAUDE_API_KEY=...  (Anthropic direct — no VPN needed)\n"
                "  Or run: python validate_cli.py  →  choose [8] Configure API key"
            )

        print(
            f"  [Orchestrator] Using AI mapper "
            f"(model: '{self._ai_mapper.model}') for '{table_name}'."
        )

        return self._ai_mapper.map_columns(
            source_columns, target_columns, primary_key_hints, table_name
        )
