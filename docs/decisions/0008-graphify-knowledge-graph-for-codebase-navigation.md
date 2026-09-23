# 0008. Use graphify to build a navigable knowledge graph of the repo

**Status:** Accepted
**Date:** 2026-09-23

## Context

This repo has accumulated a lot of look-alike pairs and drift that `CLAUDE.md`
already calls out by hand: two YAML-writing paths, five near-duplicate
exclusion YAMLs, `.claude/` skill mirrors that can go stale, and
docs (`docs/architecture/*.md`, several `docs/deployment/JIRA_*.md`,
`docs/deployment/local-setup.md`) that still reference the removed
`gemini_connector`/chatbot subsystem. Grepping finds *occurrences* of a name
but not *relationships* — which configs share data with which, which docs
cite which code, which two things solve the same problem without importing
each other. Answering "is X still true" by hand means re-reading the whole
tree every time.

## Decision

Run the `graphify` skill (`/graphify .`) against the repo to build a
persistent knowledge graph: structural AST extraction for the 111 code files
(free, deterministic, no LLM) plus subagent-based semantic extraction for the
102 doc/config files (concepts, rationale, cross-file references), merged
into `graphify-out/graph.json`, `GRAPH_REPORT.md`, and `graph.html`. Every
edge is tagged EXTRACTED / INFERRED / AMBIGUOUS by confidence, so a claim like
"these two exclusion YAMLs duplicate each other" is auditable back to a
specific file pair rather than asserted from memory. Re-run with `--update`
after future changes to re-extract only new/changed files instead of
rebuilding from scratch.

## Alternatives considered

- **Keep relying on `CLAUDE.md` + manual grep** — works for the drift already known and documented by hand, but doesn't scale to new drift (e.g. it took this graphify run to surface the `.github/agents/*.agent.md` mirrors and the CodeMie generic-assistant overlap as separate, unlinked findings).
- **A custom one-off script to cross-reference specific known pairs** (the 5 exclusion YAMLs, the two YAML-writer paths) — narrower, cheaper to run once, but a new script is needed for every new question; doesn't generalize to "what else looks like this."
- **Full static-analysis/dependency-graph tool (e.g. a Python-only import grapher)** — would cover code-to-code edges but misses the doc/config layer entirely, which is where most of this repo's actual drift (stale docs, near-duplicate YAMLs) lives.

## Consequences

- `graphify-out/` (graph.json, GRAPH_REPORT.md, graph.html, cost.json, manifest) is now a build artifact of this repo — not checked in as source of truth, regenerate with `/graphify --update` after significant changes rather than trusting a stale copy.
- Semantic extraction used general-purpose subagents (~566K input tokens for this run) since no `GEMINI_API_KEY`/`CLAUDE_API_KEY` was configured for direct extraction; a future run with a key set would be cheaper and faster.
- The first run already surfaced concrete findings worth acting on separately: the `rules_catalog.json` vs `base_rules.py` staleness `CLAUDE.md` already flags, and confirmed-stale `gemini_connector` naming in 5 docs — these are follow-up cleanup items, not covered by this decision.
- Graph health check flagged ~398 dangling-endpoint edges and ~90 collapsed duplicate edges from the semantic pass (subagent-produced node IDs not always matching the AST's exact ID format) — non-fatal, but means edge counts in the report should be read as directionally useful, not exact.
