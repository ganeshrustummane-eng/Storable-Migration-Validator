"""Hybrid Tier-1/Tier-2 large-table validation engine.

Opt-in per table (Project/main.py dispatches here only when
validation_plan.execution_strategy == "hybrid_v1" and a populated
row_hash_validation block exists — see docs/large-table-scalable-architecture).

Tier 1: run the already-generated, dialect-aware SQL row-hash queries
(src/generated_queries/sql_query_generator.py's _row_hash_queries /
_hash_expression) on both sides, streamed in bounded chunks, to classify every
key as SOURCE_ONLY / TARGET_ONLY / HASH_MATCH / HASH_MISMATCH without ever
materializing full-width rows for the whole table.

Tier 2: for anything Tier 1 couldn't confidently resolve, re-fetch only that
narrow slice of full rows (via get_database().execute_query() on the
*unmodified* sourcequery/targetquery, wrapped as a subquery so every
Fivetran/exclusion/filter clause in the original query text is preserved
exactly) and run it through the *same* canonicalize_frames +
compare_indexed_frames used by the default engine — one comparison
implementation, not two.

Scope of this slice (see plan): single-column PK or row_hash-keyed (PK-less)
tables only. Composite-PK narrowing and Athena's CTAS-to-S3 fast-follow are
explicitly not built here.
"""

import os

import pandas as pd

from db.factory import get_database
from utils.row_compare import compare_indexed_frames
from utils.semantic_normalize import canonicalize_frames
from utils.utility import get_logger

logger = get_logger(__name__)

# ponytail: in-memory dict, not an external sort-merge — (key,hash) pairs are
# the smallest structure in this whole pipeline; swap for an external merge
# only if a real table's key cardinality makes this too big (unmeasured today,
# see docs/large-table-scalable-architecture §N/§P).
TIER1_FETCH_CHUNK = 50_000
TIER2_BATCH_SIZE = 2_000


def _strip_trailing_semicolon(sql):
    return sql.rstrip().rstrip(";")


