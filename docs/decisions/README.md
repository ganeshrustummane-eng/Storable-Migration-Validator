# Decision Log

One file per decision, numbered in order made. Read this index to see what's
been decided without re-reading the full discussion each time.

Template for a new entry — copy `TEMPLATE.md`, name it `NNNN-short-slug.md`
(next number after the highest one here), fill it in, and add a row below.

| # | Decision | Status | Date |
|---|---|---|---|
| [0001](0001-pyspark-vs-streaming-chunking.md) | Use streaming/chunked reads + hash-narrowing (Tier1/Tier2) instead of a PySpark cluster for large-table validation | Accepted | 2026-09-23 |
| [0002](0002-table-level-thread-pool-parallelism.md) | Parallelize table-level validation in `main.py` with a bounded `ThreadPoolExecutor` | Accepted | 2026-09-23 |
| [0003](0003-ai-usage-yaml-generation-vs-run-validation.md) | Where AI is actually used (YAML generation, mandatory) vs. not (Run Validation, zero AI) | Investigation — awaiting your decision | 2026-09-23 |
| [0004](0004-run-validation-hang-subprocess-pipe-deadlock.md) | Fix: Run Validation stuck on "Running..." forever — subprocess pipe deadlock introduced by the Stop-button change | Fixed | 2026-09-23 |
| [0005](0005-run-validation-slow-full-page-rerun-polling.md) | Fix: Run Validation taking minutes instead of seconds — poll loop was triggering full-page Streamlit reruns, not scoped ones | Fixed, not yet browser-verified | 2026-09-23 |
| [0006](0006-run-validation-results-never-rendered-misindented-block.md) | Fix: Run Validation finished and wrote CSVs but UI stayed on "Running..." forever — results-rendering block was misindented into the wrong branch during the 0005 fragment splice | Fixed, not yet browser-verified | 2026-09-23 |
| [0007](0007-false-positive-failure-multi-source-config-directory.md) | Fix: "Raw stdout — non-zero exit" shown even though every table passed — `main.py` counted "table not in this one source's config file" as a failure instead of only "table validated by nobody" | Fixed | 2026-09-23 |
| [0008](0008-graphify-knowledge-graph-for-codebase-navigation.md) | Use `graphify` to build a navigable knowledge graph (AST + semantic extraction) of the repo instead of relying on manual grep/CLAUDE.md upkeep alone | Accepted | 2026-09-23 |
| [0009](0009-remove-github-mirror-and-fix-stale-gemini-docs.md) | Remove `.github/` agent mirror; fix/banner docs still describing the removed chatbot/Gemini/approval-audit layer that graphify flagged as stale | Accepted | 2026-09-23 |
