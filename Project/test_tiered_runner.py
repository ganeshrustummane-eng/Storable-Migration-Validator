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
import re
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
    pre-split (record_key, row_hash) stream for its Tier-1 row-hash query.

    Also simulates the quality-check aggregate (`SELECT ... __total_rows ...`)
    and bounded-sample (`LIMIT`/`TOP`) queries tiered_runner's
    _run_quality_checks_hybrid generates -- computed with pandas against the
    same fixture, driven by the alias names actually present in the query
    text, so this exercises the real SQL-generation code, not a hand-picked
    stand-in result."""

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
        if "__total_rows" in query:
            return self._quality_aggregate(query)
        limit_match = re.search(r"LIMIT (\d+)", query) or re.search(r"TOP (\d+)", query)
        if limit_match:
            return self._full_df.head(int(limit_match.group(1))).reset_index(drop=True)
        return self._full_df

    def _quality_aggregate(self, query):
        df = self._full_df
        row = {"__total_rows": len(df)}
        for col in df.columns:
            if f"{col}__notnull" in query:
                row[f"{col}__notnull"] = int(df[col].notna().sum())
            if f"{col}__nullish" in query:
                row[f"{col}__nullish"] = int(df[col].apply(
                    lambda v: v is None or (isinstance(v, float) and v != v) or v == "<<NULL>>"
                ).sum())
            if f"{col}__distinct" in query:
                row[f"{col}__distinct"] = int(df[col].dropna().nunique())
            if any(f"{col}__{name}" in query for name in ("sum", "min", "max")):
                numeric = pd.to_numeric(df[col], errors="coerce")
                has_numeric = bool(numeric.notna().any())
                if f"{col}__sum" in query:
                    row[f"{col}__sum"] = float(numeric.sum()) if has_numeric else None
                if f"{col}__min" in query:
                    row[f"{col}__min"] = float(numeric.min()) if has_numeric else None
                if f"{col}__max" in query:
                    row[f"{col}__max"] = float(numeric.max()) if has_numeric else None
        return pd.DataFrame([row])


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


def test_duplicate_key_count_mismatch_produces_fail_not_silent_pass():
    """Duplicate-PK scalability edge case, isolated from expected_grain (no
    grain check configured here -- see test_expected_grain_source_target_
    duplicate_mismatch_fails_like_oracle for that separate concern). id=3 has
    3 identical-content rows in source, only 1 in target -- same content hash
    repeated a different number of times. _collect_hash_multimap stores one
    list entry per physical row (never collapses to a set), so the sorted
    hash lists differ by length alone -> hash_mismatch -> Tier 2 -> real
    fetch -> compare_indexed_frames' sorted-multiset-length check -> FAIL.
    A count-collapsing bug here would instead let this look like a clean
    HASH_MATCH and silently PASS."""
    source_full = pd.DataFrame({"id": [1, 2, 3, 3, 3], "name": ["a", "b", "dup", "dup", "dup"]})
    target_full = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "dup"]})

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
    }
    row_hash_config = {
        "sourcequery": "SELECT id AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT id AS record_key, h AS row_hash FROM target_t",
    }

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

    by_key = result_csv.set_index(result_csv["row_key"].astype(str))
    assert by_key.loc["1", "status"] == "PASS"
    assert by_key.loc["2", "status"] == "PASS"
    assert by_key.loc["3", "status"] == "FAIL"  # 3-vs-1 duplicate count -> real mismatch
    assert result["is_match"] is False
    print("test_duplicate_key_count_mismatch_produces_fail_not_silent_pass: OK")


def test_pk_less_duplicate_rows_matching_count_passes():
    """PK-less scalability edge case: no pksourcecolumn/pktargetcolumn
    configured, so record_key falls back to the content hash itself
    (sql_query_generator._row_hash_queries). Two physically identical rows
    on each side hash to the same key, appearing as a 2-entry list on both
    sides -- equal-length sorted multisets -> HASH_MATCH -> clean PASS,
    without ever needing a real PK to key off of."""
    hash_rows = [("same-hash", "same-hash"), ("same-hash", "same-hash")]
    src_full = pd.DataFrame({"a": [1, 1]})
    tgt_full = pd.DataFrame({"a": [1, 1]})
    src_db = _FakeDB(src_full, hash_rows)
    tgt_db = _FakeDB(tgt_full, hash_rows)

    validation_config = {
        "sourcequery": "SELECT a FROM source_t",
        "targetquery": "SELECT a FROM target_t",
    }
    row_hash_config = {
        "sourcequery": "SELECT h AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT h AS record_key, h AS row_hash FROM target_t",
    }

    with patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        result = tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=".", run_id="test",
        )
    assert result["is_match"] is True
    print("test_pk_less_duplicate_rows_matching_count_passes: OK")


def test_pk_less_duplicate_count_mismatch_refuses_rather_than_guessing():
    """PK-less table where source has one more physically-identical row than
    target under the same content-hash key (3 vs 2) -- there's no real PK to
    narrow a targeted re-fetch by, so run_table_hybrid must refuse (raise)
    rather than guess which rows correspond, per its own documented
    contract (see run_table_hybrid's is_pk_less branch)."""
    hash_rows_src = [("same-hash", "same-hash")] * 3
    hash_rows_tgt = [("same-hash", "same-hash")] * 2
    src_full = pd.DataFrame({"a": [1, 1, 1]})
    tgt_full = pd.DataFrame({"a": [1, 1]})
    src_db = _FakeDB(src_full, hash_rows_src)
    tgt_db = _FakeDB(tgt_full, hash_rows_tgt)

    validation_config = {
        "sourcequery": "SELECT a FROM source_t",
        "targetquery": "SELECT a FROM target_t",
    }
    row_hash_config = {
        "sourcequery": "SELECT h AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT h AS record_key, h AS row_hash FROM target_t",
    }

    with patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        try:
            tiered_runner.run_table_hybrid(
                table_name="fixture_table", validation_name="data_validation",
                validation_config=validation_config, row_hash_config=row_hash_config,
                source="postgresql", target="snowflake", environment="local", base_dir=".",
                source_database="", source_schema="", target_database="", target_schema="",
                output_path=".", run_id="test",
            )
            assert False, "expected RuntimeError for PK-less duplicate-count mismatch"
        except RuntimeError as exc:
            assert "PK-less" in str(exc) or "row-hash" in str(exc).lower() or "primary key" in str(exc).lower()
    print("test_pk_less_duplicate_count_mismatch_refuses_rather_than_guessing: OK")


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


# --- hybrid_v1 run_quality_checks parity (docs/large-table-scalable-architecture §R/§S) ---
# Pure-helper unit tests first (no DB), then end-to-end tests through
# run_table_hybrid using the extended _FakeDB above.

def test_null_rate_hybrid_placeholder_contract_intentionally_diverges_from_oracle():
    """Documents the null_rate divergence flagged in the live comparison
    (docs/large-table-scalable-architecture): once a column is COALESCE'd to
    the '<<NULL>>' sentinel string (base_rules.py's default path), the
    oracle's run_quality_checks never sees a real null there at all --
    isna() doesn't match a non-null string, so its null_rate is 0%.
    hybrid_v1's contract is explicitly different (SQL NULL OR text = sentinel
    both count as null-equivalent) -- kept as-is per this task's constraint
    against silently changing that semantic again; this test is the retained
    live evidence of the difference, not a bug."""
    from utils import quality_checks as oracle_qc

    source_df = pd.DataFrame({"amount": ["<<NULL>>", "10.00"]})
    target_df = pd.DataFrame({"amount": ["<<NULL>>", "10.00"]})
    oracle_failures = oracle_qc.run_quality_checks(
        source_df, target_df, {"quality_checks": {"null_rate_tolerance_pct": 0}},
    )
    assert oracle_failures == [], oracle_failures  # oracle: sentinel isn't isna() -> 0% both sides, no failure

    src_stats = {"__total_rows": 2, "amount__nullish": 1}  # hybrid: sentinel counted as null
    tgt_stats = {"__total_rows": 2, "amount__nullish": 0}
    hybrid_failures = tiered_runner._null_rate_failures(src_stats, tgt_stats, ["amount"], tolerance=0)
    assert len(hybrid_failures) == 1  # hybrid: 50% vs 0% -> flags a difference the oracle can't see
    print("test_null_rate_hybrid_placeholder_contract_intentionally_diverges_from_oracle: OK")


def test_null_rate_failures_catches_real_null_and_sentinel_uniformly():
    """`{col}__nullish` (built from `col IS NULL OR CAST(col) = '<<NULL>>'`)
    already unifies both YAML-generation paths (§R.1) -- this test only
    exercises the tolerance math on top of that pre-computed count."""
    src_stats = {"__total_rows": 4, "amount__nullish": 0}
    tgt_stats = {"__total_rows": 4, "amount__nullish": 2}
    failures = tiered_runner._null_rate_failures(src_stats, tgt_stats, ["amount"], tolerance=0)
    assert len(failures) == 1
    assert failures[0] == {
        "check": "null_rate", "column": "amount", "source": 0.0, "target": 50.0, "tolerance": 0,
    }
    print("test_null_rate_failures_catches_real_null_and_sentinel_uniformly: OK")


def test_null_rate_tolerance_boundary_is_strict_greater_than():
    src_stats = {"__total_rows": 100, "x__nullish": 10}
    tgt_stats = {"__total_rows": 100, "x__nullish": 15}
    assert tiered_runner._null_rate_failures(src_stats, tgt_stats, ["x"], tolerance=5) == []
    assert len(tiered_runner._null_rate_failures(src_stats, tgt_stats, ["x"], tolerance=4.999)) == 1
    print("test_null_rate_tolerance_boundary_is_strict_greater_than: OK")


def test_distinct_count_null_presence_correction_matches_dropna_false():
    """§R.2.2.3: COUNT(DISTINCT) excludes NULL; nunique(dropna=False) counts it
    as one more category. source has a NULL (4 of 5 notnull, 3 distinct
    notnull) -> corrected 4; target has none (5 of 5 notnull, 4 distinct) ->
    corrected 4. Must agree once the correction is applied."""
    src_stats = {"__total_rows": 5, "amount__notnull": 4, "amount__distinct": 3}
    tgt_stats = {"__total_rows": 5, "amount__notnull": 5, "amount__distinct": 4}
    assert tiered_runner._distinct_count_failures(src_stats, tgt_stats, ["amount"], tolerance=0) == []
    print("test_distinct_count_null_presence_correction_matches_dropna_false: OK")


def test_distinct_count_real_mismatch_still_fails():
    src_stats = {"__total_rows": 5, "amount__notnull": 5, "amount__distinct": 3}
    tgt_stats = {"__total_rows": 5, "amount__notnull": 5, "amount__distinct": 5}
    failures = tiered_runner._distinct_count_failures(src_stats, tgt_stats, ["amount"], tolerance=0)
    assert len(failures) == 1 and failures[0]["source"] == 3 and failures[0]["target"] == 5
    print("test_distinct_count_real_mismatch_still_fails: OK")


def test_canonicalization_risk_columns_flags_json_and_numeric_string_not_plain():
    df = pd.DataFrame({
        "payload": ['{"b": 2, "a": 1}', '{"c": 3}'],
        "amount_txt": ["400000.000000", "10.50"],
        "name": ["alice", "bob"],
    })
    risky = tiered_runner._canonicalization_risk_columns(df, ["payload", "amount_txt", "name"])
    assert risky == {"payload", "amount_txt"}
    print("test_canonicalization_risk_columns_flags_json_and_numeric_string_not_plain: OK")


def test_numeric_sample_columns_matches_pd_to_numeric_any():
    df = pd.DataFrame({"amount": ["10", "abc"], "name": ["alice", "bob"], "empty": [None, None]})
    assert tiered_runner._numeric_sample_columns(df, ["amount", "name", "empty"]) == ["amount"]
    print("test_numeric_sample_columns_matches_pd_to_numeric_any: OK")


def test_dialect_numeric_cast_uses_try_cast_or_regex_guard():
    for dialect in ("mssql", "snowflake", "athena", "trino", "presto"):
        assert "TRY_CAST" in tiered_runner._dialect_numeric_cast(dialect, "col"), dialect
    for dialect in ("postgresql", "redshift"):
        cast = tiered_runner._dialect_numeric_cast(dialect, "col")
        assert "CASE WHEN" in cast and "~" in cast, dialect
    print("test_dialect_numeric_cast_uses_try_cast_or_regex_guard: OK")


def test_quality_aggregate_sql_only_includes_safe_columns():
    sql = tiered_runner._quality_aggregate_sql(
        "SELECT id, amount, payload FROM t", ["id", "amount", "payload"],
        distinct_columns=["id", "amount"], numeric_columns=["amount"], dialect="postgresql",
    )
    assert "id__notnull" in sql and "amount__notnull" in sql and "payload__notnull" in sql
    assert "id__distinct" in sql and "amount__distinct" in sql
    assert "payload__distinct" not in sql  # excluded (canonicalization risk), not silently wrong
    assert "amount__sum" in sql and "amount__min" in sql and "amount__max" in sql
    assert "id__sum" not in sql  # not classified numeric
    print("test_quality_aggregate_sql_only_includes_safe_columns: OK")


def test_sample_hash_failures_caps_absolute_size():
    """§R.2.4.6: sample_hash_percent=100% of a million-row table must still
    only ever fetch SAMPLE_HASH_ABSOLUTE_CAP rows per side, not the whole
    table -- confirmed by asserting the LIMIT this issues, not just the
    result."""
    calls = []

    class _CapDB:
        def execute_query(self, query):
            m = re.search(r"LIMIT (\d+)", query)
            n = int(m.group(1))
            calls.append(n)
            return pd.DataFrame({"id": list(range(n)), "name": ["x"] * n})

    db = _CapDB()
    with patch("tiered_runner.SAMPLE_HASH_ABSOLUTE_CAP", 5):
        failures = tiered_runner._sample_hash_failures(
            db, db, "postgresql", "postgresql",
            "SELECT id, name FROM t", "SELECT id, name FROM t",
            ["id", "name"], sample_percent=100, src_total_rows=1_000_000, tgt_total_rows=1_000_000,
        )
    assert calls == [5, 5], calls
    assert failures == []
    print("test_sample_hash_failures_caps_absolute_size: OK")


def test_quote_ident_snowflake_style_lowercase_double_quotes():
    """Base-rule-generated Snowflake targetqueries emit AS "id_normalized"
    (quoted lowercase) specifically so Snowflake's default uppercase
    identifier folding doesn't apply -- an outer reference must quote the
    same way or Snowflake resolves it to ID_NORMALIZED, which doesn't
    exist (the live CRITICAL bug this guards)."""
    assert tiered_runner._quote_ident("snowflake", "id_normalized") == '"id_normalized"'
    assert tiered_runner._quote_ident("postgresql", "id_normalized") == '"id_normalized"'
    assert tiered_runner._quote_ident("mssql", "id_normalized") == "[id_normalized]"
    # Escaping: a literal quote/bracket character in the name itself must not
    # break out of the quoting.
    assert tiered_runner._quote_ident("snowflake", 'a"b') == '"a""b"'
    assert tiered_runner._quote_ident("mssql", "a]b") == "[a]]b]"
    print("test_quote_ident_snowflake_style_lowercase_double_quotes: OK")


def test_fetch_batch_quotes_pk_reference_for_snowflake():
    """Regression for the live CRITICAL divergence: WHERE id_normalized IN
    (...) against a Snowflake-wrapped query that projects "id_normalized"
    (quoted lowercase) must reference it quoted, or Snowflake folds the
    unquoted reference to ID_NORMALIZED and the WHERE clause targets a
    column that doesn't exist."""
    captured = {}

    class _RecordingDB:
        def execute_query(self, query):
            captured["sql"] = query
            return pd.DataFrame({"id_normalized": [1]})

    tiered_runner._fetch_batch(_RecordingDB(), "snowflake", "SELECT * FROM t", "id_normalized", [1, 2])
    assert '"id_normalized" IN' in captured["sql"], captured["sql"]

    tiered_runner._fetch_batch(_RecordingDB(), "mssql", "SELECT * FROM t", "id_normalized", [1])
    assert "[id_normalized] IN" in captured["sql"], captured["sql"]
    print("test_fetch_batch_quotes_pk_reference_for_snowflake: OK")


def test_quality_aggregate_sql_quotes_every_column_reference():
    """Same bug, quality-check aggregate path: every COUNT/SUM/MIN/MAX/
    COUNT(DISTINCT) reference into the wrapped query must be quoted, not
    just the pk used by Tier-2's narrowed fetch."""
    sql = tiered_runner._quality_aggregate_sql(
        "SELECT * FROM t", ["id_normalized", "amount_normalized"],
        distinct_columns=["id_normalized"], numeric_columns=["amount_normalized"],
        dialect="snowflake",
    )
    assert '"id_normalized"' in sql and '"amount_normalized"' in sql
    assert "COUNT(id_normalized)" not in sql
    assert "SUM(id_normalized" not in sql
    print("test_quality_aggregate_sql_quotes_every_column_reference: OK")


def test_dialect_numeric_cast_round2_wraps_round():
    cast = tiered_runner._dialect_numeric_cast("snowflake", '"amount_normalized"', round2=True)
    assert cast.startswith("ROUND(") and cast.endswith(", 2)")
    cast_no_round = tiered_runner._dialect_numeric_cast("snowflake", '"amount_normalized"', round2=False)
    assert not cast_no_round.startswith("ROUND(")
    print("test_dialect_numeric_cast_round2_wraps_round: OK")


def test_numeric_string_round_columns_flags_decimal_strings_only():
    df = pd.DataFrame({
        "amount_txt": ["0.1234", "10.50"],
        "count_txt": ["5", "10"],
        "name": ["a", "b"],
    })
    flagged = tiered_runner._numeric_string_round_columns(df, ["amount_txt", "count_txt", "name"])
    assert flagged == {"amount_txt"}
    print("test_numeric_string_round_columns_flags_decimal_strings_only: OK")


def test_quality_aggregate_sql_rounds_numeric_string_columns():
    """Live divergence repro: raw '0.1234' sums to 1.2334 in unrounded SQL,
    but the oracle rounds numeric-string columns to 2dp (via
    canonicalize_frames) *before* summing, giving 1.23. round2_columns must
    make the SQL SUM apply the same ROUND(...,2) the oracle effectively
    applies per-row."""
    sql = tiered_runner._quality_aggregate_sql(
        "SELECT * FROM t", ["amount_txt"], distinct_columns=[], numeric_columns=["amount_txt"],
        dialect="postgresql", round2_columns={"amount_txt"},
    )
    assert "ROUND(" in sql
    sql_unrounded = tiered_runner._quality_aggregate_sql(
        "SELECT * FROM t", ["amount_txt"], distinct_columns=[], numeric_columns=["amount_txt"],
        dialect="postgresql", round2_columns=set(),
    )
    assert "ROUND(" not in sql_unrounded
    print("test_quality_aggregate_sql_rounds_numeric_string_columns: OK")


def test_sample_hash_failures_canonicalizes_before_hashing():
    """Oracle parity: Project/main.py canonicalizes both frames before
    run_quality_checks ever hashes them. A JSON column with reordered keys
    (semantically equal, textually different) must hash equal here too --
    hashing the raw fetched sample (pre-fix behavior) would flag this as a
    sample_hash mismatch every single run."""
    class _JsonDB:
        def __init__(self, rows):
            self._rows = rows

        def execute_query(self, query):
            return pd.DataFrame(self._rows)

    src_db = _JsonDB({"id": [1], "payload": ['{"a": 1, "b": 2}']})
    tgt_db = _JsonDB({"id": [1], "payload": ['{"b": 2, "a": 1}']})

    failures = tiered_runner._sample_hash_failures(
        src_db, tgt_db, "postgresql", "snowflake", "SELECT * FROM t", "SELECT * FROM t",
        ["id", "payload"], sample_percent=100, src_total_rows=1, tgt_total_rows=1,
    )
    assert failures == [], failures
    print("test_sample_hash_failures_canonicalizes_before_hashing: OK")


def test_sample_hash_failures_noop_without_fetching_when_inactive():
    class _BoomDB:
        def execute_query(self, query):
            raise AssertionError("sample_hash must not fetch when inactive")

    db = _BoomDB()
    assert tiered_runner._sample_hash_failures(db, db, "postgresql", "postgresql", "q", "q", ["id"], 0, 100, 100) == []
    assert tiered_runner._sample_hash_failures(db, db, "postgresql", "postgresql", "q", "q", ["id"], 50, 0, 100) == []
    assert tiered_runner._sample_hash_failures(db, db, "postgresql", "postgresql", "q", "q", [], 50, 100, 100) == []
    print("test_sample_hash_failures_noop_without_fetching_when_inactive: OK")


def _quality_fixture(source_full, target_full, hash_rows_src, hash_rows_tgt, quality_checks_config,
                      sourcecols="id, amount"):
    src_db = _FakeDB(source_full, hash_rows_src)
    tgt_db = _FakeDB(target_full, hash_rows_tgt)
    validation_config = {
        "pksourcecolumn": "id", "pktargetcolumn": "id",
        "sourcequery": f"SELECT {sourcecols} FROM source_t",
        "targetquery": f"SELECT {sourcecols} FROM target_t",
        "quality_checks": quality_checks_config,
    }
    row_hash_config = {
        "sourcequery": "SELECT id AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT id AS record_key, h AS row_hash FROM target_t",
    }
    return src_db, tgt_db, validation_config, row_hash_config


def test_quality_checks_hybrid_null_rate_drift_end_to_end():
    """Both sides HASH_MATCH on every key (row-level comparison alone would
    PASS) -- only the quality-check gate can flip is_match here, isolating
    that it actually runs and actually flips it."""
    source_full = pd.DataFrame({"id": [1, 2, 3, 4], "amount": [10, 20, 30, 40]})
    target_full = pd.DataFrame({"id": [1, 2, 3, 4], "amount": [10, 20, None, None]})
    hash_rows = [(i, "same") for i in [1, 2, 3, 4]]
    src_db, tgt_db, validation_config, row_hash_config = _quality_fixture(
        source_full, target_full, hash_rows, hash_rows,
        {"enabled": True, "null_rate_tolerance_pct": 0},
    )

    import tempfile
    with tempfile.TemporaryDirectory() as tmp, patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        result = tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )

    assert result["is_match"] is False
    null_failures = [f for f in result["quality_failures"] if f["check"] == "null_rate"]
    assert len(null_failures) == 1
    assert null_failures[0]["column"] == "amount"
    assert null_failures[0]["source"] == 0.0
    assert null_failures[0]["target"] == 50.0
    print("test_quality_checks_hybrid_null_rate_drift_end_to_end: OK")


