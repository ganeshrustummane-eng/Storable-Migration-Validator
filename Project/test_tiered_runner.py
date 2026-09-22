"""Differential check for the hybrid Tier-1/Tier-2 engine: does tiering (accept
HASH_MATCH keys directly, narrow HASH_MISMATCH/SOURCE_ONLY/TARGET_ONLY keys to a
targeted re-fetch) produce the same (row_key, status) set as running the
existing, unmodified compare_indexed_frames on the full, untiered data?

No live DB connection — source/target are fake objects implementing just the
two methods run_table_hybrid needs (execute_query_stream, execute_query), same
duck-typing style as Project/db/test_postgres.py's MagicMock fakes.

Run:  python -m pytest Project/test_tiered_runner.py -q
  or: python Project/test_tiered_runner.py
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

import tiered_runner  # noqa: E402
from utils.quality_checks import validate_expected_grain  # noqa: E402
from utils.row_compare import compare_indexed_frames  # noqa: E402
from utils.semantic_normalize import canonicalize_frames  # noqa: E402


class _FakeDB:
    """Serves canned full-table data for a table's real query, and a
    pre-split (record_key, row_hash) stream for its Tier-1 row-hash query."""

    def __init__(self, full_df, hash_rows):
        self._full_df = full_df
        self._hash_rows = hash_rows  # list of (record_key, row_hash)

    def execute_query_stream(self, query, chunksize=50_000):
        for i in range(0, len(self._hash_rows), chunksize):
            batch = self._hash_rows[i:i + chunksize]
            yield pd.DataFrame(batch, columns=["record_key", "row_hash"])

    def execute_query(self, query):
        # Simulates "SELECT * FROM (<query>) AS t WHERE 1 = 0" (schema probe)
        # and "... WHERE id IN (...)" (narrowed fetch) against the same fixture.
        if "WHERE 1 = 0" in query:
            return self._full_df.iloc[0:0]
        if " IN (" in query:
            in_list = query.split(" IN (", 1)[1].rsplit(")", 1)[0]
            keys = {int(v.strip().strip("'")) for v in in_list.split(",")}
            return self._full_df[self._full_df["id"].isin(keys)].reset_index(drop=True)
        return self._full_df


def _build_fixture():
    # Table with: 1 identical row, 1 changed row, 1 source-only row, 1
    # target-only row, 1 duplicate-PK pair (same multiset -> PASS).
    source_full = pd.DataFrame({
        "id": [1, 2, 5, 5, 8],
        "name": ["a", "b", "dup1", "dup2", "only-in-source"],
    })
    target_full = pd.DataFrame({
        "id": [1, 2, 5, 5, 9],
        "name": ["a", "CHANGED", "dup2", "dup1", "only-in-target"],
    })

    # Tier-1 hash rows: identical content hashes the same, differing content
    # hashes differently, duplicate group hashes as a matching multiset.
    def h(name):
        return f"hash-{name}" if name not in ("dup1", "dup2") else "hash-dup"

    source_hash_rows = [(row.id, h(row.name)) for row in source_full.itertuples()]
    target_hash_rows = [(row.id, h(row.name)) for row in target_full.itertuples()]

    src_db = _FakeDB(source_full, source_hash_rows)
    tgt_db = _FakeDB(target_full, target_hash_rows)
    return source_full, target_full, src_db, tgt_db


def _oracle_result(source_full, target_full):
    s_df, t_df = canonicalize_frames(source_full.copy(), target_full.copy())
    return compare_indexed_frames(s_df, t_df, "id", "id")


def test_hybrid_matches_untiered_oracle_on_row_level_status():
    source_full, target_full, src_db, tgt_db = _build_fixture()
    oracle = _oracle_result(source_full, target_full).set_index("row_key")["status"].to_dict()

    validation_config = {
        "pksourcecolumn": "id",
        "pktargetcolumn": "id",
        "sourcequery": "SELECT id, name FROM source_t",
        "targetquery": "SELECT id, name FROM target_t",
    }
    row_hash_config = {"sourcequery": "SELECT id AS record_key, h AS row_hash FROM source_t",
                        "targetquery": "SELECT id AS record_key, h AS row_hash FROM target_t"}

    import tempfile
    with tempfile.TemporaryDirectory() as tmp, \
         patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        result = tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )

        result_csv = pd.read_csv(os.path.join(tmp, "fixture_table_data_validation_result_test.csv"))

    hybrid = dict(zip(result_csv["row_key"].astype(str), result_csv["status"]))

    assert hybrid == oracle, f"hybrid={hybrid} oracle={oracle}"
    assert result["is_match"] is False  # real mismatches exist (changed value + missing rows)
    print("test_hybrid_matches_untiered_oracle_on_row_level_status: OK")


def test_incomplete_row_hash_coverage_refuses_rather_than_silently_passing():
    """Phase 2 audit finding F2: row_hash.columns=[id, name] does not cover
    'amount', which genuinely differs between source and target for the same
    key. The hash (computed only from id+name, identical on both sides) would
    look like a clean HASH_MATCH -- run_table_hybrid must refuse rather than
    let that become a silent PASS."""
    source_full = pd.DataFrame({"id": [1], "name": ["a"], "amount": [100]})
    target_full = pd.DataFrame({"id": [1], "name": ["a"], "amount": [999]})
    hash_rows = [(1, "same-hash-of-id-and-name")]  # identical on both sides

    src_db = _FakeDB(source_full, hash_rows)
    tgt_db = _FakeDB(target_full, hash_rows)

    validation_config = {
        "pksourcecolumn": "id", "pktargetcolumn": "id",
        "sourcequery": "SELECT id, name, amount FROM source_t",
        "targetquery": "SELECT id, name, amount FROM target_t",
    }
    row_hash_config = {
        "sourcequery": "SELECT id AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT id AS record_key, h AS row_hash FROM target_t",
    }

    with patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        try:
            tiered_runner.run_table_hybrid(
                table_name="fixture_table", validation_name="data_validation",
                validation_config=validation_config, row_hash_config=row_hash_config,
                row_hash_columns=["id", "name"],
                source="postgresql", target="snowflake", environment="local", base_dir=".",
                source_database="", source_schema="", target_database="", target_schema="",
                output_path=".", run_id="test",
            )
            assert False, "expected RuntimeError for incomplete row_hash.columns coverage"
        except RuntimeError as exc:
            assert "amount" in str(exc), f"error should name the uncovered column: {exc}"
    print("test_incomplete_row_hash_coverage_refuses_rather_than_silently_passing: OK")


def test_hash_match_bucket_is_batched_not_built_as_one_unbounded_frame():
    """Phase 2 audit finding F3: HASH_MATCH keys (7, all matching -- nothing
    else to compare) must be written through the existing _batches()
    mechanism, never as one unbounded list/DataFrame for the whole set."""
    ids = list(range(1, 8))
    source_full = pd.DataFrame({"id": ids, "name": ["same"] * 7})
    target_full = pd.DataFrame({"id": ids, "name": ["same"] * 7})
    hash_rows = [(i, "same-hash") for i in ids]

    src_db = _FakeDB(source_full, hash_rows)
    tgt_db = _FakeDB(target_full, hash_rows)

    validation_config = {
        "pksourcecolumn": "id", "pktargetcolumn": "id",
        "sourcequery": "SELECT id, name FROM source_t",
        "targetquery": "SELECT id, name FROM target_t",
    }
    row_hash_config = {
        "sourcequery": "SELECT id AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT id AS record_key, h AS row_hash FROM target_t",
    }

    import tempfile
    write_sizes = []
    real_to_csv = pd.DataFrame.to_csv

    def counting_to_csv(self, *args, **kwargs):
        write_sizes.append(len(self))
        return real_to_csv(self, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp, \
         patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]), \
         patch("tiered_runner.TIER2_BATCH_SIZE", 3), \
         patch.object(pd.DataFrame, "to_csv", counting_to_csv):
        result = tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )
        result_csv = pd.read_csv(os.path.join(tmp, "fixture_table_data_validation_result_test.csv"))

    assert result["is_match"] is True
    assert len(result_csv) == 7
    assert set(result_csv["status"]) == {"PASS"}
    # 7 keys at a patched batch size of 3 -> three separate incremental
    # writes of sizes [3, 3, 1], never one write of all 7 at once.
    assert write_sizes == [3, 3, 1], f"expected batched writes [3,3,1], got {write_sizes}"
    print("test_hash_match_bucket_is_batched_not_built_as_one_unbounded_frame: OK")


def test_transformation_specs_propagate_through_tier2_batches():
    """A: non-empty transformation_specs, {name}_value present on both sides,
    both rows forced into Tier-2 (distinct hashes) -- hybrid's transformation
    columns must match compare_indexed_frames' direct output exactly."""
    source_full = pd.DataFrame({
        "id": [1, 2],
        "name": ["a", "b"],
        "amt_value": [100.0, 50.0],
    })
    target_full = pd.DataFrame({
        "id": [1, 2],
        "name": ["a", "b"],
        "amt_value": [100.0, 999.0],  # row 2 differs -> transformation FAIL
    })
    hash_rows_src = [(1, "h1-src"), (2, "h2-src")]
    hash_rows_tgt = [(1, "h1-tgt"), (2, "h2-tgt")]

    src_db = _FakeDB(source_full, hash_rows_src)
    tgt_db = _FakeDB(target_full, hash_rows_tgt)

    transformation_specs = [{"name": "amt"}]

    oracle_s, oracle_t = canonicalize_frames(source_full.copy(), target_full.copy())
    oracle = compare_indexed_frames(oracle_s, oracle_t, "id", "id", transformation_specs)
    oracle_by_key = oracle.set_index(oracle["row_key"].astype(str))

    validation_config = {
        "pksourcecolumn": "id", "pktargetcolumn": "id",
        "sourcequery": "SELECT id, name, amt_value FROM source_t",
        "targetquery": "SELECT id, name, amt_value FROM target_t",
    }
    row_hash_config = {
        "sourcequery": "SELECT id AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT id AS record_key, h AS row_hash FROM target_t",
    }

    import tempfile
    with tempfile.TemporaryDirectory() as tmp, \
         patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            transformation_specs=transformation_specs,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )
        result_csv = pd.read_csv(os.path.join(tmp, "fixture_table_data_validation_result_test.csv"))

    hybrid_by_key = result_csv.set_index(result_csv["row_key"].astype(str))
    for key in ["1", "2"]:
        for col in ("amt__expected", "amt__actual", "amt__difference", "amt__status"):
            assert str(hybrid_by_key.loc[key, col]) == str(oracle_by_key.loc[key, col]), (
                f"key={key} col={col}: hybrid={hybrid_by_key.loc[key, col]!r} "
                f"oracle={oracle_by_key.loc[key, col]!r}"
            )
    assert hybrid_by_key.loc["2", "amt__status"] == "FAIL"
    print("test_transformation_specs_propagate_through_tier2_batches: OK")


