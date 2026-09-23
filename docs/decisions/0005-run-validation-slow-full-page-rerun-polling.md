# 0005. Fix: Run Validation taking minutes instead of seconds — full-page rerun on every poll tick

**Status:** Fixed (subprocess-level fix from [0004](0004-run-validation-hang-subprocess-pipe-deadlock.md) verified working; this is a second, separate bug on top of it)
**Date:** 2026-09-23

## What you reported

After the [0004](0004-run-validation-hang-subprocess-pipe-deadlock.md) fix, Run
Validation no longer hung forever, but a run of 3 tables that used to take
2-3 seconds now took "almost 5 minutes" and you weren't sure it would ever
finish. You suspected an orchestration problem between the two frameworks
(the AI/Streamlit webapp and the Python `Project/` engine).

## What I checked before touching anything

I did not assume the [0004](0004-run-validation-hang-subprocess-pipe-deadlock.md)
fix was wrong — I re-verified it directly, twice, outside the webapp:

1. Ran `python main.py --layer_type silver --tables customers orders --count_validation yes --data_validation yes --environment local` directly. **Finished in 10 seconds.**
2. Called `runner.start_validation()` / `collect_validation_result()` (the exact functions the webapp calls) directly in a script, polling `.poll()` in a loop exactly like the webapp does. **Finished in 11.5 seconds, correct results.**

So the subprocess and the runner wrapper are both fine and fast. The extra
minutes are not in `Project/` at all — they're in how the webapp *polls* for
the result.

## Root cause

`webapp/app.py`'s Run Validation tab is one `with tab_execute:` block inside
one ~4,400-line Streamlit script. Streamlit's default execution model: any
`st.rerun()` re-executes the **entire script from the top** — every tab's
setup code, every YAML inventory scan, every cached/uncached helper — not
just the section that called it.

The Stop-button feature (added alongside 0004) polls with:

```python
elif _proc.poll() is None:
    time.sleep(1)
    st.rerun()
```

Each of those `st.rerun()` calls was a **full-page rerun**, not a scoped one.
So instead of "sleep 1 second, check again" costing ~1 second per tick, each
tick actually cost 1 second of sleep **plus** however long it takes to
re-execute the entire rest of app.py (every other tab's YAML inventory scans,
schema/discovery code, etc.). An 11-second subprocess polled every ~1s means
roughly 10-15 ticks — at even a few seconds of full-script overhead per tick,
that alone reaches minutes. This is exactly the "orchestration issue" you
suspected, just not between the two frameworks in the way you guessed — it's
Streamlit's own full-script rerun model interacting badly with a polling loop
I added.

## Fix

Wrapped the whole Run Validation execution panel (`webapp/app.py`, the block
starting at "Run validation"/Stop button through the results rendering) in
`@st.fragment`. Per Streamlit's own performance guidance: `st.rerun()` called
**inside** a fragment reruns only that fragment by default, not the whole
app — every other tab's setup code is skipped entirely on each poll tick. No
other logic changed: same button, same Stop behavior, same result rendering,
just isolated so polling doesn't drag the rest of the page along with it.

```python
@st.fragment
def _run_validation_panel(selected_tables, do_count, do_data, _run_layer, environment,
                           mismatch_threshold, count_mismatch_threshold,
                           picked_data_tables, picked_count_tables, _inventory):
    ...  # unchanged body — button, poll loop, Stop button, results rendering
_run_validation_panel(...)
```

## Verification

- `python -m py_compile webapp/app.py` — clean.
- Confirmed `st.fragment` is available in the installed Streamlit (1.62.0).
- Existing test suite (43 tests across `Project/`) still passes — no
  regression from the reindentation.
- **Not yet click-tested in an actual browser** — I don't have browser
  automation available in this environment, so I verified the subprocess
  layer directly (both isolated calls above) and confirmed the fragment
  mechanism is the documented, correct fix for "st.rerun() dragging the whole
  page along," but I have not personally watched the Stop button and the
  "Running..." panel behave correctly end-to-end in your browser. Please run
  it once and tell me if the poll now feels like ~1 second per tick again.

## Consequences

- Any other place in this app that adds a manual poll-and-rerun loop should
  use `@st.fragment` from the start, not add it as an afterthought — this is
  now the second bug from the same root cause pattern.
- If you see slowness again in a *different* tab that also polls or
  auto-refreshes, check first whether it's wrapped in a fragment before
  assuming it's a `Project/` engine problem.
