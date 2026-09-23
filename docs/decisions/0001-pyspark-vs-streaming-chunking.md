# 0001. Use streaming/chunked reads + hash-narrowing instead of a PySpark cluster

**Status:** Accepted
**Date:** 2026-09-23

## Context

At 200-300M rows, the validator can't hold a full source table + full target
table + full result in Python memory at once (see
`docs/performance-audit-large-tables/README.md`). Management asked whether
PySpark's distributed/parallel processing was the fix.

## Decision

Don't introduce a Spark cluster. Instead: stream reads in bounded chunks
(`execute_query_stream` per connector) and use a cheap hash pre-filter
(Tier 1) to narrow the expensive, exact Python comparison (Tier 2) down to
only the rows that actually disagree. Full design in
[`../large-table-scalable-architecture/README.md`](../large-table-scalable-architecture/README.md);
plain-language version in
[`../large-table-scalable-architecture/pyspark-vs-streaming-explainer.md`](../large-table-scalable-architecture/pyspark-vs-streaming-explainer.md).

## Alternatives considered

- **Rewrite in PySpark** — needs new cluster infrastructure we don't run today, and would require re-proving every existing correctness rule (duplicate keys, composite/PK-less handling, JSON/hstore canonicalization) in Spark's dataframe API instead of reusing it.
- **Chunked fetch only, no hash-narrowing** — fixes the fetch-stage memory bottleneck but still moves 100% of row bytes over the network and through the compare stage; doesn't reach the 200-300M target on its own.
- **Database-side hashing as the final PASS/FAIL answer** — rejected: the current row_hash is computed *after* Python-side JSON/decimal canonicalization, which no single SQL dialect reproduces identically across all four sources. Used only as a pre-filter (safe direction: worst case is extra Tier-2 work, never a missed mismatch).

## Consequences

- No new infrastructure to provision, monitor, or pay for.
- Reuses the existing, already-correct comparison code (`Project/main.py`'s canonicalization + multiset compare) unchanged for the narrowed mismatch set — this is the actual load-bearing correctness logic, and Spark would have meant rebuilding it.
- Doesn't speed up per-row Python CPU work directly — see [0002](0002-table-level-thread-pool-parallelism.md) for that.
- Full quality-check parity for hybrid_v1 (null-rate/distinct-count/aggregate checks) is still open — tracked in the architecture doc's §P/§R.
