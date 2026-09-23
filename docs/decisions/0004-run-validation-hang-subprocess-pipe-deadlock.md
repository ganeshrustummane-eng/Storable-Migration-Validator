# 0004. Fix: Run Validation stuck on "Running..." forever (subprocess pipe deadlock)

**Status:** Fixed
**Date:** 2026-09-23

## The bug you hit

You generated YAMLs for the silver layer / Postgres, then clicked "🚀 Run
validation" in the webapp — it got stuck showing "⏳ Running..." and never
finished. Confirmed with you: not an error, not a missing-table issue, not a
wrong-result issue — a permanent hang.

## Root cause

I caused this in the previous change (decision [0002](0002-table-level-thread-pool-parallelism.md)'s neighbor —
the Stop-button feature added in the same session). To let you cancel a
running validation from the UI, I changed `Project/runner.py` from:

```python
subprocess.run(args, capture_output=True, text=True, timeout=timeout)
```

to a manual, pollable version:

```python
proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
# ... poll proc.poll() from the UI on each rerun ...
proc.communicate()   # only called once poll() says it already exited
```

`subprocess.run(capture_output=True)` is safe because it drains both pipes
*concurrently* with waiting, internally, via threads. My replacement only
ever read the pipes at the very end, via `.communicate()`, gated on
`proc.poll()` first reporting the process had already exited.

A pipe has a small, fixed OS buffer (~64KB on Windows). `Project/main.py`'s
logger runs at DEBUG level to stdout, including full SQL query text per
table (`logger.debug("Source query: %s", source_query)` and similar) —
comfortably enough output to fill that buffer for more than a couple of
tables. Once the buffer is full, the **child process blocks inside its own
`print`/logging write() call**, waiting for someone to read the pipe. Nobody
does, because the parent is waiting for `proc.poll()` to report the process
as exited — which it never will, because the child is blocked, not exited.
Both sides wait on each other forever. This is a textbook subprocess pipe
deadlock, and it also silently broke the scheduler's blocking `run_validation()`
path (`proc.wait(timeout=...)` has the exact same problem — a full pipe blocks
`wait()` too), not just the interactive Stop-button path.

**Reproduced and confirmed directly** (not inferred): a Python subprocess
printing 200KB to stdout via `subprocess.PIPE` with no reader hangs past a
5-second timeout; the same subprocess with stdout redirected to a file
completes immediately.

## Fix

`Project/runner.py`: redirect the child's stdout/stderr to temp files instead
of `subprocess.PIPE` (`RunningValidation` dataclass wraps the `Popen` handle
plus the two file paths). A file has no bounded buffer to fill, so the child
can never block on writing output regardless of how much it logs. Both
`start_validation()`/`collect_validation_result()` (webapp's Stop-button-aware
path) and `run_validation()` (the scheduler's blocking path) now go through
this — both had the bug, both are fixed the same way. Temp files are deleted
in `collect_validation_result()`'s `finally`.

`webapp/app.py` needed no changes — it only ever called `.poll()` on the
returned handle, which `RunningValidation.poll()` delegates straight to the
underlying `Popen`.

## Verification

- Added `Project/test_runner_subprocess.py` — spawns a real subprocess that
  prints >64KB of stdout (same shape as `main.py`'s DEBUG logging across a
  few tables) using the fixed file-redirect pattern and asserts it completes
  within 15s. Confirmed the *old* PIPE-based version actually hangs on the
  same input (reproduced directly, timeout hit, output above) before
  confirming the fix resolves it.
- Existing suite (`Project/test_tiered_runner.py`, `test_hybrid_dispatch.py`,
  `Project/utils/test_utility_checks.py`) still passes — 43 tests, no
  regressions.
- Not yet re-tested against a real Postgres→Snowflake YAML through the actual
  Streamlit UI end-to-end — worth doing one real Run Validation click on the
  silver/Postgres tables you generated before calling this fully closed.

## Consequences

- Any future change to `Project/runner.py`'s subprocess handling should keep
  output going to files (or use `communicate()`/threaded readers *while*
  waiting, never after), not back to bare `PIPE` + poll-only — that combination
  is the specific thing that hangs.
- This also means large/verbose validation runs no longer risk hanging
  regardless of table count or logging volume, not just the case you hit.