def test_hash_match_and_tier2_rows_share_transformation_columns():
    """B: id=1 is HASH_MATCH (never re-fetched), id=2 is HASH_MISMATCH (real
    Tier-2 fetch). Both rows must land in the same CSV with the same
    transformation column set -- a schema mismatch between the two buckets
    would corrupt this file on read, exactly like the original F3 bug did."""
    source_full = pd.DataFrame({
        "id": [1, 2],
        "name": ["a", "b"],
        "amt_value": [100.0, 50.0],
    })
    target_full = pd.DataFrame({
        "id": [1, 2],
        "name": ["a", "CHANGED"],
        "amt_value": [100.0, 999.0],
    })
    hash_rows_src = [(1, "same-hash"), (2, "src-hash-2")]
    hash_rows_tgt = [(1, "same-hash"), (2, "tgt-hash-2")]

    src_db = _FakeDB(source_full, hash_rows_src)
    tgt_db = _FakeDB(target_full, hash_rows_tgt)

    transformation_specs = [{"name": "amt"}]

    validation_config = {
        "pksourcecolumn": "id", "pktargetcolumn": "id",
        "sourcequery": "SELECT id, name, amt_value FROM source_t",
        "targetquery": "SELECT id, name, amt_value FROM target_t",
    }
    row_hash_config = {
        "sourcequery": "SELECT id AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT id AS record_key, h AS row_hash FROM target_t",
    }

    import tempfile
    with tempfile.TemporaryDirectory() as tmp, \
         patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            transformation_specs=transformation_specs,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )
        # A column-count/name mismatch between the HASH_MATCH batch and the
        # Tier-2 batch would corrupt this CSV -- read_csv succeeding at all
        # is itself part of the regression signal.
        result_csv = pd.read_csv(os.path.join(tmp, "fixture_table_data_validation_result_test.csv"))

    expected_cols = {"validation_type", "amt__expected", "amt__actual", "amt__difference", "amt__status"}
    assert expected_cols.issubset(set(result_csv.columns)), result_csv.columns.tolist()

    by_key = result_csv.set_index(result_csv["row_key"].astype(str))
    assert by_key.loc["1", "status"] == "PASS"  # HASH_MATCH row
    assert by_key.loc["2", "status"] == "FAIL"  # Tier-2 row
    assert by_key.loc["1", "validation_type"] == "TRANSFORMATION"
    assert by_key.loc["2", "validation_type"] == "TRANSFORMATION"
    print("test_hash_match_and_tier2_rows_share_transformation_columns: OK")


