# 0012. Standard vs. hybrid_v1 benchmark: investigation, gap report, and decisions needed

**Status:** Investigation only — no code changed, no benchmark run
**Date:** 2026-09-23
**Follows on from:** [0010](0010-hybrid-v1-tiered-runner-audit-no-front-door.md) (audit), [0011](0011-hybrid-v1-front-door.md) (front door)

## What you asked

With the `hybrid_v1` front door in place (0011), you asked for a read-only
investigation of the existing benchmark/test infrastructure against the §L
benchmark ladder already documented in
`docs/large-table-scalable-architecture/README.md`, to determine — with real
measurements, not assumptions — when `hybrid_v1` becomes more efficient than
the standard (`Project/main.py`) path. Explicitly: no threshold invented, no
production code changed, no expensive 200M/300M-row run started, until the
plan, required environment, and cost are reported and agreed.

## What was checked

`docs/large-table-scalable-architecture/README.md` §K (differential
correctness plan) and §L (benchmark plan); `Project/test_tiered_runner.py`;
`Project/test_hybrid_dispatch.py`; §T.7/T.8 (the one live connector run
performed to date); `Project/main.py`'s and `Project/tiered_runner.py`'s
timing code; every connector's `execute_query_stream` (`Project/db/*.py`);
`requirements.txt`; `docker-compose.yml` and the `tests/postgres/init` path
it references; `.env.example`'s `SRC_*` blocks; repo-wide search for any
data-generator, seed script, memory-profiling, or query-history/warehouse-
credit instrumentation.

## Findings — what already exists

- **Differential-correctness harness (§K), real and passing**:
  `Project/test_tiered_runner.py` — a fake in-memory DB (`_FakeDB`, duck-typed
  `execute_query`/`execute_query_stream`) runs both `compare_indexed_frames`
  (oracle logic) and `tiered_runner.run_table_hybrid` against the same small
  pandas fixture and asserts identical `(row_key, status)` sets. 38 tests,
  all passing. Proves *correctness equivalence* at small scale — not *scale
  behavior*.
- **One live connector run (§T.7)**: a 12-row table (`semistructured_demo`)
  run once against real controlled Postgres + Snowflake instances (`.env`'s
  `local` environment, `migration_demo` DB). Found and fixed 5 real bugs
  (Snowflake quoted-identifier handling, MD5-vs-pgcrypto, canonicalization
  ordering). Valuable for correctness; tells us nothing about the 1M–300M
  ladder.
- **Streaming fetch primitive, production-ready**: every connector
  (`Project/db/{postgres,mssqlserver,snowflake,athena}.py`) implements
  `execute_query_stream(query, chunksize=50_000)`, already used by Tier 1.
  This is the one piece of §L's infra that's genuinely ready for a real
  ladder run.
- **Per-table wall-clock, coarse**: `Project/main.py` records one
  `batch_start_time`/`batch_end_time` pair per table, truncated to
  `%H:%M:%S` (second resolution, no phase split). `Project/tiered_runner.py`
  has **zero** timing instrumentation — no Tier 1 vs. Tier 2 split at all.

## Findings — what's missing (the performance half of §L, entirely)

| §L metric | Status |
|---|---|
| Phase-separated wall-clock (fetch / compare / write) | Not implemented on either path |
| Peak Python RSS | No `resource`/`psutil`/`tracemalloc` anywhere in the repo; `psutil` is not even a dependency (`requirements.txt`) |
| DB-side CPU / warehouse credits / bytes scanned | No Snowflake `QUERY_HISTORY` lookup, no Postgres server-side stat capture anywhere in code |
| Network bytes transferred | No packet capture or connector-level byte counters |
| Query count / chunk count issued per tier | No counters — connectors don't log how many `execute_query_stream` batches ran |
| Result-file size, rows/sec | Trivially derivable from existing `to_csv` output + timing above, but nothing computes it today |
| Failure/retry injection at each rung | No fault-injection harness exists |
| **1M–300M-row synthetic dataset** | **Does not exist.** No data generator, no seed SQL, no Faker usage anywhere in the repo. `docker-compose.yml` references a Postgres init directory (`./tests/postgres/init`) that is not present in the repo — the `migration_demo`/`semistructured_demo` controlled instances were evidently set up by hand, out-of-band, and are not reproducible from a fresh checkout. There is no Snowflake equivalent of the docker-compose container at all — Snowflake can't be containerized; it needs a real warehouse. |

