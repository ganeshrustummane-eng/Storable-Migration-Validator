---
plan: 003
title: Surface failed-only CSVs (colored) on the Run Validation dashboard
status: TODO
effort: S
depends_on: none
commit: cc0c1fe
branch: version1.2
---

## Why

`Project/main.py` already writes a **failed-rows-only CSV** for every data-validation
table run (`{table}_{validation}_failed_{run_id}.csv`, written at
`Project/main.py:333-337`), right next to the full `{table}_{validation}_result_{run_id}.csv`.
Nobody asked for this file to be created from scratch — it already exists on disk today.
The problem is purely on the read side:

1. `Project/runner.py:110` only globs `*_result_*.csv` into `result["diff_files"]`. The
   `*_failed_*.csv` files are never picked up, so the dashboard never shows them.
2. `webapp/app.py` has no `st.download_button` anywhere in the file (verified: zero
   matches for `download_button`). Users can currently only *look* at results in the
   browser, never export a CSV from the dashboard.
3. The table the user is actually looking at
   (`data_validation_summary.csv`, produced by `src/utils/summary_reporter.py`, one row
   per **table**, not per data row) always shows PASS and FAIL tables together in
   `render_paginated_df` (`webapp/app.py:2967`) — no failed-only filter.

Two distinct "failed only" asks map to two distinct existing CSVs:
- **Per-table summary** (`data_validation_summary.csv`) → needs a status filter on an
  existing view.
- **Per-record mismatches** (`{table}_data_validation_failed_{run_id}.csv`) → the file
  already exists, it just needs to be surfaced.

Do both — they're the same UI area, same effort tier.

## What NOT to do

- Do not touch `src/utils/summary_reporter.py` or `Project/main.py`'s CSV-writing logic.
  The failed-rows file already exists and is correct; this plan is display/export only.
- Do not add a new dependency (e.g. `openpyxl`/`xlsxwriter`) to produce a literally
  colored `.xlsx` file. CSV has no color model — the color the user is asking for is
  achievable entirely with Streamlit's existing `.style` machinery (already used at
  `webapp/app.py:451-461` and `506-533`), rendered in the browser. If the user later
  wants color that survives *outside* the browser (opened in Excel), that's a separate,
  bigger ask — flag it back to them, don't build it speculatively.
- Do not change `count_validation` handling — it has no per-record diff file, only the
  per-table summary, which the filter below already covers.

## Steps

### 1. `Project/runner.py` — also collect the failed-only files

File: `Project/runner.py`, around line 110.

Current:
```python
    result["diff_files"] = sorted(Path(p) for p in glob.glob(str(run_dir / "**" / "*_result_*.csv"), recursive=True))
```

Change to also collect failed-only files under a new key, so callers can tell the two
apart (full result set vs. failures-only):

```python
    result["diff_files"] = sorted(Path(p) for p in glob.glob(str(run_dir / "**" / "*_result_*.csv"), recursive=True))
    result["failed_files"] = sorted(Path(p) for p in glob.glob(str(run_dir / "**" / "*_failed_*.csv"), recursive=True))
```

Also update the docstring block a few lines above (`"diff_files": [Path, ...],   # per-table mismatch CSVs, if any`)
to add a matching line for `"failed_files"`, and initialize `"failed_files": []` next to
the existing `"diff_files": []` in the default `result` dict (~line 94).

**Verification:** run `python -c "import ast; ast.parse(open('Project/runner.py').read())"`
from repo root — confirms no syntax error. No test currently exercises `runner.py`
directly; this is a pure additive glob, same pattern as the existing line.

### 2. `webapp/app.py` — render the failed-only files after the full result files

File: `webapp/app.py`, in the Run Validation tab, right after the existing block at
line 2969-2974:

```python
                if result["diff_files"]:
                    with st.container(border=True):
                        st.markdown("#### 🔍 Data validation — row-level results")
                        st.caption("Every row is shown — green = matched, red = differed. Local files only, never sent to any AI/LLM.")
                        for f in result["diff_files"]:
                            _render_diff_file(f, key_prefix=f"exec_diff_{f.stem}")
```

Add a new block right after it, reusing `_render_diff_file` (it already colors
PASS/FAIL rows and diffing cells — see `webapp/app.py:451-533` — and already tolerates
files with zero PASS rows, since it computes `n_pass = n_total - n_fail`):

```python
                if result.get("failed_files"):
                    with st.container(border=True):
                        st.markdown("#### ❌ Data validation — failed rows only")
                        st.caption("Same rows as above, pre-filtered to mismatches. Download below to share just the failures.")
                        for f in result["failed_files"]:
                            _render_diff_file(f, key_prefix=f"exec_failed_{f.stem}")
                            st.download_button(
                                f"⬇ Download {f.name}",
                                data=f.read_bytes(),
                                file_name=f.name,
                                mime="text/csv",
                                key=f"dl_failed_{f.stem}",
                            )
```

