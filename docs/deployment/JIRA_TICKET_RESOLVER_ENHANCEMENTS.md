# Jira Ticket Resolver — Enhancements (My Jira Tickets tab)

Date: 2026-09-18
Author: Ganesh Rustum Mane
Scope: `webapp/app.py` — "🎫 My Jira Tickets" tab only. No other files touched.

## Why

Feedback from the data quality team on the existing "My Jira Tickets" tab:

1. It only showed summary/status/priority — not enough detail to understand what the actual issue is without leaving the app and opening Jira.
2. No help resolving the ticket — the person picking it up (often not the one who raised it) had to work out the root cause from scratch.
3. The "attach validation result" comment wasn't actually attaching a result — it posted a config file's path as text, not the outcome of a run.

## What changed

### 1. Full ticket detail on expand

`src/connector/jira_client.py` already had `get_ticket(key)` returning description, assignee, created/updated — it just wasn't being called from the tab (only `get_my_tickets()`'s summary/status/priority were shown). The tab now calls `get_ticket()` when a ticket is expanded and displays the full description alongside assignee and dates. Cached per ticket key in `st.session_state` so it isn't re-fetched from Jira on every widget interaction/rerun.

### 2. "🤖 Suggest a fix" button

Each ticket now has a button that asks the existing Migration Intelligence agent (`src/connector/agent.py`'s `create_agent()` / `agent.chat()` — same one behind the Chat tab) to diagnose the ticket and suggest resolution steps.

**Why `agent.chat()` instead of a new AI call:** the agent already has tool-calling wired to `get_jira_ticket_status`, `get_validation_failures`, `get_applicable_rules`, and `get_rule` (all in `src/connector/tools.py`, registered in `agent.py`'s `_OPENAI_TOOLS`). Building a second AI path just for this button would duplicate that integration — exactly what `CLAUDE.md`'s "no redundant implementations" rule warns against. Instead, the button sends one prompt naming the ticket key, and the agent's own tool loop looks up the ticket, infers the table, and pulls the rule-book/failure-analysis context itself.

**Data boundary respected:** `get_validation_failures` and `get_applicable_rules`/`get_rule` return aggregate failure analysis and rule metadata only — column names, mismatch counts, likely cause, recommended action — never raw row-level values. This matches the existing rule already stated in the Execute Validation tab ("Local files only, never sent to any AI/LLM"). If the agent can't find a matching table or validation run for the ticket, it's instructed to say so rather than guess.

### 3. "Attach validation result" now attaches an actual result

Previously: the picker listed *config* YAMLs (`config/**/data_validation|count_validation/*.yaml` — the rule/mapping definitions) and the button posted the picked file's path as the entire comment. That's not a result at all.

Now: the picker lists recorded validation **runs** from `Project/results_store.py` (`query_runs()` / `query_results()` — the same SQLite-backed history already used by the History tab, table-level counts only, never row-level values). Posting the comment computes and includes:
- total checks / passed / failed for the run
- per validation type (`count_validation`, `data_validation`): tables checked, tables failed, and which tables failed (names only)

So a developer reading the ticket in Jira sees the real outcome without opening the app.

## What was intentionally *not* changed

- The self-attested "Corporate email / user ID" free-text identity field (used for the audit trail and now also for Jira actions) — flagged separately as a known gap (no verification against `.env`/SSO). Per team decision, left as-is for now; real fix (SSO-backed identity) is a larger follow-up, not bundled into this change.
- No new AI backend integration, no new config knobs — reused `agent.chat()` and `results_store` as they already existed.

## Files touched

- `webapp/app.py` — "My Jira Tickets" tab only (ticket detail block, new "Suggest a fix" expander, rewritten "Attach validation result" expander).

No changes to `src/connector/jira_client.py`, `src/connector/tools.py`, `src/connector/agent.py`, or `Project/results_store.py` — all reused as-is.