def test_quality_checks_hybrid_distinct_count_skips_canonicalization_risk_column():
    """source's payload is the same JSON text on both rows (raw distinct=1);
    target's is the same value with keys reordered on row 2 (raw distinct=2,
    canonical distinct=1). A naive SQL COUNT(DISTINCT) would flag this as a
    real drift -- it must be excluded instead, per §R.2.2/§R.3.1."""
    source_full = pd.DataFrame({"id": [1, 2], "payload": ['{"a": 1, "b": 2}', '{"a": 1, "b": 2}']})
    target_full = pd.DataFrame({"id": [1, 2], "payload": ['{"a": 1, "b": 2}', '{"b": 2, "a": 1}']})
    hash_rows = [(1, "same"), (2, "same")]
    src_db, tgt_db, validation_config, row_hash_config = _quality_fixture(
        source_full, target_full, hash_rows, hash_rows,
        {"enabled": True, "distinct_count_tolerance": 0},
        sourcecols="id, payload",
    )

    import tempfile
    with tempfile.TemporaryDirectory() as tmp, patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        result = tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )

    assert not any(f.get("column") == "payload" for f in result["quality_failures"]), result["quality_failures"]
    assert result["is_match"] is True
    print("test_quality_checks_hybrid_distinct_count_skips_canonicalization_risk_column: OK")