**Verification:** `python -c "import ast; ast.parse(open('webapp/app.py').read())"`.
Then run the app (`streamlit run webapp/app.py` from `webapp/`, or however it's normally
launched — check `webapp/README.md`), go to Run Validation, run a data validation
against any table that has at least one intentional mismatch, and confirm:
- a new "❌ Data validation — failed rows only" panel appears below the existing
  row-level results panel,
- it shows only FAIL/SOURCE_ONLY/TARGET_ONLY rows, still colored red/amber with yellow
  diff cells,
- the download button produces a `.csv` that opens and matches what's on screen (no
  color in the raw file — that's expected, see "What NOT to do").
- If no table run has mismatches, the panel simply doesn't render (empty `failed_files`
  list) — confirm this doesn't throw.

### 3. `webapp/app.py` — "Show failed only" toggle on the per-table summary

File: `webapp/app.py`, in the same Run Validation block, around line 2957-2967:

Current:
```python
                for vtype, df in result["summaries"].items():
                    with st.container(border=True):
                        st.markdown(f"#### {'🔢' if vtype == 'count_validation' else '🧬'} {vtype.replace('_', ' ').title()} summary")
                        n_total = len(df)
                        n_pass = int((df["status"] == "PASS").sum())
                        n_fail = n_total - n_pass
                        m1, m2, m3 = st.columns(3)
                        m1.metric("Tables checked", n_total)
                        m2.metric("Passed", n_pass)
                        m3.metric("Failed", n_fail, delta=-n_fail if n_fail else None, delta_color="inverse")
                        render_paginated_df(df, key_prefix=f"exec_summary_{vtype}")
```

Add a checkbox (same pattern already used at `webapp/app.py:2870`,
`st.checkbox("Select all", ...)`) and filter before rendering, plus a download button
for the filtered CSV:

```python
                for vtype, df in result["summaries"].items():
                    with st.container(border=True):
                        st.markdown(f"#### {'🔢' if vtype == 'count_validation' else '🧬'} {vtype.replace('_', ' ').title()} summary")
                        n_total = len(df)
                        n_pass = int((df["status"] == "PASS").sum())
                        n_fail = n_total - n_pass
                        m1, m2, m3 = st.columns(3)
                        m1.metric("Tables checked", n_total)
                        m2.metric("Passed", n_pass)
                        m3.metric("Failed", n_fail, delta=-n_fail if n_fail else None, delta_color="inverse")
                        show_failed_only = st.checkbox(
                            "Show failed only", value=False, key=f"exec_summary_failed_only_{vtype}"
                        )
                        display_df = df[df["status"] == "FAIL"] if show_failed_only else df
                        render_paginated_df(display_df, key_prefix=f"exec_summary_{vtype}")
                        st.download_button(
                            f"⬇ Download {'failed-only ' if show_failed_only else ''}{vtype} summary CSV",
                            data=display_df.to_csv(index=False).encode("utf-8"),
                            file_name=f"{vtype}_summary{'_failed' if show_failed_only else ''}.csv",
                            mime="text/csv",
                            key=f"dl_summary_{vtype}",
                        )
```

`render_paginated_df` already applies `_style_status` (green PASS / red FAIL text,
`webapp/app.py:451-461`) by default (`style_status: bool = True`) — no changes needed
there.

**Verification:** same manual run as step 2. Toggle "Show failed only" on the
count/data validation summary panel — table should shrink to only FAIL rows, metrics
stay showing totals (not filtered — that's intentional, they describe the whole run),
and the download button's filename/content should match the toggle state.

## Also apply to the Historical Runs tab (optional, same pattern)

`webapp/app.py:3051` already has a `status_filter = st.selectbox("Status", ["All", "PASS", "FAIL"], ...)`
for the history tab. Check whether `results_store.query_results(...)` (called just
below, `webapp/app.py:3059`) is already passed `status=status_filter` when it's not
"All" — if so, this tab already supports failed-only filtering and needs only a
download button added next to it, following the same `st.download_button` pattern as
step 3. If it's not wired through, that's a separate small bug — fix it in this same
step since it's the same code path. **Read the surrounding ~20 lines before assuming
either way.**

## Test

No existing test file covers `webapp/app.py` or `Project/runner.py` (Streamlit apps
aren't typically unit-tested this way in this repo — confirm by checking
`test_jira_integration.py` and any `tests/` directory for precedent before adding one).
Given that, the manual verification in steps 2-3 above is the check for this plan — no
new automated test needed for a pure Streamlit rendering change. If a `tests/` directory
with pytest coverage of `runner.py` is found during execution, add one assertion there
for the new `failed_files` key instead of skipping — check before writing.

## Maintenance note

If `Project/main.py`'s failed-file naming convention (`*_failed_*.csv`) ever changes,
`Project/runner.py:110`'s new glob must change with it — same coupling that already
exists for `*_result_*.csv`. Anyone adding a third per-table CSV export (e.g. a
schema-drift report) should follow the same `result["<name>_files"]` pattern rather
than inventing a new shape.
