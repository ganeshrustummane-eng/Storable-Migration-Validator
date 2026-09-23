# 0006. Fix: Run Validation finishes and writes CSVs, but UI stays stuck on "Running..." forever

**Status:** Fixed
**Date:** 2026-09-23

## What you reported

After the [0005](0005-run-validation-slow-full-page-rerun-polling.md) fix, the
poll no longer took minutes — but the panel still never showed a result. You
confirmed the backend actually ran: `Project/`'s output folder had fresh
summary/data-validation CSVs for the run, but the Streamlit page kept showing
"⏳ Running..." indefinitely. Not a backend problem — a UI-only bug, per your
own read of it.

## Root cause

This is a bug I introduced in [0005](0005-run-validation-slow-full-page-rerun-polling.md)'s
own fix, not a new orchestration problem between the two frameworks. When I
wrapped the panel in `@st.fragment`, the script I used to splice/reindent
`webapp/app.py`'s lines 3239–3393 preserved the *relative* indentation of the
results-rendering block (`if result: ...`, `webapp/app.py:3318`) one level too
deep — it landed **inside** the `elif st.button("🚀 Run validation"):` branch
instead of after the whole `if`/`elif`/`else` chain.

The control flow is:

```python
if st.session_state.get(_exec_proc_key) is not None:      # already running
    ...
    elif _proc.poll() is None:      # still running
        time.sleep(1); st.rerun()
    else:                            # just finished
        result = collect_validation_result(...)            # <-- result set HERE
        st.session_state[_exec_proc_key] = None
elif st.button("🚀 Run validation", ...):                  # user just clicked
    ...
    st.rerun()
    if result:                       # <-- rendering was nested HERE (wrong branch)
        ...render summaries, diffs, downloads...
```

`result` is only ever populated in the *first* branch's `else` (the tick where
`.poll()` reports the subprocess has exited). The rendering code that turns
`result` into `st.success`/summaries/diff tables lived inside the *second*,
mutually-exclusive branch (the button-click branch) — a branch that only runs
when the button is freshly clicked, where `result` is always `None`. So on the
tick where the run actually finishes, the code correctly collects the result
and clears the running-process state, but then falls out of the `if`/`elif`
with no matching code left to run — nothing renders, and no `st.rerun()` is
called either. The fragment's last visible output stays whatever `st.info("⏳
Running...")` printed earlier in that same tick, frozen, even though the
process and session state have already moved on. The CSVs on disk were real
and correct the whole time — only the UI's response to a *completed* run was
unreachable code.

## Fix

Dedented `webapp/app.py:3318`'s `if result:` block (through the end of the
function, line 3403) by one level, moving it out of the button-click `elif`
and to the same level as the `if`/`elif`/`else` chain — so it runs after
either branch that can produce a `result` (poll-detected completion, or the
Stop button), not just the one that structurally never has one.

## Verification

- `python -m py_compile webapp/app.py` — clean.
- Traced the control flow line-by-line against `Project/runner.py`'s
  `collect_validation_result()` contract to confirm `result` is now rendered
  on the same script execution that sets it, with no extra rerun needed.
- **Not yet click-tested in an actual browser** — same limitation as
  [0005](0005-run-validation-slow-full-page-rerun-polling.md), no browser
  automation available here. Please run one real validation end-to-end and
  confirm the summary/diff tables actually appear when it finishes, and that
  the Stop button also renders whatever partial result it collected.

## Consequences

- Any future edit to this fragment must keep the results-rendering block
  outside of (after) the `if`/`elif`/`else` that decides *whether* a result
  was just produced — never inside one specific branch of it.
- This is the third bug from the same feature (Stop-button + poll loop) in
  one session: [0004](0004-run-validation-hang-subprocess-pipe-deadlock.md)
  (deadlock), [0005](0005-run-validation-slow-full-page-rerun-polling.md)
  (full-page rerun), 0006 (misplaced render block from fixing 0005). Before
  calling this feature done, do one full manual click-through: start a run,
  watch it poll, watch it finish and render, then repeat once using Stop
  mid-run.
