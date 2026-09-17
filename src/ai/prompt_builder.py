"""
Prompt Builder
===============
Builds minimal, focused prompts for AI-assisted column matching.

Token-efficiency principle: send ONLY what the AI needs for this specific
ambiguous column. Do NOT send the full schema or full rule book.

For each ambiguous source column the prompt includes:
  1. The source column (name, normalized name, type)
  2. Top N fuzzy candidate target columns (not the whole schema)
  3. Relevant transformation rules (filtered to plausible pairs)
  4. Relevant learned examples (filtered by source/target type)
  5. Relevant validation rule definitions (brief, not the full catalog)

The AI must return a strict JSON response (no markdown).

System prompt enforces:
  - Read-only reasoning (no SQL execution, no data modification)
  - Structured JSON output
  - No invented columns (must choose from provided candidates)
  - Evidence-based reasoning
  - Explicit confidence score
  - Explicit reason
  - Return "ambiguous" status when evidence is insufficient
"""

import json
from typing import List, Optional, Tuple

from sql_extractor.extractors import ColumnMetadata
from matching.candidate_matcher import MatchDecision
from matching.fuzzy_matcher import FuzzyCandidate
from matching.normalizer import normalize_column_name
from rule_book import rule_book

# ---------------------------------------------------------------------------
# Transformation rules summary — sent to AI for ambiguous columns only.
#
# This used to be a hand-typed markdown table here, kept in sync by hand with
# rules_catalog.json and the AI-SQL-generation prompt in
# generated_queries/ai_sql_generator.py. It drifted (it was missing hstore for
# a while) and never reflected learned rules from the Rule Book UI tab. Now it
# reads from rule_book.build_prompt_block() instead — same base+learned rules
# your SQL generation already trusts, filtered to only the type pairs this
# specific table actually has (see build_system_prompt below), so adding a
# rule to rules_catalog.json or activating a learned rule is picked up here
# automatically, with nothing to hand-edit.
# ---------------------------------------------------------------------------

_TIMESTAMP_NOTE = (
    "\nIMPORTANT: timestamp → VARCHAR is a VALID migration transformation, not an error.\n"
    "Use rule \"text\" for timestamp→VARCHAR (Fivetran may convert timestamps to strings).\n"
)


