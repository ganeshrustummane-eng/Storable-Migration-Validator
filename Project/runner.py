"""
Thin subprocess wrapper around Project/main.py so the webapp can trigger a
real YAML-driven validation run and read back its results, without
reimplementing main.py's execution logic (which has module-level side
effects and isn't import-safe).
"""
import glob
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

import results_store

PROJECT_DIR = Path(__file__).parent
_RUN_ID_RE = re.compile(r"Run ID:\s*(\S+)")


@dataclass
class RunningValidation:
    """Handle for an in-flight `main.py` subprocess. stdout/stderr are
    redirected to temp files rather than `subprocess.PIPE` -- see
    docs/decisions/0004-run-validation-hang-subprocess-pipe-deadlock.md.
    A pipe has a small OS buffer (~64KB on Windows); main.py logs at DEBUG
    level (full SQL text per table) and can exceed that easily, so if nobody
    drains the pipe until the process exits, the child blocks on write() and
    never exits -- exactly the "stuck on Running... forever" hang. Files have
    no such bounded buffer."""
    proc: subprocess.Popen
    stdout_path: Path
    stderr_path: Path

    def poll(self):
        return self.proc.poll()


def list_configured_tables(layer: str) -> dict:
    """Table names available for this layer, split by validation type, read
    directly from what's actually on disk (not assumed to be in sync with
    each other — a table can have a count_validation entry with no matching
    data_validation YAML file, or vice versa).

    Returns {"count_validation": [...], "data_validation": [...]}, each sorted.
    """
    cv_dir = PROJECT_DIR / "config" / layer / "count_validation"
    count_tables_set = set()
    if cv_dir.exists():
        for cv_path in sorted(cv_dir.glob("*.yaml")):
            with open(cv_path) as f:
                cfg = yaml.safe_load(f) or {}
            count_tables_set.update((cfg.get("tables") or {}).keys())
    count_tables = sorted(count_tables_set)

    config_root = PROJECT_DIR / "config" / layer
    report_root = PROJECT_DIR / "config" / "report"
    # Layer-scoped + report/ (independent of layer) data_validation YAMLs
    search_roots = [config_root] + ([report_root] if report_root.exists() else [])
    data_tables = sorted(
        p.stem for root in search_roots for p in root.rglob("*.yaml")
        if "data_validation" in p.parts
    )

    return {"count_validation": count_tables, "data_validation": data_tables}


def start_validation(layer: str, environment: str, tables: list,
                      count_validation: bool, data_validation: bool) -> RunningValidation:
    """Launches `python main.py --layer_type ... --tables ... --environment ...`
    in Project/ and returns immediately with a handle to the live process.

    stdout/stderr are redirected to temp files, NOT subprocess.PIPE -- see
    RunningValidation's docstring for why a pipe here causes a permanent hang.

    Callers that need to let a user cancel mid-run (webapp's Run Validation tab)
    should poll `.poll()` and call `terminate_validation()` on the returned
    handle, then pass it to `collect_validation_result`. Callers that just want
    a blocking call (the scheduler) should use `run_validation` below.
    """
    if not tables:
        raise ValueError("At least one table (or 'all') is required.")
    if not count_validation and not data_validation:
        raise ValueError("Enable at least one of count_validation / data_validation.")

    args = [
        sys.executable, "main.py",
        "--layer_type", layer,
        "--tables", *tables,
        "--count_validation", "yes" if count_validation else "no",
        "--data_validation", "yes" if data_validation else "no",
        "--environment", environment,
    ]
    stdout_fd, stdout_path = tempfile.mkstemp(prefix="validation_stdout_", suffix=".log")
    stderr_fd, stderr_path = tempfile.mkstemp(prefix="validation_stderr_", suffix=".log")
    with os.fdopen(stdout_fd, "w") as stdout_f, os.fdopen(stderr_fd, "w") as stderr_f:
        proc = subprocess.Popen(
            args, cwd=str(PROJECT_DIR), stdout=stdout_f, stderr=stderr_f, text=True,
        )
    return RunningValidation(proc=proc, stdout_path=Path(stdout_path), stderr_path=Path(stderr_path))


def collect_validation_result(running: RunningValidation, layer: str, environment: str,
                                cancelled: bool = False) -> dict:
    """Waits for `running` (from `start_validation`) to finish and loads the
    summary CSV(s) it produced.

    Returns:
        {
            "run_id": str | None,
            "returncode": int,
            "stdout_tail": str,
            "cancelled": bool,
            "summaries": {"count_validation": DataFrame, "data_validation": DataFrame},
            "diff_files": [Path, ...],   # per-table full result CSVs (all rows), if any
            "failed_files": [Path, ...], # per-table failed-rows-only CSVs, if any
            "run_dir": Path | None,
        }
    """
    proc = running.proc
    proc.wait()
    try:
        stdout = running.stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = running.stderr_path.read_text(encoding="utf-8", errors="replace")
    finally:
        for p in (running.stdout_path, running.stderr_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    run_id = None
    m = _RUN_ID_RE.search(stdout)
    if m:
        run_id = m.group(1)

    result = {
        "run_id": run_id,
        "returncode": proc.returncode,
        "stdout_tail": "\n".join(stdout.splitlines()[-60:]),
        "stderr_tail": "\n".join(stderr.splitlines()[-60:]),
        "cancelled": cancelled,
        "summaries": {},
        "diff_files": [],
        "failed_files": [],
        "run_dir": None,
    }
    if not run_id:
        return result

    run_dir = PROJECT_DIR / "output" / layer / f"validation_{run_id}"
    result["run_dir"] = run_dir
    if not run_dir.exists():
        return result

    for vtype in ("count_validation", "data_validation"):
        summary_path = run_dir / f"{vtype}_{run_id}" / f"{vtype}_summary.csv"
        if summary_path.exists():
            result["summaries"][vtype] = pd.read_csv(summary_path)

    result["diff_files"] = sorted(Path(p) for p in glob.glob(str(run_dir / "**" / "*_result_*.csv"), recursive=True))
    result["failed_files"] = sorted(Path(p) for p in glob.glob(str(run_dir / "**" / "*_failed_*.csv"), recursive=True))

    if result["summaries"] and not cancelled:
        results_store.record_run(run_id, layer, environment, proc.returncode, result["summaries"])

    return result


def terminate_validation(running: RunningValidation, grace_seconds: float = 5.0) -> None:
    """User-initiated stop: TERM first so main.py's connector `finally: conn.close()`
    blocks get a chance to run and free the source/Snowflake connections cleanly,
    then KILL if it hasn't exited within `grace_seconds`."""
    proc = running.proc
    proc.terminate()
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_validation(layer: str, environment: str, tables: list,
                    count_validation: bool, data_validation: bool,
                    timeout: int = 900) -> dict:
    """Blocking convenience wrapper over start_validation + collect_validation_result,
    for callers with no user-facing cancel control (e.g. the scheduler)."""
    running = start_validation(layer, environment, tables, count_validation, data_validation)
    try:
        running.proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        running.proc.kill()
        running.proc.wait()
        raise
    return collect_validation_result(running, layer, environment)
