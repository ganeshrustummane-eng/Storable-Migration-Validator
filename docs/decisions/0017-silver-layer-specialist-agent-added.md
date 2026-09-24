# 0017. Dedicated Silver-layer/Coalesce specialist agent added; orchestrator, backend, frontend, and review agents updated to route to it

**Status:** Accepted
**Date:** 2026-09-24
**Follows on from:** [0013](0013-silver-layer-validation-strategy.md), [0014](0014-coalesce-metadata-extraction-rules.md), [0015](0015-silver-v1-schema-diff-gate-and-deferred-pk-resolution.md), [0016](0016-coalesce-connector-and-silver-skill-added.md)

## Context

Silver-layer validation (0013-0016) is ongoing work, not a one-off change —
the user is continuing to iterate on it (multi-table/multi-database batch
support, the unconfirmed Bronze-side schema assumption, recomputable-expression
normalization) across future sessions. Until now, Silver/Coalesce work would
have been picked up by `validation-query-yaml-generator` (generic SQL/YAML
backend) by default, which owns the *shared* `CanonicalValidationPlan`/generator
layer but has no specific knowledge of Coalesce's metadata shape, the 4-bucket
column classification, or the deferred surrogate-key heuristic — routing
Silver work there risks re-deriving decisions already made in 0013/0014/0015,
the same failure mode ADR 0009's `.github` mirror cleanup and ADR 0008's
`graphify` adoption were meant to prevent for docs.

## Decision

Added a fifth specialist agent, `.claude/agents/silver-layer-coalesce-specialist.md`,
scoped to exactly `src/connector/coalesce_client.py` and `src/silver/**`
(Coalesce node fetching, 4-bucket classification, `SchemaDiff`, business-key
resolution) — explicitly *not* the shared `validation_plan.py`/SQL/YAML
generator files, which stay owned by `validation-query-yaml-generator` so the
plan schema has one owner regardless of which layer produced the plan.

Updated the other three existing agents to know about it:

- `migration-validator-orchestrator` — added to "Available specialists," with
  routing guidance (Coalesce/Silver-node/schema-drift requests go to the new
  specialist first; only bring in `validation-query-yaml-generator` too if the
  shared plan schema itself needs a new field).
- `validation-query-yaml-generator` — noted that Silver shares its
  generator/schema layer via the new specialist's output, and that plan-schema
  changes prompted by Silver work are still this agent's to make.
- `streamlit-frontend-manager` — noted the Bronze/Silver radio and Silver
  sub-flow's backend is the new specialist, not the generic backend agent.
- `migration-validator-dqe-review` — added `src/connector/coalesce_client.py`
  and `src/silver/**` to its reviewed scope, with a pointer to 0013-0016 so it
  doesn't flag documented deferrals (no macro interpreter, no batch mode yet)
  as undiscovered gaps.

## Alternatives considered

- **Keep routing Silver work through `validation-query-yaml-generator` alone.**
  Rejected — that agent's description and constraints are written around the
  Bronze fuzzy/AI-matching problem; a Coalesce-metadata extraction task doesn't
  fit its "extend `CanonicalValidationPlan`" framing without first explaining
  the whole 0013/0014 backstory in every prompt, which is exactly the
  restated-context tax a dedicated agent avoids.
- **Merge Silver ownership into `streamlit-frontend-manager` since the only
  current caller is the UI.** Rejected — the backend logic (`coalesce_plan_builder.py`)
  has no UI dependency and will grow (batch mode, macro-argument parsing) well
  beyond what a frontend-scoped agent should touch.

## Consequences

- Gets easier: future Silver/Coalesce requests (batch support, second sample
  node, schema-assumption fix) get a specialist that already knows the 4-bucket
  rules and the deferred-heuristic history, without re-reading 0013-0016 into
  context via the orchestrator's generic backend agent each time.
- Gets harder: one more agent file to keep in sync if `CanonicalValidationPlan`'s
  shared schema changes — the boundary (specialist owns `src/silver/**` and the
  connector; `validation-query-yaml-generator` owns the shared plan/generator
  files) needs to hold, or duplicate ownership drifts the same way the 5
  exclusion YAMLs already have.
- Watch for: this ADR does not itself change any runtime behavior — it's
  agent-routing metadata only. If a future session finds the specialist
  boundary doesn't hold in practice (e.g. Silver needs to touch
  `sql_query_generator.py` routinely), revisit the split rather than letting
  the specialist quietly grow into the shared files.