class PromptBuilder:
    """
    Builds focused, token-efficient prompts for ambiguous column resolution.

    Usage
    -----
        builder = PromptBuilder()
        system_prompt = builder.build_system_prompt()
        user_prompt   = builder.build_user_prompt(
            source_col=col,
            candidates=top_candidates,
            learned_examples=relevant_examples,
            table_name="orders",
        )
    """

    def build_system_prompt(
        self,
        type_pairs: Optional[List[Tuple[str, str]]] = None,
        source_label: str = "PostgreSQL",
    ) -> str:
        """
        Build the system prompt that enforces all AI constraints.

        This prompt is sent once per API call and establishes:
          - Read-only reasoning constraint
          - Structured JSON output contract
          - No invented columns or transformations
          - Evidence-based decision making

        Args:
            type_pairs   : (source_type, target_type) pairs actually present
                           among this batch's ambiguous columns/candidates.
                           Passed to rule_book.build_prompt_block() so the
                           rules block stays small and relevant instead of
                           dumping the whole catalog. None = full catalog.
            source_label : Which source system these types came from, for
                           the rules block's header (e.g. "MSSQL", "Athena").
        """
        rules_block = rule_book.build_prompt_block(
            type_pairs=type_pairs, source_label=source_label
        )
        return (
            "You are a Senior Data Migration QA Engineer specialising in "
            "data migration validation.\n\n"
            "Your task: resolve an ambiguous column mapping. "
            "You will receive one source column and a ranked list of candidate "
            "target columns. Choose the BEST match or return 'ambiguous' if "
            "the evidence is insufficient.\n\n"
            f"{rules_block}\n"
            f"{_TIMESTAMP_NOTE}\n"
            "## STRICT CONSTRAINTS (enforce all):\n"
            "1. CHOOSE from the provided candidates only — never invent column names.\n"
            "2. Return ONLY valid JSON — no markdown, no explanation outside the JSON.\n"
            "3. Assign ONE transformation rule from the table above.\n"
            "4. Return confidence as a float 0.0–1.0.\n"
            "5. If evidence is genuinely insufficient, set status='ambiguous' and "
            "   target_column to the best guess but flag it.\n"
            "6. Never execute SQL, modify data, or invent transformations.\n"
            "7. Prefer exact evidence over fuzzy evidence over semantic inference.\n\n"
            "## OUTPUT CONTRACT (JSON only):\n"
            "{\n"
            '  "status": "resolved" | "ambiguous",\n'
            '  "source_column": "<source_name>",\n'
            '  "target_column": "<chosen_target_name_from_candidates>",\n'
            '  "source_type": "<source_type>",\n'
            '  "target_type": "<target_type>",\n'
            '  "transformation_rule": "<rule_id>",\n'
            '  "confidence": 0.0,\n'
            '  "reason": "<one sentence explaining the decision>"\n'
            "}"
        )

    def build_user_prompt(
        self,
        source_col: ColumnMetadata,
        candidates: List[FuzzyCandidate],
        learned_examples: Optional[List[dict]] = None,
        table_name: str = "unknown",
        top_n: int = 5,
    ) -> str:
        """
        Build the minimal user prompt for one ambiguous source column.

        Sends ONLY:
          - The source column (name, normalized name, type, nullable)
          - Top N fuzzy candidates (sorted by score)
          - Relevant learned examples (filtered, not the full list)

        Does NOT send:
          - The full source schema
          - The full target schema
          - The full rule book
          - Unrelated learned examples

        Args:
            source_col       : The ambiguous source column
            candidates       : Ranked fuzzy candidates (best first)
            learned_examples : Optional filtered learned examples
            table_name       : Table name for context
            top_n            : Maximum candidates to include in prompt

        Returns:
            Focused user prompt string.
        """
        src_norm = normalize_column_name(source_col.column_name)

        src_block = (
            f"Source column:\n"
            f"  name            : {source_col.column_name}\n"
            f"  normalized_name : {src_norm}\n"
            f"  type            : {source_col.data_type}\n"
            f"  nullable        : {source_col.is_nullable}\n"
            f"  ordinal_position: {source_col.ordinal_position}\n"
        )

        # Top N candidates
        cand_lines = []
        for i, cand in enumerate(candidates[:top_n], 1):
            tgt = cand.target_col
            tgt_norm = normalize_column_name(tgt.column_name)
            cand_lines.append(
                f"  [{i}] name={tgt.column_name}  normalized={tgt_norm}  "
                f"type={tgt.data_type}  nullable={tgt.is_nullable}  "
                f"position={tgt.ordinal_position}  "
                f"fuzzy_score={cand.fuzzy_score:.3f}"
            )

        cand_block = "Ranked target candidates (best fuzzy match first):\n"
        if cand_lines:
            cand_block += "\n".join(cand_lines)
        else:
            cand_block += "  (no candidates above threshold)"

        # Relevant learned examples
        learned_block = ""
        if learned_examples:
            relevant = _filter_learned(
                source_col.column_name,
                source_col.data_type,
                [c.target_col.data_type for c in candidates[:top_n]],
                learned_examples,
            )
            if relevant:
                learned_block = "\nRelevant learned examples from previous corrections:\n"
                for ex in relevant[:3]:
                    learned_block += (
                        f"  • src={ex.get('source_column')} ({ex.get('source_type')}) "
                        f"→ tgt={ex.get('target_column')} ({ex.get('target_type')}) "
                        f"| rule={ex.get('correct_rule', ex.get('rule', 'unknown'))} "
                        f"| reason={ex.get('reason', '')}\n"
                    )

        task_line = (
            f"Table: {table_name}\n"
            f"Task: Match the source column to the BEST candidate and assign the correct rule.\n\n"
        )

        return task_line + src_block + "\n" + cand_block + learned_block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _filter_learned(
    src_name: str,
    src_type: str,
    tgt_types: List[str],
    learned: List[dict],
) -> List[dict]:
    """
    Return only learned examples relevant to this source column.
    Relevance criteria (any match):
      - source column name matches (case-insensitive)
      - source type matches
      - target type matches any candidate
    """
    src_upper = src_name.upper()
    src_type_upper = src_type.upper()
    tgt_types_upper = {t.upper() for t in tgt_types}

    result = []
    for ex in learned:
        ex_src_name = ex.get("source_column", "").upper()
        ex_src_type = ex.get("source_type", "").upper()
        ex_tgt_type = ex.get("target_type", "").upper()

        if (
            ex_src_name == src_upper
            or ex_src_type == src_type_upper
            or ex_tgt_type in tgt_types_upper
        ):
            result.append(ex)

    return result
