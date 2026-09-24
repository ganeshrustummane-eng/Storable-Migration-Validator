# 0016. New Silver/Coalesce connector and skill added

**Status:** Accepted
**Date:** 2026-09-24
**Follows on from:** [0013](0013-silver-layer-validation-strategy.md), [0014](0014-coalesce-metadata-extraction-rules.md), [0015](0015-silver-v1-schema-diff-gate-and-deferred-pk-resolution.md)

## Context

This session added a genuinely new subsystem (Coalesce metadata extraction
for Silver-layer validation) rather than extending an existing one. Per the
precedent set in ADR 0008/0009 (documenting `graphify` and the `.github`
mirror cleanup as their own decisions), a new connector plus a new skill
directory is worth its own decision record so later agents can find why
these files exist without re-deriving it from the code alone.

## Decision

Two new pieces of source were added, both read-only, both following the
existing `src/connector/jira_client.py` client shape:

- `src/connector/coalesce_client.py` — thin Coalesce.io REST client.
  `get_node(workspace_id, node_id)` is the only function; no write
  operations, no general Coalesce SDK.
- `src/silver/coalesce_plan_builder.py` — the extractor that turns one
  Coalesce node's metadata into a `CanonicalValidationPlan` plus a
  `SchemaDiff`. Public entrypoint: `build_plan(node_id, workspace_id=None)`.

A new skill, `.claude/skills/silver-layer-coalesce-validation/SKILL.md`, was
added to the skills index (`.claude/skills/`) to describe this subsystem for
future agents, matching the frontmatter/body shape of the existing
`reference-filter-joins` skill.

## Alternatives considered

- **Fold Coalesce extraction into the existing bronze mapping pipeline
  (`src/validation_pipeline.py`).** Rejected — ADR 0013 already decided
  Silver's mapping problem is structurally different (given, not
  discovered); mixing the two pipelines would reintroduce the fuzzy/AI
  matching path ADR 0013 explicitly bypasses for Silver.
- **Skip a dedicated skill file and rely on CLAUDE.md alone.** Rejected —
  CLAUDE.md's skills index exists precisely so an agent can find the
  detailed playbook for a subsystem without re-reading the whole ADR set;
  Silver/Coalesce is different enough from bronze's reference-filter-joins
  playbook to warrant its own entry.

## Consequences

- Gets easier: future agents working on Silver validation have one skill
  file and two ADRs (0013/0014, plus this one and 0015) to read instead of
  re-deriving the Coalesce metadata shape from scratch.
- Gets harder: nothing new — both files are additive and read-only; no
  existing bronze code path was changed.
- Watch for: the webapp's own Silver sub-flow (the UI call sites for
  `build_plan()`, the schema-diff gate, and the exclude/JIRA buttons) is
  explicitly out of scope for this ADR and this session's backend work —
  see the skill file's placeholder section for what the frontend agent
  still needs to wire up.