def test_default_empty_transformation_specs_unchanged():
    """C: transformation_specs=[] (today's real-world default) must produce
    exactly the same output shape hybrid_v1 already produced before this fix
    -- no validation_type / transformation columns anywhere."""
    source_full = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
    target_full = pd.DataFrame({"id": [1, 2], "name": ["a", "CHANGED"]})
    hash_rows_src = [(1, "same-hash"), (2, "src-hash-2")]
    hash_rows_tgt = [(1, "same-hash"), (2, "tgt-hash-2")]

    src_db = _FakeDB(source_full, hash_rows_src)
    tgt_db = _FakeDB(target_full, hash_rows_tgt)

    validation_config = {
        "pksourcecolumn": "id", "pktargetcolumn": "id",
        "sourcequery": "SELECT id, name FROM source_t",
        "targetquery": "SELECT id, name FROM target_t",
    }
    row_hash_config = {
        "sourcequery": "SELECT id AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT id AS record_key, h AS row_hash FROM target_t",
    }

    import tempfile
    with tempfile.TemporaryDirectory() as tmp, \
         patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            transformation_specs=[],
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )
        result_csv = pd.read_csv(os.path.join(tmp, "fixture_table_data_validation_result_test.csv"))

    assert "validation_type" not in result_csv.columns
    assert not any(c.endswith(("__expected", "__actual", "__difference")) for c in result_csv.columns)
    by_key = result_csv.set_index(result_csv["row_key"].astype(str))
    assert by_key.loc["1", "status"] == "PASS"
    assert by_key.loc["2", "status"] == "FAIL"
    print("test_default_empty_transformation_specs_unchanged: OK")


