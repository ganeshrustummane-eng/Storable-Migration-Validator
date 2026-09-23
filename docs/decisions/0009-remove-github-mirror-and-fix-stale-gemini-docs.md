# 0009. Remove `.github/` agent mirror; correct docs that still describe the removed chatbot/Gemini layer

**Status:** Accepted
**Date:** 2026-09-23

## Context

Two separate pieces of drift surfaced while exploring the repo with graphify
([0008](0008-graphify-knowledge-graph-for-codebase-navigation.md)):

1. `.github/agents/*.agent.md` and `.github/skills/reference-filter-joins/SKILL.md`
   were Copilot-format mirrors of the `.claude/` agents/skills. `CLAUDE.md`
   already warned these could drift ("confirmed divergence found once
   already") and said `.claude/` is authoritative — the mirror was
   maintenance overhead with no consumer once Copilot's format wasn't in
   active use.
2. Graphify's semantic extraction flagged ~10 docs as still saying
   `src/gemini_connector` where `CLAUDE.md` says the module was renamed to
   `src/connector`. Investigating turned out to be bigger than a rename:
   `git log` shows the "removed chat bot" commit (2026-09-22, `b457146`)
   deleted `src/connector/agent.py`, `api.py`, `tools.py`,
   `approval_store.py`, `audit.py`, `authz.py`, `auth.py`, `version_store.py`,
   `metrics.py`, `a2a.py` — the entire FastAPI/agent/approval/audit-log
   layer. Only `jira_client.py` and `__init__.py` are left. `CLAUDE.md`
   itself still asserted "approval store, audit log all live here and are
   real, in-use features," which was no longer true.

## Decision

- Deleted `.github/agents/*.agent.md` and `.github/skills/reference-filter-joins/SKILL.md` outright (already done in the working tree, committed as part of this cleanup) — `.claude/` remains the single source.
- Corrected `CLAUDE.md`'s "Agentic/chat layer" bullet to state plainly that only `jira_client.py` is live in `src/connector/`, and that it's called directly from `webapp/app.py`, not through an agent.
- For docs where the removed-subsystem content was small and mechanical (a code path, an env var row, a broken doc link), fixed it directly: `docs/deployment/jira-integration.md`'s `jira_client` import path, `docs/deployment/environment.md`'s dead `GOOGLE_API_KEY`/`GEMINI_API_KEY` rows, `docs/deployment/local-setup.md`'s "Start the Connector Server" step and Common Issues rows, and `README.md`'s architecture diagram, supported-systems table, quickstart, repository-structure tree, and documentation index (which linked to four docs that don't exist: `gemini-integration.md`, `connector-tools.md`, `authorization.md`, `review-workflow.md`, `audit-trail.md`, `gcp-deployment.md`).
- For docs where the removed subsystem *is* the doc's subject (`docs/architecture/system-architecture.md`, `docs/architecture/security-architecture.md`, `docs/deployment/JIRA_IMPLEMENTATION_SUMMARY.md`, `docs/deployment/JIRA_TICKET_RESOLVER_ENHANCEMENTS.md`) or a large self-contained section of it (`docs/deployment/jira-integration.md`'s "Use Cases" through "Metrics & Reporting", `docs/architecture/data-flow.md`'s persistence-file-map rows) — added a dated stale-content notice instead of rewriting, so the historical content stays intact but a reader can't mistake it for current.

## Alternatives considered

- **Rewrite every flagged doc in full** — more accurate long-term, but a much larger diff for content whose main value now is historical record of what the chatbot layer looked like; risks introducing new errors in sections the user hasn't reviewed.
- **Delete the fully-stale docs outright** (`JIRA_IMPLEMENTATION_SUMMARY.md`, `security-architecture.md`, the dead half of `system-architecture.md`) — rejected per `CLAUDE.md`'s own convention of moving removed things to `trash/` rather than deleting outright "in case something's needed later"; a banner achieves the same "don't trust this as current" outcome without losing the record.
- **Leave `.github/` in place, just stop updating it** — rejected; an un-updated mirror is worse than no mirror, since it looks live but silently drifts.

## Consequences

- `README.md`, `docs/deployment/local-setup.md`, `docs/deployment/environment.md`, and `docs/deployment/jira-integration.md` no longer tell a new developer to run `start_connector.py` or set a Gemini key that does nothing.
- Six dead links removed from `README.md`'s documentation index.
- `CLAUDE.md` now accurately scopes `src/connector/` to just JIRA — any future agent reading it won't assume an approval-store/audit-log/authz layer exists to build on.
- The banner'd docs (`system-architecture.md`, `security-architecture.md`, `JIRA_IMPLEMENTATION_SUMMARY.md`, `JIRA_TICKET_RESOLVER_ENHANCEMENTS.md`, and the bannered sections of `jira-integration.md`/`data-flow.md`) still contain detailed, now-explicitly-historical descriptions of the removed chatbot/agent/RBAC/audit design — worth a follow-up decision on whether to move them to `trash/docs/` or an `archive/` folder rather than leaving them in the live `docs/` tree indefinitely.
- Not done in this pass: a full read-through of every doc in the repo for other unrelated staleness (e.g. `verify_jira_config.py` is referenced in `docs/deployment/local-setup.md` and `docs/deployment/jira-integration.md` but doesn't exist on disk — flagged here, not fixed, since it wasn't part of the Gemini/chatbot drift this decision covers).
