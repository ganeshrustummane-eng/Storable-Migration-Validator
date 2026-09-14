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
from pathlib import Path

import pandas as pd
import yaml

import results_store

PROJECT_DIR = Path(__file__).parent
_RUN_ID_RE = re.compile(r"Run ID:\s*(\S+)")


def list_configured_tables(layer: str) -> dict:
    """Table names available for this layer, split by validation type, read
    directly from what's actually on disk (not assumed to be in sync with
    each other — a table can have a count_validation entry with no matching
    data_validation YAML file, or vice versa).

    Returns {"count_validation": [...], "data_validation": [...]}, each sorted.
    """
    count_path = PROJECT_DIR / "config" / layer / "count_validation" / f"{layer}.yaml"
    count_tables = []
    if count_path.exists():
        with open(count_path) as f:
            cfg = yaml.safe_load(f) or {}
        count_tables = sorted((cfg.get("tables") or {}).keys())

    config_root = PROJECT_DIR / "config" / layer
    report_root = PROJECT_DIR / "config" / "report"
    # Layer-scoped + report/ (independent of layer) data_validation YAMLs
    search_roots = [config_root] + ([report_root] if report_root.exists() else [])
    data_tables = sorted(
        p.stem for root in search_roots for p in root.rglob("*.yaml")
        if p.parent.name == "data_validation"
    )

    return {"count_validation": count_tables, "data_validation": data_tables}


def run_validation(layer: str, environment: str, tables: list,
                    count_validation: bool, data_validation: bool,
                    timeout: int = 900) -> dict:
    """Runs `python main.py --layer_type ... --tables ... --environment ...`
    in Project/, then locates and loads the summary CSV(s) it produced.

    Returns:
        {
            "run_id": str | None,
            "returncode": int,
            "stdout_tail": str,
            "summaries": {"count_validation": DataFrame, "data_validation": DataFrame},
            "diff_files": [Path, ...],   # per-table full result CSVs (all rows), if any
            "failed_files": [Path, ...], # per-table failed-rows-only CSVs, if any
            "run_dir": Path | None,
        }
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
    proc = subprocess.run(
        args, cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=timeout,
    )

    run_id = None
    m = _RUN_ID_RE.search(proc.stdout)
    if m:
        run_id = m.group(1)

    result = {
        "run_id": run_id,
        "returncode": proc.returncode,
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-60:]),
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-60:]),
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

    if result["summaries"]:
        results_store.record_run(run_id, layer, environment, proc.returncode, result["summaries"])

    return result