def test_quality_checks_hybrid_aggregate_sum_drift_end_to_end():
    source_full = pd.DataFrame({"id": [1, 2], "amount": [100.0, 200.0]})
    target_full = pd.DataFrame({"id": [1, 2], "amount": [100.0, 999.0]})
    hash_rows = [(1, "same"), (2, "same")]
    src_db, tgt_db, validation_config, row_hash_config = _quality_fixture(
        source_full, target_full, hash_rows, hash_rows,
        {
            "enabled": True,
            "null_rate_tolerance_pct": 100,
            "distinct_count_tolerance": 1000,
            "aggregate_tolerance_pct": 0,
        },
    )

    import tempfile
    with tempfile.TemporaryDirectory() as tmp, patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        result = tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )

    assert result["is_match"] is False
    sum_failures = [f for f in result["quality_failures"] if f["check"] == "sum" and f["column"] == "amount"]
    assert len(sum_failures) == 1
    print("test_quality_checks_hybrid_aggregate_sum_drift_end_to_end: OK")


def test_quality_checks_hybrid_empty_tables_no_crash():
    source_full = pd.DataFrame({"id": pd.Series(dtype="int64"), "amount": pd.Series(dtype="float64")})
    target_full = pd.DataFrame({"id": pd.Series(dtype="int64"), "amount": pd.Series(dtype="float64")})
    src_db, tgt_db, validation_config, row_hash_config = _quality_fixture(
        source_full, target_full, [], [],
        {"enabled": True, "sample_hash_percent": 50},
    )

    import tempfile
    with tempfile.TemporaryDirectory() as tmp, patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        result = tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )

    assert result["quality_failures"] == []
    assert result["is_match"] is True
    print("test_quality_checks_hybrid_empty_tables_no_crash: OK")