# --- hybrid_v1 expected_grain parity (docs/large-table-scalable-architecture §P.1) ---

def test_expected_grain_no_duplicates_is_zero():
    multimap = {"1": ["h1"], "2": ["h2"], "3": ["h3"]}
    assert tiered_runner._grain_duplicate_count(multimap) == 0
    print("test_expected_grain_no_duplicates_is_zero: OK")


def test_expected_grain_one_key_three_times_is_three():
    multimap = {"1": ["ha", "hb", "hc"]}
    assert tiered_runner._grain_duplicate_count(multimap) == 3
    print("test_expected_grain_one_key_three_times_is_three: OK")


def test_expected_grain_multiple_groups_matches_oracle():
    # PK "5" appears 3x, "8" appears 2x, "9" appears once -> expected 3+2=5,
    # matching validate_expected_grain's frame.duplicated(keep=False).sum().
    df = pd.DataFrame({"id": [5, 5, 5, 8, 8, 9]})
    oracle_failures = validate_expected_grain(
        df, df, {"expected_grain": "one_row_per_key", "grain_columns": ["id"]}
    )
    oracle_count = oracle_failures[0]["duplicate_rows"]
    assert oracle_count == 5, oracle_count

    multimap = {"5": ["h"] * 3, "8": ["h"] * 2, "9": ["h"]}
    assert tiered_runner._grain_duplicate_count(multimap) == oracle_count
    print("test_expected_grain_multiple_groups_matches_oracle: OK")


