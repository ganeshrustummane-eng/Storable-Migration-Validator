"""
Metrics Tracker
===============
Instruments the Migration Intelligence Connector to capture measurable
business value metrics for the hackathon and ongoing ROI reporting.

Tracked metrics:
  - tables_processed
  - columns_processed
  - mappings_automated         (confidence >= auto-accept threshold)
  - mappings_requiring_review  (below review threshold)
  - validations_automated
  - manual_sql_avoided         (count of SQL scripts auto-generated)
  - validation_execution_time  (seconds)
  - human_review_time          (seconds, when recorded)
  - failures_detected
  - coverage_pct
  - ai_token_usage             (prompt + completion tokens)
  - ai_calls_made
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_METRICS_PATH = Path(__file__).resolve().parents[2] / "output" / "connector_metrics.jsonl"


@dataclass
class RunMetrics:
    """Metrics for one validation run or connector operation."""
    run_id:               str
    operation:            str       # e.g. "validate_migration", "get_migration_summary"
    table:                str = ""
    source_type:          str = ""
    timestamp:            str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Volume
    tables_processed:     int = 0
    columns_processed:    int = 0
    mappings_automated:   int = 0   # confidence >= AUTO_ACCEPT_THRESHOLD
    mappings_reviewed:    int = 0   # humans had to decide
    mappings_rejected:    int = 0

    # Quality
    validations_run:      int = 0
    pass_count:           int = 0
    fail_count:           int = 0
    warning_count:        int = 0
    failures_detected:    int = 0
    coverage_pct:         float = 0.0

    # Efficiency
    manual_sql_avoided:   int = 0   # SQL files auto-generated
    execution_time_s:     float = 0.0
    human_review_time_s:  float = 0.0

    # AI cost
    ai_token_usage:       int = 0
    ai_calls_made:        int = 0

    metadata:             Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MetricsTracker:
    """
    Append-only metrics store.

    Usage:
        tracker = MetricsTracker()
        with tracker.time_operation("validate_migration", table="customer") as m:
            m.tables_processed = 1
            m.columns_processed = 42
            ...
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else _METRICS_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, metrics: RunMetrics) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(metrics.to_dict(), ensure_ascii=False) + "\n")
        except Exception as exc:
            print(f"[METRICS ERROR] {exc}")

    def aggregate(self) -> Dict[str, Any]:
        """Return aggregate totals across all recorded metrics."""
        records = self._load_all()
        if not records:
            return {"error": "no metrics recorded yet"}

        totals: Dict[str, Any] = {
            "total_runs":             len(records),
            "tables_processed":       sum(r.tables_processed for r in records),
            "columns_processed":      sum(r.columns_processed for r in records),
            "mappings_automated":     sum(r.mappings_automated for r in records),
            "mappings_reviewed":      sum(r.mappings_reviewed for r in records),
            "mappings_rejected":      sum(r.mappings_rejected for r in records),
            "validations_run":        sum(r.validations_run for r in records),
            "pass_count":             sum(r.pass_count for r in records),
            "fail_count":             sum(r.fail_count for r in records),
            "failures_detected":      sum(r.failures_detected for r in records),
            "manual_sql_avoided":     sum(r.manual_sql_avoided for r in records),
            "ai_token_usage":         sum(r.ai_token_usage for r in records),
            "ai_calls_made":          sum(r.ai_calls_made for r in records),
            "avg_execution_time_s":   (
                sum(r.execution_time_s for r in records) / len(records)
                if records else 0.0
            ),
        }

        auto = totals["mappings_automated"]
        reviewed = totals["mappings_reviewed"]
        total_mappings = auto + reviewed
        totals["automation_rate_pct"] = (
            round(100.0 * auto / total_mappings, 1) if total_mappings else 0.0
        )
        return totals

    def _load_all(self) -> List[RunMetrics]:
        records = []
        if not self._path.exists():
            return records
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            d = json.loads(line)
                            records.append(RunMetrics(**{
                                k: v for k, v in d.items()
                                if k in RunMetrics.__dataclass_fields__
                            }))
                        except Exception:
                            pass
        except Exception:
            pass
        return records

    class _TimedOperation:
        def __init__(self, tracker: "MetricsTracker", operation: str, table: str, run_id: str):
            self._tracker = tracker
            self._metrics = RunMetrics(run_id=run_id, operation=operation, table=table)
            self._start: float = 0.0

        def __enter__(self) -> RunMetrics:
            self._start = time.monotonic()
            return self._metrics

        def __exit__(self, *_):
            self._metrics.execution_time_s = round(time.monotonic() - self._start, 3)
            self._tracker.record(self._metrics)

    def time_operation(self, operation: str, table: str = "", run_id: str = "") -> "_TimedOperation":
        if not run_id:
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return self._TimedOperation(self, operation, table, run_id)


# Module-level singleton
metrics_tracker = MetricsTracker()
