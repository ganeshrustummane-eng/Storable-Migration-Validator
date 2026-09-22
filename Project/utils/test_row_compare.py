"""Tests for the PK-indexed row comparison core.

These are the regression guard proving the extraction out of Project/main.py's
inline comparison block (see docs/large-table-scalable-architecture) didn't
change behavior, and the cases from that design doc's differential-test list
(§K) that don't need a live DB connection.

Run:  python -m pytest Project/utils/test_row_compare.py -q
  or: python Project/utils/test_row_compare.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from utils.row_compare import compare_indexed_frames  # noqa: E402


def _by_key(result_df):
    return result_df.set_index("row_key")


def test_identical_rows_pass():
    src = pd.DataFrame({"id": [1], "name": ["a"]})
    tgt = pd.DataFrame({"id": [1], "name": ["a"]})
    result = _by_key(compare_indexed_frames(src, tgt, "id", "id"))
    assert result.loc["1", "status"] == "PASS"


def test_changed_value_fails():
    src = pd.DataFrame({"id": [2], "name": ["b"]})
    tgt = pd.DataFrame({"id": [2], "name": ["CHANGED"]})
    result = _by_key(compare_indexed_frames(src, tgt, "id", "id"))
    assert result.loc["2", "status"] == "FAIL"


def test_null_vs_null_passes():
    src = pd.DataFrame({"id": [3], "name": [None]})
    tgt = pd.DataFrame({"id": [3], "name": [None]})
    result = _by_key(compare_indexed_frames(src, tgt, "id", "id"))
    assert result.loc["3", "status"] == "PASS"


def test_null_vs_non_null_fails():
    src = pd.DataFrame({"id": [4], "name": [None]})
    tgt = pd.DataFrame({"id": [4], "name": ["not-null"]})
    result = _by_key(compare_indexed_frames(src, tgt, "id", "id"))
    assert result.loc["4", "status"] == "FAIL"


def test_missing_source_row_is_source_only():
    src = pd.DataFrame({"id": [8], "name": ["only-in-source"]})
    tgt = pd.DataFrame({"id": [], "name": []})
    result = _by_key(compare_indexed_frames(src, tgt, "id", "id"))
    assert result.loc["8", "status"] == "SOURCE_ONLY"


def test_extra_target_row_is_target_only():
    src = pd.DataFrame({"id": [], "name": []})
    tgt = pd.DataFrame({"id": [9], "name": ["only-in-target"]})
    result = _by_key(compare_indexed_frames(src, tgt, "id", "id"))
    assert result.loc["9", "status"] == "TARGET_ONLY"


def test_duplicate_pk_same_multiset_passes():
    src = pd.DataFrame({"id": [5, 5], "name": ["dup1", "dup2"]})
    tgt = pd.DataFrame({"id": [5, 5], "name": ["dup2", "dup1"]})  # different row order
    result = _by_key(compare_indexed_frames(src, tgt, "id", "id"))
    assert result.loc["5", "status"] == "PASS"


def test_duplicate_count_mismatch_fails():
    src = pd.DataFrame({"id": [7, 7], "v": ["x", "x"]})
    tgt = pd.DataFrame({"id": [7], "v": ["x"]})
    result = _by_key(compare_indexed_frames(src, tgt, "id", "id"))
    assert result.loc["7", "status"] == "FAIL"


def test_composite_pk():
    src = pd.DataFrame({"a": [1, 1], "b": [1, 2], "v": ["x", "y"]})
    tgt = pd.DataFrame({"a": [1, 1], "b": [1, 2], "v": ["x", "Z"]})
    result = _by_key(compare_indexed_frames(src, tgt, ["a", "b"], ["a", "b"]))
    assert result.loc["1|1", "status"] == "PASS"
    assert result.loc["1|2", "status"] == "FAIL"


def test_schema_drift_columns_excluded_from_comparison_but_displayed():
    src = pd.DataFrame({"id": [1], "name": ["a"], "src_only": ["x"]})
    tgt = pd.DataFrame({"id": [1], "name": ["a"], "tgt_only": ["y"]})
    result = _by_key(compare_indexed_frames(src, tgt, "id", "id"))
    assert result.loc["1", "status"] == "PASS"  # drift columns don't affect PASS/FAIL
    assert "src_only__source" in result.columns
    assert "tgt_only__target" in result.columns


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