def test_quality_checks_hybrid_pk_less_early_return_has_no_quality_failures_key():
    """PK-less tables never reach the quality-check region -- same documented
    limitation as expected_grain (§Q.3). main.py's
    hybrid_result.get("quality_failures", []) must default safely here, not
    be mistaken for "checked and clean"."""
    hash_rows = [("k1", "same-hash")]
    src_db = _FakeDB(pd.DataFrame({"a": [1]}), hash_rows)
    tgt_db = _FakeDB(pd.DataFrame({"a": [1]}), hash_rows)
    validation_config = {
        "sourcequery": "SELECT a FROM source_t",
        "targetquery": "SELECT a FROM target_t",
        "quality_checks": {"enabled": True},
    }
    row_hash_config = {
        "sourcequery": "SELECT k AS record_key, h AS row_hash FROM source_t",
        "targetquery": "SELECT k AS record_key, h AS row_hash FROM target_t",
    }

    import tempfile
    with tempfile.TemporaryDirectory() as tmp, patch("tiered_runner.get_database", side_effect=[src_db, tgt_db]):
        result = tiered_runner.run_table_hybrid(
            table_name="fixture_table", validation_name="data_validation",
            validation_config=validation_config, row_hash_config=row_hash_config,
            source="postgresql", target="snowflake", environment="local", base_dir=".",
            source_database="", source_schema="", target_database="", target_schema="",
            output_path=tmp, run_id="test",
        )

    assert "quality_failures" not in result
    assert result["is_match"] is True
    print("test_quality_checks_hybrid_pk_less_early_return_has_no_quality_failures_key: OK")


