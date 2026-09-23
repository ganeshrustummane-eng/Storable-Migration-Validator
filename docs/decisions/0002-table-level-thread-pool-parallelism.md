# 0002. Parallelize table-level validation with a bounded ThreadPoolExecutor

**Status:** Accepted
**Date:** 2026-09-23

## Context

`Project/main.py` validated tables strictly one at a time. A batch run of many
tables paid the full sequential sum of every table's fetch+compare time, even
though tables are independent — separate DB connections, separate output
files. No parallelism existed anywhere in `Project/` before this
(`concurrent.futures`/`multiprocessing`/`threading` were unused).

## Decision

Run tables within the same yaml/validation batch concurrently on a bounded
`concurrent.futures.ThreadPoolExecutor` (`main.py`'s `MAX_TABLE_WORKERS`,
default 4, overridable via `VALIDATOR_MAX_TABLE_WORKERS`). Threads (not
processes) because each table's work is I/O-bound (waiting on source/Snowflake
network round-trips), not CPU-bound — the GIL is released during those waits.

The per-table validation body was extracted into `_validate_table(table_name,
table_config)`, returning `(local_failure_count, local_system_error)` instead
of mutating module-level counters directly, since `+=` from multiple threads
isn't atomic. The two places that append to a **file shared across every
table in a run** — `create_summary()`'s `{validation_type}_summary.csv`
(`Project/utils/utility.py`) and `append_validation_audit()`'s
`validation_audit.jsonl` (`Project/utils/quality_checks.py`) — got a
`threading.Lock()` around their check-exists/open/write so concurrent tables
can't interleave writes or double-write a header.

## Alternatives considered

- **`multiprocessing`/process pool** — would work too, but processes can't share the in-memory locks above; each table's own DB connections/output files already avoid cross-process state, but the summary/audit files would need file-locking instead of a `threading.Lock`, for no real benefit since the bottleneck here is network I/O, not CPU.
- **Rewrite as `asyncio`** — bigger surface change across every `Project/db/*.py` connector (each would need an async driver), for the same I/O-bound win threads already give us for free.
- **Parallelize source+target fetch within one table too** — real, smaller win (roughly halves fetch-stage wall clock per table), deliberately deferred as a fast-follow; this decision covers table-level parallelism only.

## Consequences

- Batch runs of N tables get up to `MAX_TABLE_WORKERS`x wall-clock improvement, bounded by how many concurrent connections the source DBs/Snowflake can actually take — that's why it's a small bounded pool (4), not "one thread per table."
- `processed_tables`/`_warned_stale_exclusions` mutations now go through `_state_lock`; a stale-exclusions warning could in principle be skipped by a losing thread on a first-seen race, which is a duplicate-suppression heuristic already, so this doesn't change correctness, only (harmlessly) timing of the dedup.
- Failure in one table's thread is caught and converted to a counted failure + `system_error=True` rather than crashing the whole run (mirrors the existing per-table `try/except` behavior, just at the `future.result()` boundary too).
- Added `Project/utils/test_utility_checks.py::test_create_summary_thread_safe_concurrent_writes` as the regression guard for the lock.
