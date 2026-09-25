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
| [0010](0010-hybrid-v1-tiered-runner-audit-no-front-door.md) | Audit: hybrid_v1 tiered runner is wired and tested, but has no front door to turn it on | Investigation only | 2026-09-23 |
| [0011](0011-hybrid-v1-front-door.md) | hybrid_v1 gets a front door — config/schema opt-in, no automatic row-count switch | Accepted | 2026-09-23 |
| [0012](0012-hybrid-v1-benchmark-plan-standard-vs-hybrid.md) | Standard vs. hybrid_v1 benchmark: investigation, gap report, and decisions needed | Investigation only | 2026-09-23 |
| [0013](0013-silver-layer-validation-strategy.md) | Silver-layer validation strategy: recompute Coalesce's declared transform against Bronze and diff against materialized Silver, instead of fuzzy-matching columns | Proposed | 2026-09-24 |
| [0014](0014-coalesce-metadata-extraction-rules.md) | Coalesce metadata extraction rules: column classification (passthrough / recomputable / macro-skip / non-deterministic-exclude), cross-node reference resolution, surrogate-key fallback | Proposed | 2026-09-24 |
| [0015](0015-silver-v1-schema-diff-gate-and-deferred-pk-resolution.md) | Silver v1 implementation: schema-diff-before-generation gate, exclude-or-raise-JIRA-bug UX, deferred surrogate-key macro-argument parsing | Accepted | 2026-09-24 |
| [0016](0016-coalesce-connector-and-silver-skill-added.md) | New Silver/Coalesce connector (`coalesce_client.py`, `coalesce_plan_builder.py`) and `silver-layer-coalesce-validation` skill added | Accepted | 2026-09-24 |
| [0017](0017-silver-layer-specialist-agent-added.md) | Dedicated `silver-layer-coalesce-specialist` agent added; orchestrator, backend, frontend, and DQE review agents updated to route Silver/Coalesce work to it | Accepted | 2026-09-24 |
| [0018](0018-silver-sql-verbatim-transform-no-generic-normalization.md) | Silver recompute SQL must use the Coalesce-declared transform verbatim, bypassing the generic Bronze normalization wrapper entirely (row-comparator is already NULL-safe in Python) | Accepted | 2026-09-24 |
| [0019](0019-silver-multisource-nodes-and-batch-node-ui.md) | Support `isMultisource=true` Coalesce nodes (multiple joined Bronze dependencies, `joinCondition` used verbatim) and a Streamlit batch-node UI for validating several Coalesce node IDs in one pass | Accepted | 2026-09-24 |
| [0020](0020-silver-fivetran-active-filter-open-question.md) | Whether Silver's Bronze-recompute SQL should filter `_FIVETRAN_ACTIVE = TRUE` — evidence shows it would break the one real SCD-shaped Silver node sample; needs a metadata/config-driven flag or Coalesce-team confirmation before any filter is added | Investigation only | 2026-09-24 |
| [0024](0024-bronze-column-schema-validation-gate.md) | Bronze "Review columns" step gets a data-type mismatch check plus a persisted "Mark OK" action (new `bronze_schema` exclusion key), extending `render_mapping_review()` instead of building a second Silver-style diff/gate | Accepted | 2026-09-25 |