**Conclusion:** the correctness half of the hybrid_v1 work (§K) has real,
checked-in infrastructure and passing tests. The performance half (§L) is
**100% unimplemented** — exactly what the source doc calls it: "the plan to
produce a number," not a runnable benchmark, today.

## Decision

Do not invent a threshold, and do not attempt a 200M/300M-row run against
this gap as it stands. Instead, close the gap in cheap, reviewable stages,
each one producing real numbers before committing to the next and more
expensive one:

1. **Instrumentation only (code, no data yet)** — add phase timers
   (fetch/compare/write) to `main.py`, and Tier 1/Tier 2 timers plus
   batch/query counters to `tiered_runner.py`. Stdlib `time.perf_counter()`,
   no new dependency. Small, mechanical, independently reviewable diff —
   this alone unblocks measuring rows/sec and query counts at *any* scale,
   including the existing 12-row live run.
2. **A synthetic data generator + seed path**, parameterized by row count,
   shaped like `semistructured_demo` (needs at least one JSON/HStore column
   and one PK column, since those are the cases §K/§L both care about).
   Postgres side: a `generate_series` + `INSERT`/`COPY` script, stdlib +
   `psycopg2` only. Snowflake side: bulk `COPY INTO` from the same generated
   rows (or via Fivetran if the benchmark should also exercise the real
   migration path — more expensive, and arguably out of scope since this
   repo validates migration, it doesn't perform it).
3. **Memory sampling** — `resource.getrusage(RUSAGE_SELF).ru_maxrss`
   (stdlib) or `psutil` (new dependency, needs your sign-off) sampled
   through the run, not just observed after the fact, per §L's explicit
   instruction.
4. **Run rungs 1M → 10M → 50M first**, oracle vs. candidate, against
   existing controlled Postgres/Snowflake instances. Stop and report actual
   numbers before going further — §L itself expects the oracle to start
   struggling somewhere in this range, which is itself a data point.
5. **200M/300M only after 1–50M numbers are in and you've decided it's
   worth the infra cost.** Snowflake warehouse credits and load time at that
   scale are real money and real wall-clock, not a default.

## Decisions needed from you (nothing proceeds without these)

- **Snowflake target**: is there a warehouse to bill this against, and is
  `migration_demo` the right database to load synthetic rows into, or should
  benchmarking use an instance isolated from anything real?
- **Postgres target**: local docker container (cheap, disposable — but its
  init path needs to actually be created, since `tests/postgres/init`
  doesn't exist today) or a real controlled instance?
- **New dependency approval**: is `psutil` acceptable for memory sampling,
  or stick to stdlib? Note `resource` (the other stdlib option §L names) is
  POSIX-only and does not exist on Windows — this environment is Windows, so
  either `psutil` or the cross-platform-but-Python-only `tracemalloc` (won't
  capture C-level/pandas buffer RSS) is the realistic stdlib-adjacent choice
  here, not `resource`.
- **Sequencing**: should the instrumentation (stage 1 above) be its own
  reviewed change before any synthetic data gets generated, or should stages
  1–2 land together?
- **Scope of the first real rung**: 1M rows now, or hold until the above are
  answered?

## Consequences

- No benchmark numbers exist yet; no performance claim about `hybrid_v1`
  vs. the standard path should be made or repeated until the staged ladder
  above actually runs and reports real numbers, per §L's own explicit
  instruction not to publish or rely on any number until then.
- The automatic row-count-based `execution_strategy` switch flagged as an
  open decision in [0011](0011-hybrid-v1-front-door.md) remains blocked on
  this benchmark, which itself is blocked on the environment/dependency
  decisions above — this is the dependency chain to point to if anyone asks
  "why don't large tables just use hybrid_v1 automatically yet."