def test_expected_grain_source_target_duplicate_mismatch_fails_like_oracle():
    """id=5 is duplicated 3x in source but not at all in target -- both the
    hybrid engine and the untiered oracle must fail this table, and agree on
    which side is over-grain and by how many rows."""
    source_full = pd.DataFrame({
        "id": [1, 5, 5, 5],
        "name": ["a", "dup", "dup", "dup"],
    })
    target_full = pd.DataFrame({
        "id": [1, 5],
        "name": ["a", "dup"],
    })

    def h(name):
        return f"hash-{name}"

    source_hash_rows = [(row.id, h(row.name)) for row in source_full.itertuples()]
    target_hash_rows = [(row.id, h(row.name)) for row in target_full.itertuples()]
    src_db = _FakeDB(source_full, source_hash_rows)
    tgt_db = _FakeDB(target_full, target_hash_rows)

    validation_config = {
        "pksourcecolumn": "id", "pktargetcolumn": "id",
        "sourcequery": "SELECT id, name FROM source_t",
        "targetquery": "SELECT id, name FROM target_t",
        "expected_grain": "one_row_per_key",
        "grain_columns": ["id"],
    }
    row_hash_config = {
        "sourcequery": "SELECT id AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT id AS record_key, h AS row_hash FROM target_t",
    }

    oracle_grain_failures = validate_expected_grain(source_full, target_full, validation_config)
    assert len(oracle_grain_failures) == 1
    assert oracle_grain_failures[0]["side"] == "source"
    assert oracle_grain_failures[0]["duplicate_rows"] == 3

    import tempfile
    with tempfile.TemporaryDirectory() as tmp, \
         patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        result = tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )

    assert result["is_match"] is False
    assert len(result["grain_failures"]) == 1
    assert result["grain_failures"][0]["side"] == "source"
    assert result["grain_failures"][0]["duplicate_rows"] == 3
    print("test_expected_grain_source_target_duplicate_mismatch_fails_like_oracle: OK")


def test_expected_grain_non_pk_grain_columns_refuses():
    """grain_columns pointing at a column other than the hybrid key/PK can't
    be derived from the Tier-1 hash multimap -- must refuse, not guess."""
    try:
        tiered_runner._validate_expected_grain_hybrid(
            {"expected_grain": "one_row_per_key", "grain_columns": ["some_other_column"]},
            pk_col="id",
            src_hash={"1": ["h"]},
            tgt_hash={"1": ["h"]},
        )
        assert False, "expected RuntimeError for non-PK grain_columns"
    except RuntimeError as exc:
        assert "some_other_column" in str(exc), exc
    print("test_expected_grain_non_pk_grain_columns_refuses: OK")


def test_expected_grain_absent_is_unchanged():
    """No expected_grain configured -- existing (empty) behavior, no
    exception even when grain_columns looks unsupported, since the gate on
    expected_grain must short-circuit before that check runs."""
    failures = tiered_runner._validate_expected_grain_hybrid(
        {"grain_columns": ["some_other_column"]},
        pk_col="id",
        src_hash={"1": ["h", "h"]},
        tgt_hash={"1": ["h"]},
    )
    assert failures == []
    print("test_expected_grain_absent_is_unchanged: OK")


if __name__ == "__main__":
    test_hybrid_matches_untiered_oracle_on_row_level_status()
    test_incomplete_row_hash_coverage_refuses_rather_than_silently_passing()
    test_hash_match_bucket_is_batched_not_built_as_one_unbounded_frame()
    test_transformation_specs_propagate_through_tier2_batches()
    test_hash_match_and_tier2_rows_share_transformation_columns()
    test_default_empty_transformation_specs_unchanged()
    test_expected_grain_no_duplicates_is_zero()
    test_expected_grain_one_key_three_times_is_three()
    test_expected_grain_multiple_groups_matches_oracle()
    test_expected_grain_source_target_duplicate_mismatch_fails_like_oracle()
    test_expected_grain_non_pk_grain_columns_refuses()
    test_expected_grain_absent_is_unchanged()
    print("All tiered_runner differential checks passed.")
