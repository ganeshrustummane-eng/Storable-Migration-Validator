"""Self-check for the pure decision helpers in utility.py — no DB, no fixtures.
Run: python Project/utils/test_utility_checks.py
"""
from utility import count_validation_match, row_hash_fallback_looks_like_column_drift


def test_count_validation_match():
    # Default (threshold=0) preserves the historical strict-== behavior.
    assert count_validation_match(100, 100, 0) == (True, 0.0)
    assert count_validation_match(100, 101, 0)[0] is False

    # Opt-in tolerance for live/CDC tables.
    is_match, diff_pct = count_validation_match(1000, 1002, 0.5)
    assert is_match is True and round(diff_pct, 4) == 0.2
    is_match, diff_pct = count_validation_match(1000, 1010, 0.5)
    assert is_match is False and round(diff_pct, 4) == 1.0

    # source_rows == 0 with threshold set: no division by zero, falls back to exact match.
    assert count_validation_match(0, 0, 0.5) == (True, 0.0)
    assert count_validation_match(0, 5, 0.5)[0] is False


def test_row_hash_fallback_heuristic():
    # Roughly-equal SOURCE_ONLY/TARGET_ONLY across most of the table → flag it.
    assert row_hash_fallback_looks_like_column_drift(48, 50, 100) is True
    # Real one-sided drift (rows genuinely missing on one side only) → don't flag.
    assert row_hash_fallback_looks_like_column_drift(5, 0, 100) is False
    # Small mismatch relative to table size → don't flag (looks like real, minor drift).
    assert row_hash_fallback_looks_like_column_drift(2, 2, 1000) is False
    # No rows at all → don't flag.
    assert row_hash_fallback_looks_like_column_drift(0, 0, 0) is False


if __name__ == "__main__":
    test_count_validation_match()
    test_row_hash_fallback_heuristic()
    print("OK")