def _sql_literal(value):
    """Escape a Tier-1-derived key value as a SQL literal for a WHERE ... IN (...)
    clause. Values here come from our own row-hash query result, not raw user
    input, but they're escaped anyway rather than trusted, per CLAUDE.md's
    injection-avoidance guidance."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _probe_columns(db_obj, query):
    """Zero-row schema probe — learns column names without transferring data,
    by wrapping the unmodified query exactly like the real Tier-2 narrowed
    fetch does. Needed so HASH_MATCH rows (never fetched) can still be written
    with the same column set as FAIL/SOURCE_ONLY/TARGET_ONLY rows."""
    probe_sql = f"SELECT * FROM ({_strip_trailing_semicolon(query)}) AS probe_t WHERE 1 = 0"
    df = db_obj.execute_query(probe_sql)
    return [c.strip().lower() for c in df.columns]


def _collect_hash_multimap(db_obj, query):
    """Stream a Tier-1 row-hash query, return {record_key_str: [row_hash, ...]}.

    A list (not a single value) per key so duplicate keys are handled as
    multisets of hashes — a duplicate-count mismatch (2 rows on one side, 1 on
    the other) must never be classified as HASH_MATCH just because one pair of
    hashes happens to agree (see design doc §I).

    Hash strings are lower-cased before storage — SHA256 hex output case
    differs by dialect (MSSQL's HASHBYTES/CONVERT(...,2) is uppercase; the
    others are lowercase), which would otherwise make every row look mismatched
    for MSSQL sources even when the underlying data is identical.
    """
    result: dict[str, list[str]] = {}
    for chunk in db_obj.execute_query_stream(query, chunksize=TIER1_FETCH_CHUNK):
        chunk.columns = chunk.columns.str.strip().str.lower()
        for key_val, hash_val in zip(chunk["record_key"], chunk["row_hash"]):
            key_str = str(key_val)
            hash_str = str(hash_val).lower() if hash_val is not None else "<<NULL>>"
            result.setdefault(key_str, []).append(hash_str)
    for key_str in result:
        result[key_str].sort()
    return result


def _empty_frame(columns):
    return pd.DataFrame(columns=columns)


def _grain_duplicate_count(hash_multimap):
    """Rows belonging to a duplicate-key group, summed across every group --
    exact parity with quality_checks.validate_expected_grain's
    `frame.duplicated(columns, keep=False).sum()`: a key with N physical rows
    (N>1) contributes N, not N-1 (keep=False marks every row in the group,
    not just the extras)."""
    return sum(len(hashes) for hashes in hash_multimap.values() if len(hashes) > 1)


def _validate_expected_grain_hybrid(validation_config, pk_col, src_hash, tgt_hash):
    """hybrid_v1 parity for quality_checks.validate_expected_grain(), computed
    from the Tier-1 hash multimap instead of a materialized frame (no full
    frame exists in this engine to call .duplicated() on).

    Only supports the case where grain_columns resolves to the same single
    column already used as the Tier-1/Tier-2 key -- that key's multimap
    already carries one hash entry per physical row per key, which is exactly
    what a duplicate-row count needs. Any other grain_columns configuration
    can't be derived from this multimap and is refused rather than guessed.
    """
    if validation_config.get("expected_grain") not in {"one_row_per_key", "one_row_per_driving_key"}:
        return []

    grain_columns = validation_config.get("grain_columns") or validation_config.get("pksourcecolumn")
    if isinstance(grain_columns, str):
        grain_columns = [grain_columns]
    grain_columns = [str(c).strip().lower() for c in (grain_columns or [])]

    if grain_columns != [pk_col]:
        raise RuntimeError(
            f"hybrid_v1 does not support expected_grain for grain_columns={grain_columns} "
            f"(the Tier-1/Tier-2 key for this table is '{pk_col}') -- this optimization only "
            "derives duplicate-row counts from the existing Tier-1 hash multimap when "
            "grain_columns resolves to that same key. Configure grain_columns to the PK, or "
            "remove execution_strategy: hybrid_v1 for this table."
        )

    failures = []
    for side, hash_multimap in (("source", src_hash), ("target", tgt_hash)):
        duplicate_count = _grain_duplicate_count(hash_multimap)
        if duplicate_count:
            failures.append({
                "check": "expected_grain",
                "side": side,
                "grain_columns": grain_columns,
                "duplicate_rows": duplicate_count,
                "detail": "many-to-many or one-to-many join changed declared grain",
            })
    return failures


def _fetch_batch(db_obj, query, pk_col, key_values):
    if not key_values:
        return None
    in_list = ", ".join(_sql_literal(v) for v in key_values)
    wrapped = (
        f"SELECT * FROM ({_strip_trailing_semicolon(query)}) AS t "
        f"WHERE {pk_col} IN ({in_list})"
    )
    df = db_obj.execute_query(wrapped)
    df.columns = df.columns.str.strip().str.lower()
    return df


def _append_result_batch(result_df, filepath, failed_filepath, wrote_header_flags):
    """Append one batch's rows to the result CSV (and failed CSV, if any rows
    in this batch aren't PASS) — never builds one full-table DataFrame."""
    header = not wrote_header_flags["result"]
    result_df.to_csv(filepath, mode="a", header=header, index=False)
    wrote_header_flags["result"] = True

    failed_batch = result_df[result_df["status"] != "PASS"]
    if not failed_batch.empty:
        failed_header = not wrote_header_flags["failed"]
        failed_batch.to_csv(failed_filepath, mode="a", header=failed_header, index=False)
        wrote_header_flags["failed"] = True