if __name__ == "__main__":
    test_hybrid_matches_untiered_oracle_on_row_level_status()
    test_duplicate_key_count_mismatch_produces_fail_not_silent_pass()
    test_pk_less_duplicate_rows_matching_count_passes()
    test_pk_less_duplicate_count_mismatch_refuses_rather_than_guessing()
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
    test_null_rate_hybrid_placeholder_contract_intentionally_diverges_from_oracle()
    test_null_rate_failures_catches_real_null_and_sentinel_uniformly()
    test_null_rate_tolerance_boundary_is_strict_greater_than()
    test_distinct_count_null_presence_correction_matches_dropna_false()
    test_distinct_count_real_mismatch_still_fails()
    test_canonicalization_risk_columns_flags_json_and_numeric_string_not_plain()
    test_numeric_sample_columns_matches_pd_to_numeric_any()
    test_dialect_numeric_cast_uses_try_cast_or_regex_guard()
    test_quality_aggregate_sql_only_includes_safe_columns()
    test_quote_ident_snowflake_style_lowercase_double_quotes()
    test_fetch_batch_quotes_pk_reference_for_snowflake()
    test_quality_aggregate_sql_quotes_every_column_reference()
    test_dialect_numeric_cast_round2_wraps_round()
    test_numeric_string_round_columns_flags_decimal_strings_only()
    test_quality_aggregate_sql_rounds_numeric_string_columns()
    test_sample_hash_failures_canonicalizes_before_hashing()
    test_sample_hash_failures_caps_absolute_size()
    test_sample_hash_failures_noop_without_fetching_when_inactive()
    test_quality_checks_hybrid_null_rate_drift_end_to_end()
    test_quality_checks_hybrid_distinct_count_skips_canonicalization_risk_column()
    test_quality_checks_hybrid_aggregate_sum_drift_end_to_end()
    test_quality_checks_hybrid_empty_tables_no_crash()
    test_quality_checks_hybrid_pk_less_early_return_has_no_quality_failures_key()
    print("All tiered_runner differential checks passed.")
