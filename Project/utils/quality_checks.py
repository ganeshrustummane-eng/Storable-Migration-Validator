"""Configurable migration data-quality checks for executed validation frames."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _column_pairs(source_df: pd.DataFrame, target_df: pd.DataFrame) -> list[tuple[str, str]]:
    target_columns = set(target_df.columns)
    return [(column, column) for column in source_df.columns if column in target_columns]


def _pct_difference(source_value: float, target_value: float) -> float:
    """abs(source-target) as a % of |source|, floored at a denominator of 1.0
    (see docs/large-table-scalable-architecture §B's flagged near-zero-source
    caveat -- inherited as-is here, not fixed). Shared by the sum/min/max
    checks below and by the hybrid_v1 aggregate parity check
    (Project/tiered_runner.py) so the tolerance math isn't duplicated."""
    denominator = max(abs(source_value), 1.0)
    return abs(source_value - target_value) / denominator * 100


def _hash_dataframe(df: pd.DataFrame, columns: list[str]) -> str:
    """sha256 of columns rendered the same way sample_hash always has:
    astype(str) then to_csv. Shared with hybrid_v1's sample_hash check
    (Project/tiered_runner.py) so both paths hash identically."""
    csv_text = df[columns].astype(str).to_csv(index=False)
    return hashlib.sha256(csv_text.encode("utf-8")).hexdigest()


def run_quality_checks(
    source_df: pd.DataFrame,
    target_df: pd.DataFrame,
    config: dict,
) -> list[dict[str, Any]]:
    """Return failed configured checks. Empty list means all checks pass."""
    if not isinstance(config, dict):
        return [{"check": "quality_checks", "detail": "validation config must be a mapping"}]
    checks = config.get("quality_checks") or {}
    if not isinstance(checks, dict):
        return [{"check": "quality_checks", "detail": "quality_checks must be a mapping"}]
    if not checks or checks.get("enabled") is False:
        return []

    failures: list[dict[str, Any]] = []
    null_tolerance = _as_float(checks.get("null_rate_tolerance_pct"))
    distinct_tolerance = _as_float(checks.get("distinct_count_tolerance"))
    aggregate_tolerance = _as_float(checks.get("aggregate_tolerance_pct"))

    for source_column, target_column in _column_pairs(source_df, target_df):
        source_series = source_df[source_column]
        target_series = target_df[target_column]

        if null_tolerance >= 0:
            source_null_rate = float(source_series.isna().mean() * 100) if len(source_series) else 0.0
            target_null_rate = float(target_series.isna().mean() * 100) if len(target_series) else 0.0
            if abs(source_null_rate - target_null_rate) > null_tolerance:
                failures.append({
                    "check": "null_rate",
                    "column": source_column,
                    "source": round(source_null_rate, 6),
                    "target": round(target_null_rate, 6),
                    "tolerance": null_tolerance,
                })

        if distinct_tolerance >= 0:
            source_distinct = int(source_series.nunique(dropna=False))
            target_distinct = int(target_series.nunique(dropna=False))
            if abs(source_distinct - target_distinct) > distinct_tolerance:
                failures.append({
                    "check": "distinct_count",
                    "column": source_column,
                    "source": source_distinct,
                    "target": target_distinct,
                    "tolerance": distinct_tolerance,
                })

        if aggregate_tolerance >= 0:
            source_numeric = pd.to_numeric(source_series, errors="coerce")
            target_numeric = pd.to_numeric(target_series, errors="coerce")
            if source_numeric.notna().any() and target_numeric.notna().any():
                source_sum = float(source_numeric.sum())
                target_sum = float(target_numeric.sum())
                difference_pct = _pct_difference(source_sum, target_sum)
                if difference_pct > aggregate_tolerance:
                    failures.append({
                        "check": "sum",
                        "column": source_column,
                        "source": source_sum,
                        "target": target_sum,
                        "difference_pct": round(difference_pct, 6),
                        "tolerance": aggregate_tolerance,
                    })
                for aggregate_name, source_value, target_value in (
                    ("min", float(source_numeric.min()), float(target_numeric.min())),
                    ("max", float(source_numeric.max()), float(target_numeric.max())),
                ):
                    difference_pct = _pct_difference(source_value, target_value)
                    if difference_pct > aggregate_tolerance:
                        failures.append({
                            "check": aggregate_name,
                            "column": source_column,
                            "source": source_value,
                            "target": target_value,
                            "difference_pct": round(difference_pct, 6),
                            "tolerance": aggregate_tolerance,
                        })

    sample_percent = _as_float(checks.get("sample_hash_percent"))
    if sample_percent > 0 and len(source_df) and len(target_df):
        sample_size = max(1, int(min(len(source_df), len(target_df)) * min(sample_percent, 100) / 100))
        columns = [source for source, target in _column_pairs(source_df, target_df)]
        source_hash = _hash_dataframe(source_df.head(sample_size), columns)
        target_hash = _hash_dataframe(target_df.head(sample_size), columns)
        if source_hash != target_hash:
            failures.append({
                "check": "sample_hash",
                "sample_percent": sample_percent,
                "source_hash": source_hash,
                "target_hash": target_hash,
            })

    return failures


def validate_expected_grain(
    source_df: pd.DataFrame,
    target_df: pd.DataFrame,
    config: dict,
) -> list[dict[str, Any]]:
    """Reject duplicate rows when YAML declares one row per grain key."""
    if config.get("expected_grain") not in {"one_row_per_key", "one_row_per_driving_key"}:
        return []
    grain_columns = config.get("grain_columns") or config.get("pksourcecolumn")
    if isinstance(grain_columns, str):
        grain_columns = [grain_columns]
    if not grain_columns:
        return [{"check": "expected_grain", "detail": "grain_columns required"}]

    failures = []
    for side, frame in (("source", source_df), ("target", target_df)):
        columns = [str(column).lower() for column in grain_columns]
        missing = [column for column in columns if column not in frame.columns]
        if missing:
            failures.append({"check": "expected_grain", "side": side, "detail": f"missing grain columns: {missing}"})
            continue
        duplicate_count = int(frame.duplicated(columns, keep=False).sum())
        if duplicate_count:
            failures.append({
                "check": "expected_grain",
                "side": side,
                "grain_columns": columns,
                "duplicate_rows": duplicate_count,
                "detail": "many-to-many or one-to-many join changed declared grain",
            })
    return failures


def append_validation_audit(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON record. Audit failure must not hide validation result."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"recorded_at": datetime.now(timezone.utc).isoformat(), **record}, default=str) + "\n")
    except OSError:
        pass
