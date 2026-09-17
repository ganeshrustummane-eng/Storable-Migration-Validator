"""
AI Transformation Package
===========================
Maps source columns to Snowflake columns and assigns the correct validation
rule for each column pair.

AI-only mapping
---------------
  AIRuleMapper — DIAL/GPT-4o (or any configured model), or direct Anthropic
                 Claude when CLAUDE_API_KEY is set and DIAL_API_KEY is not.
                 The model is user-selectable at runtime.

  The former StaticRuleMapper (deterministic type-pair matching, used as an
  offline fallback) has been REMOVED. Its output was indistinguishable from a
  reviewed AI mapping, so a missing API key silently downgraded correctness
  without downgrading the reported confidence. Mapping now raises
  AIRuleMappingError instead of guessing.

  RuleMapperOrchestrator (the single-entry-point facade used by
  ValidationPipeline.run(), a 100%-AI mapping path with no exact/fuzzy
  fallback) has been REMOVED — the pipeline now exclusively uses
  run_with_plan(), which calls AIRuleMapper only for ambiguous columns via
  ai.rule_planner.RulePlanner. AIRuleMapper itself stays here because the
  webapp's model picker and rule-book "paste a rule" feature
  (rule_prompt_parser) still use it directly.

Available Models (DIAL)
-----------------------
  gpt-4o            ← default, best accuracy
  gpt-4o-mini       ← faster, lower cost
  gpt-4-turbo
  claude-3-5-sonnet (direct Claude models also available — see CLAUDE_DIRECT_MODELS)
  (any model on your DIAL endpoint)

Usage
-----
  AIRuleMapper no longer has a map_columns() entry point — that, plus its
  prompt builders, were removed once the only caller (RuleMapperOrchestrator)
  was gone. What's left is used two ways:

    from ai_transformation import AVAILABLE_MODELS
    print(AVAILABLE_MODELS)   # webapp model picker

    # rule_prompt_parser.py calls AIRuleMapper's low-level transport directly
    # (_call_dial / _call_claude) with its own prompt, for the "paste a rule"
    # feature in the Rule Book UI tab.
"""

from ai_transformation.column_mapping import ColumnRuleMapping
from ai_transformation.ai_rule_mapper import (
    AIRuleMapper,
    AIRuleMappingError,
    AVAILABLE_MODELS,
    MODEL_DESCRIPTIONS,
)

__all__ = [
    "ColumnRuleMapping",
    "AIRuleMapper",
    "AIRuleMappingError",
    "AVAILABLE_MODELS",
    "MODEL_DESCRIPTIONS",
]