def run_table_hybrid(table_name, validation_name, validation_config, row_hash_config,
                      source, target, environment, base_dir,
                      source_database, source_schema, target_database, target_schema,
                      output_path, run_id, row_hash_columns=None, transformation_specs=None):
    pk_source_col = validation_config.get("pksourcecolumn")
    pk_target_col = validation_config.get("pktargetcolumn")
    is_pk_less = not pk_source_col or not pk_target_col
    if not is_pk_less and (isinstance(pk_source_col, list) or isinstance(pk_target_col, list)):
        raise NotImplementedError(
            f"hybrid_v1 does not support composite PKs yet (table={table_name}) — "
            "fast-follow, see docs/large-table-scalable-architecture. Remove "
            "execution_strategy: hybrid_v1 for this table for now."
        )
    pk_col = None if is_pk_less else str(pk_source_col).strip().lower()

    src_db = get_database(source, base_dir, environment,
                           override_database=source_database, override_schema=source_schema)
    tgt_db = get_database(target, base_dir, environment,
                           override_database=target_database, override_schema=target_schema)

    logger.info("hybrid_v1 Tier 1: streaming row-hash keys for table=%s", table_name)
    src_hash = _collect_hash_multimap(src_db, row_hash_config["sourcequery"])
    tgt_hash = _collect_hash_multimap(tgt_db, row_hash_config["targetquery"])

    src_keys = set(src_hash)
    tgt_keys = set(tgt_hash)
    source_only_keys = src_keys - tgt_keys
    target_only_keys = tgt_keys - src_keys
    common_keys = src_keys & tgt_keys
    hash_mismatch_keys = {k for k in common_keys if src_hash[k] != tgt_hash[k]}

    source_rows = sum(len(v) for v in src_hash.values())
    target_rows = sum(len(v) for v in tgt_hash.values())

    logger.info(
        "hybrid_v1 Tier 1 classification for table=%s: source_only=%d target_only=%d "
        "hash_mismatch=%d hash_match=%d (of %d common keys)",
        table_name, len(source_only_keys), len(target_only_keys),
        len(hash_mismatch_keys), len(common_keys) - len(hash_mismatch_keys), len(common_keys),
    )

    if is_pk_less:
        # No real key column exists in the original sourcequery/targetquery to
        # narrow a targeted re-fetch by — row_hash-keyed tables can only be
        # resolved by this engine when Tier 1 finds a clean match on every key.
        # If it doesn't, we refuse to guess rather than silently reimplement (or
        # get wrong) main.py's full-table row_hash comparison a second time.
        if not source_only_keys and not target_only_keys and not hash_mismatch_keys:
            filepath = os.path.join(output_path, f"{table_name}_{validation_name}_result_{run_id}.csv")
            pd.DataFrame([{"row_key": "ALL", "status": "PASS"}]).to_csv(filepath, index=False)
            return {"source_rows": source_rows, "target_rows": target_rows, "is_match": True}
        raise RuntimeError(
            f"hybrid_v1 found row-hash differences on PK-less table '{table_name}' "
            "but this table has no primary key column to narrow a targeted "
            "re-fetch by. Re-run without validation_plan.execution_strategy: "
            "hybrid_v1 for exact row-level PASS/FAIL/SOURCE_ONLY/TARGET_ONLY detail."
        )

    grain_failures = _validate_expected_grain_hybrid(validation_config, pk_col, src_hash, tgt_hash)
    for grain_failure in grain_failures:
        logger.warning("Quality check failed for %s: %s", table_name, grain_failure)

    source_query = validation_config["sourcequery"]
    target_query = validation_config["targetquery"]
    src_probe_cols = _probe_columns(src_db, source_query)
    tgt_probe_cols = _probe_columns(tgt_db, target_query)

    # F2 (Phase 2 audit): row_hash.columns is a first-class, documented,
    # OPTIONAL SUBSET (sql_query_generator.py's _row_hash_queries selects only
    # mapped columns in that list when it's non-empty). If it's narrower than
    # what compare_indexed_frames would actually compare, a HASH_MATCH would
    # never verify the uncovered columns -- real drift there would silently
    # become PASS forever. Refuse rather than trust incomplete coverage; an
    # empty row_hash_columns means "hash covers every mapped column", which is
    # always safe and needs no check.
    if row_hash_columns:
        _configured_hash_cols = {str(c).strip().lower() for c in row_hash_columns}
        _compared_cols = {c for c in src_probe_cols if c in set(tgt_probe_cols) and c != pk_col}
        _uncovered = _compared_cols - _configured_hash_cols
        if _uncovered:
            raise RuntimeError(
                f"hybrid_v1 refuses table='{table_name}': validation_plan.row_hash.columns="
                f"{sorted(_configured_hash_cols)} does not cover comparison column(s) "
                f"{sorted(_uncovered)} -- a HASH_MATCH would never verify those columns, which "
                "could silently pass real drift there. Configure row_hash.columns to include "
                "every compared column (or leave it empty to hash all of them), or remove "
                "execution_strategy: hybrid_v1 for this table."
            )

    # compare_indexed_frames sets the PK column(s) as the index, which removes
    # them from .columns internally — display_cols here must exclude the PK
    # column too, or the HASH_MATCH block below (which never calls
    # compare_indexed_frames) writes a wider row than every other batch.
    # Used for building the empty-side frame compare_indexed_frames() indexes
    # by pk_col — must still include the PK column itself.
    src_empty_cols = src_probe_cols
    tgt_empty_cols = tgt_probe_cols
    # Used only for the HASH_MATCH block below, which never calls
    # compare_indexed_frames() and so must exclude the PK column itself to
    # match the {col}__source/{col}__target shape compare_indexed_frames
    # produces (it sets the PK as the index, removing it from display_cols).
    src_display_cols = [c for c in src_probe_cols if c != pk_col]
    tgt_display_cols = [c for c in tgt_probe_cols if c != pk_col]
    display_cols = src_display_cols + [c for c in tgt_display_cols if c not in set(src_display_cols)]

    filepath = os.path.join(output_path, f"{table_name}_{validation_name}_result_{run_id}.csv")
    failed_filepath = os.path.join(output_path, f"{table_name}_{validation_name}_failed_{run_id}.csv")
    wrote_header = {"result": False, "failed": False}

    def _batches(keys):
        keys = sorted(keys, key=str)
        for i in range(0, len(keys), TIER2_BATCH_SIZE):
            yield keys[i:i + TIER2_BATCH_SIZE]

    n_fail_from_mismatch = 0

    for batch in _batches(source_only_keys):
        s_df = _fetch_batch(src_db, source_query, pk_col, batch)
        t_df = _empty_frame(tgt_empty_cols)
        s_df, t_df = canonicalize_frames(s_df, t_df)
        chunk_result = compare_indexed_frames(s_df, t_df, pk_col, pk_col, transformation_specs)
        _append_result_batch(chunk_result, filepath, failed_filepath, wrote_header)

    for batch in _batches(target_only_keys):
        s_df = _empty_frame(src_empty_cols)
        t_df = _fetch_batch(tgt_db, target_query, pk_col, batch)
        s_df, t_df = canonicalize_frames(s_df, t_df)
        chunk_result = compare_indexed_frames(s_df, t_df, pk_col, pk_col, transformation_specs)
        _append_result_batch(chunk_result, filepath, failed_filepath, wrote_header)

    for batch in _batches(hash_mismatch_keys):
        s_df = _fetch_batch(src_db, source_query, pk_col, batch)
        t_df = _fetch_batch(tgt_db, target_query, pk_col, batch)
        s_df, t_df = canonicalize_frames(s_df, t_df)
        chunk_result = compare_indexed_frames(s_df, t_df, pk_col, pk_col, transformation_specs)
        n_fail_from_mismatch += int((chunk_result["status"] == "FAIL").sum())
        _append_result_batch(chunk_result, filepath, failed_filepath, wrote_header)

    # HASH_MATCH keys — never re-fetched. Written as PASS with the same column
    # set as every other row (schema-consistent CSV) but blank cell values —
    # dumping full column values for every provisionally-identical row at
    # 200-300M-row scale would reintroduce the exact memory/I-O cost this
    # engine exists to remove. This is a deliberate, documented exception to
    # the "every row gets full column values" rule for FAIL/SOURCE_ONLY/
    # TARGET_ONLY rows, which keep it.
    #
    # F3 (Phase 2 audit): this is the largest bucket for any healthy
    # migration, so it goes through the same _batches() mechanism as every
    # other bucket above — never one unbounded match_rows list / match_df for
    # the whole set at once.
    hash_match_keys = common_keys - hash_mismatch_keys
    if hash_match_keys:
        blank_rec = {"row_key": None, "status": "PASS"}
        for col in display_cols:
            blank_rec[f"{col}__source"] = ""
            blank_rec[f"{col}__target"] = ""
        # Match compare_indexed_frames' _rec() column set exactly (same key
        # names, blank values) so a HASH_MATCH row never leaves the CSV with
        # fewer columns than a Tier-2 row when transformation_specs is set.
        if transformation_specs:
            blank_rec["validation_type"] = "TRANSFORMATION"
            for spec in transformation_specs:
                name = str(spec.get("name", ""))
                blank_rec[f"{name}__expected"] = ""
                blank_rec[f"{name}__actual"] = ""
                blank_rec[f"{name}__difference"] = ""
                blank_rec[f"{name}__status"] = ""
        for batch in _batches(hash_match_keys):
            match_rows = [dict(blank_rec, row_key=key_str) for key_str in batch]
            match_df = pd.DataFrame(match_rows)
            match_df.to_csv(filepath, mode="a", header=not wrote_header["result"], index=False)
            wrote_header["result"] = True

    if not wrote_header["result"]:
        # Nothing at all to compare (e.g. both sides empty) — still produce a
        # result file so downstream tooling never confuses "no failures" with
        # "validation didn't run."
        pd.DataFrame(columns=["row_key", "status"]).to_csv(filepath, index=False)

    total_keys = len(src_keys | tgt_keys)
    n_fail_rows = len(source_only_keys) + len(target_only_keys) + n_fail_from_mismatch
    threshold_pct = float(validation_config.get("mismatch_threshold_pct", 0))
    if threshold_pct > 0 and total_keys > 0:
        is_match = (n_fail_rows / total_keys * 100) <= threshold_pct
    else:
        is_match = (n_fail_rows == 0)

    if grain_failures:
        is_match = False

    return {
        "source_rows": source_rows,
        "target_rows": target_rows,
        "is_match": is_match,
        "grain_failures": grain_failures,
    }
