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
from utils.quality_checks import _as_float, _hash_dataframe, _pct_difference
from utils.row_compare import compare_indexed_frames
from utils.semantic_normalize import NULL_PLACEHOLDER, canonicalize_frames, looks_numeric_string, looks_semi_structured
from utils.utility import get_logger

logger = get_logger(__name__)

# ponytail: in-memory dict, not an external sort-merge — (key,hash) pairs are
# the smallest structure in this whole pipeline; swap for an external merge
# only if a real table's key cardinality makes this too big (unmeasured today,
# see docs/large-table-scalable-architecture §N/§P).
TIER1_FETCH_CHUNK = 50_000
TIER2_BATCH_SIZE = 2_000

# run_quality_checks parity (docs/large-table-scalable-architecture §R/§S):
# bounded sample used only to CLASSIFY columns (numeric? canonicalization-risk?)
# before pushing the actual aggregate down to SQL -- never to compute the
# aggregate value itself. Mirrors semantic_normalize's own 200-row sniff
# sample, wider because a false "not numeric" here silently drops that
# column's sum/min/max check for the whole table.
QUALITY_SAMPLE_ROWS = 1_000

# sample_hash absolute cap (§R.2.4.6 design gap, resolved here): the oracle's
# sample_size is sample_hash_percent% of the fetched row count with NO cap --
# at 300M rows even 1% is 3M full-width rows, which defeats hybrid_v1's whole
# purpose. hybrid_v1 takes min(percentage-implied size, this cap).
SAMPLE_HASH_ABSOLUTE_CAP = 10_000


def _strip_trailing_semicolon(sql):
    return sql.rstrip().rstrip(";")


def _quote_ident(dialect, name):
    """Quote a column name for use in an outer query wrapped around an
    existing sourcequery/targetquery. Every base-rule-generated Snowflake
    SELECT already emits its "*_normalized" aliases double-quoted lowercase
    (AS "id_normalized") specifically so Snowflake's default unquoted-
    identifier uppercase-folding doesn't apply to them -- an outer reference
    that skips the same quoting resolves to the uppercase-folded name
    (ID_NORMALIZED) instead, which doesn't exist. Quoting our own
    already-lowercased column name here reproduces exactly the quoting the
    inner query used, on every dialect: Postgres/Redshift/Athena/Trino also
    fold unquoted aliases to lowercase, so quoting the lowercase name matches
    the same projected identifier there too. MSSQL uses bracket quoting
    instead of double quotes."""
    name = str(name)
    if (dialect or "").lower() == "mssql":
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


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


# ── run_quality_checks parity (docs/large-table-scalable-architecture §R) ───
#
# quality_checks.run_quality_checks needs a fully-materialized frame per side
# (pandas .isna()/.nunique()/.sum()/etc). This engine never has one -- Tier 1
# only ever holds (key, hash) pairs. The functions below push each check down
# to a bounded SQL aggregate over the *existing, unmodified* sourcequery/
# targetquery text (the same subquery-wrapping trick _probe_columns/
# _fetch_batch already use), so every Fivetran/exclusion/filter clause baked
# into that query text is reused exactly -- no independent filter logic here.
#
# Two checks (null_rate, distinct_count) have a real, documented semantic gap
# from the Python oracle for specific column classes -- see each helper's
# docstring and docs/large-table-scalable-architecture §S. This is not hidden:
# canonicalization-risk columns are excluded from the SQL distinct_count
# rather than silently mismeasured, and every gap is logged.


def _limit_query(dialect, query, n):
    """Bounded, unordered row sample of an existing query -- same lack of
    ORDER BY as the oracle's sample_hash (main.py never sorts before that
    check either), just capped and dialect-aware (MSSQL has no LIMIT)."""
    q = _strip_trailing_semicolon(query)
    if (dialect or "").lower() == "mssql":
        return f"SELECT TOP {n} * FROM ({q}) AS t"
    return f"SELECT * FROM ({q}) AS t LIMIT {n}"


def _fetch_sample(db_obj, dialect, query, n):
    df = db_obj.execute_query(_limit_query(dialect, query, n))
    df.columns = df.columns.str.strip().str.lower()
    return df


def _dialect_text_cast(dialect, col_expr):
    dialect = (dialect or "postgresql").lower()
    if dialect == "mssql":
        return f"CAST({col_expr} AS VARCHAR(MAX))"
    if dialect in {"postgresql", "redshift"}:
        return f"CAST({col_expr} AS TEXT)"
    return f"CAST({col_expr} AS VARCHAR)"  # snowflake, athena/trino/presto


def _dialect_numeric_cast(dialect, col_expr, round2=False):
    """A cast that returns NULL instead of erroring on non-numeric text --
    needed because every base-rule-generated comparison column already
    arrives as TEXT (COALESCE(CAST(... AS TEXT/STRING), '<<NULL>>'), see
    base_rules.py), so SUM/MIN/MAX need this to reach numeric values at all,
    and must not blow up the whole aggregate query on one noisy row.

    round2=True wraps the result in ROUND(..., 2) -- oracle parity for
    columns classified as plain-decimal-string ("numeric-string") by
    looks_numeric_string(): canonicalize_frames' _normalize_numeric_str
    rounds those same columns to 2dp *before* the oracle's pandas
    SUM/MIN/MAX ever run (Project/main.py calls canonicalize_frames() before
    run_quality_checks()), so an unrounded SQL SUM here would silently
    diverge from the oracle's rounded-then-summed result (live divergence:
    raw 0.1234 vs oracle's canonicalized-then-summed 0.12). Rounding a
    non-decimal-string numeric value (e.g. a plain integer) to 2dp is a
    no-op, so applying ROUND to the whole column is equivalent to the
    oracle's per-cell selective rounding."""
    dialect = (dialect or "postgresql").lower()
    if dialect == "mssql":
        cast = f"TRY_CAST({col_expr} AS FLOAT)"
    elif dialect in {"snowflake", "athena", "trino", "presto"}:
        cast = f"TRY_CAST({col_expr} AS DOUBLE)"
    else:
        # postgresql / redshift have no TRY_CAST across supported versions --
        # guard with a numeric-literal regex before casting, same spirit as
        # the oracle's own pd.to_numeric(errors="coerce").
        text_cast = _dialect_text_cast(dialect, col_expr)
        cast = (
            f"CASE WHEN {text_cast} ~ '^\\s*-?[0-9]+(\\.[0-9]+)?\\s*$' "
            f"THEN {text_cast}::double precision ELSE NULL END"
        )
    return f"ROUND({cast}, 2)" if round2 else cast


def _numeric_sample_columns(sample_df, columns):
    """Columns where pd.to_numeric coerces at least one non-null value --
    the exact test run_quality_checks uses (quality_checks.py:76), applied to
    a bounded sample instead of the full column. Documented difference from
    the oracle: the oracle scans every row; this scans up to
    QUALITY_SAMPLE_ROWS. A column that's all-noise in the sample but genuinely
    numeric later in the table would be missed here and skip its sum/min/max
    check -- acceptable at 200-300M-row scale, not silently equivalent."""
    numeric_cols = []
    for col in columns:
        if col not in sample_df.columns:
            continue
        if pd.to_numeric(sample_df[col], errors="coerce").notna().any():
            numeric_cols.append(col)
    return numeric_cols


def _canonicalization_risk_columns(sample_df, columns):
    """Columns where SQL COUNT(DISTINCT) cannot be trusted to match the
    Python oracle's canonicalized nunique() -- semi-structured (JSON/HStore)
    content or numeric-string precision variance ('400000.00' vs
    '400000.000000'). Both are the same fundamental gap the master design doc's
    §F already proved unfixable in SQL (canonicalization is Python-only by
    design) -- see §R.2.2. These columns are excluded from the SQL
    distinct_count rather than given a wrong answer."""
    risky = set()
    for col in columns:
        if col not in sample_df.columns:
            continue
        for cell in sample_df[col].dropna():
            if looks_semi_structured(cell) or looks_numeric_string(cell):
                risky.add(col)
                break
    return risky


def _numeric_string_round_columns(sample_df, columns):
    """Columns whose sampled values look like plain-decimal strings
    (e.g. '400000.000000') -- the same shape canonicalize_frames'
    _normalize_numeric_str rounds to 2dp before the oracle's SUM/MIN/MAX
    ever run. Unlike _canonicalization_risk_columns (which *excludes* such
    columns from distinct_count because SQL can't cheaply reproduce
    canonicalize_value()'s JSON-key-reordering semantics), aggregate parity
    for this specific shape *is* cheaply reproducible in SQL -- see
    _dialect_numeric_cast's round2 parameter -- so these columns are rounded
    in the aggregate SQL instead of excluded."""
    matched = set()
    for col in columns:
        if col not in sample_df.columns:
            continue
        for cell in sample_df[col].dropna():
            if looks_numeric_string(cell):
                matched.add(col)
                break
    return matched


def _quality_aggregate_sql(query, columns, distinct_columns, numeric_columns, dialect, round2_columns=frozenset()):
    # Every column reference below is a name projected by the wrapped
    # sourcequery/targetquery, not a literal of this outer query -- must be
    # quoted the same way the inner query's own aliases are (see
    # _quote_ident's docstring), or Snowflake's uppercase identifier folding
    # makes every one of these references resolve to a column that doesn't
    # exist.
    exprs = ["COUNT(*) AS __total_rows"]
    for col in columns:
        qcol = _quote_ident(dialect, col)
        text_cast = _dialect_text_cast(dialect, qcol)
        exprs.append(f"COUNT({qcol}) AS {col}__notnull")
        # A column is "nullish" if it's real SQL NULL (non-COALESCE'd YAML
        # paths, e.g. webapp "Simple query" mode) OR the literal sentinel
        # string base_rules emits for a NULL under the COALESCE-wrapped
        # default path (§R.1) -- checking both covers whichever path produced
        # this table's YAML without needing to know which one it was.
        exprs.append(
            f"SUM(CASE WHEN {qcol} IS NULL OR {text_cast} = '{NULL_PLACEHOLDER}' "
            f"THEN 1 ELSE 0 END) AS {col}__nullish"
        )
    for col in distinct_columns:
        exprs.append(f"COUNT(DISTINCT {_quote_ident(dialect, col)}) AS {col}__distinct")
    for col in numeric_columns:
        cast = _dialect_numeric_cast(dialect, _quote_ident(dialect, col), round2=col in round2_columns)
        exprs.append(f"SUM({cast}) AS {col}__sum")
        exprs.append(f"MIN({cast}) AS {col}__min")
        exprs.append(f"MAX({cast}) AS {col}__max")
    return f"SELECT {', '.join(exprs)} FROM ({_strip_trailing_semicolon(query)}) AS t"


def _run_quality_aggregate(db_obj, dialect, query, columns, distinct_columns, numeric_columns, round2_columns=frozenset()):
    if not columns:
        return {"__total_rows": 0}
    sql = _quality_aggregate_sql(query, columns, distinct_columns, numeric_columns, dialect, round2_columns)
    df = db_obj.execute_query(sql)
    df.columns = df.columns.str.strip().str.lower()
    return df.iloc[0].to_dict() if len(df) else {"__total_rows": 0}


def _null_rate_failures(src_stats, tgt_stats, columns, tolerance):
    failures = []
    src_total = float(src_stats.get("__total_rows") or 0)
    tgt_total = float(tgt_stats.get("__total_rows") or 0)
    for col in columns:
        src_rate = (float(src_stats.get(f"{col}__nullish") or 0) / src_total * 100) if src_total else 0.0
        tgt_rate = (float(tgt_stats.get(f"{col}__nullish") or 0) / tgt_total * 100) if tgt_total else 0.0
        if abs(src_rate - tgt_rate) > tolerance:
            failures.append({
                "check": "null_rate",
                "column": col,
                "source": round(src_rate, 6),
                "target": round(tgt_rate, 6),
                "tolerance": tolerance,
            })
    return failures


def _distinct_count_failures(src_stats, tgt_stats, distinct_columns, tolerance):
    """+1 correction when the column has at least one real SQL NULL --
    COUNT(DISTINCT col) excludes NULL, but the oracle's nunique(dropna=False)
    counts it as one more category (quality_checks.py:80, §R.2.2.3). No
    correction is needed for the COALESCE-sentinel case: that arrives as an
    ordinary string, which COUNT(DISTINCT) already counts like any other
    value, same as nunique() would."""
    failures = []
    src_total = float(src_stats.get("__total_rows") or 0)
    tgt_total = float(tgt_stats.get("__total_rows") or 0)
    for col in distinct_columns:
        src_notnull = float(src_stats.get(f"{col}__notnull") or 0)
        tgt_notnull = float(tgt_stats.get(f"{col}__notnull") or 0)
        src_distinct = int(src_stats.get(f"{col}__distinct") or 0) + (1 if src_total > src_notnull else 0)
        tgt_distinct = int(tgt_stats.get(f"{col}__distinct") or 0) + (1 if tgt_total > tgt_notnull else 0)
        if abs(src_distinct - tgt_distinct) > tolerance:
            failures.append({
                "check": "distinct_count",
                "column": col,
                "source": src_distinct,
                "target": tgt_distinct,
                "tolerance": tolerance,
            })
    return failures


def _aggregate_value_failures(src_stats, tgt_stats, numeric_columns, tolerance):
    failures = []
    for col in numeric_columns:
        for name in ("sum", "min", "max"):
            source_value = src_stats.get(f"{col}__{name}")
            target_value = tgt_stats.get(f"{col}__{name}")
            if source_value is None or target_value is None:
                continue  # matches oracle: nothing numeric on one side -> skip, not a mismatch
            source_value, target_value = float(source_value), float(target_value)
            difference_pct = _pct_difference(source_value, target_value)
            if difference_pct > tolerance:
                failures.append({
                    "check": name,
                    "column": col,
                    "source": source_value,
                    "target": target_value,
                    "difference_pct": round(difference_pct, 6),
                    "tolerance": tolerance,
                })
    return failures


def _sample_hash_failures(src_db, tgt_db, source_dialect, target_dialect, source_query, target_query,
                           columns, sample_percent, src_total_rows, tgt_total_rows):
    """Bounded LIMIT-fetch-and-hash, not an aggregate -- unlike the other three
    checks this needs actual row content. Capped at SAMPLE_HASH_ABSOLUTE_CAP
    regardless of what sample_percent implies (§R.2.4.6). Inherits the
    oracle's own ordering fragility: two independently-executed, unordered
    queries against two different engines aren't guaranteed to return "the
    same" logical rows first -- not fixed here, same as the oracle."""
    if sample_percent <= 0 or not src_total_rows or not tgt_total_rows or not columns:
        return []
    pct_size = max(1, int(min(src_total_rows, tgt_total_rows) * min(sample_percent, 100) / 100))
    sample_size = min(pct_size, SAMPLE_HASH_ABSOLUTE_CAP)
    src_sample = _fetch_sample(src_db, source_dialect, source_query, sample_size)
    tgt_sample = _fetch_sample(tgt_db, target_dialect, target_query, sample_size)
    cols = [c for c in columns if c in src_sample.columns and c in tgt_sample.columns]
    if not cols:
        return []
    # Oracle parity (Project/main.py calls canonicalize_frames() before
    # run_quality_checks(), which is what hashes sample_hash's rows) --
    # hashing the raw fetched sample here instead would flag JSON key
    # reordering, HStore formatting, and numeric-string precision as
    # mismatches that canonicalize_frames treats as equal everywhere else in
    # this same validation run.
    src_sample, tgt_sample = canonicalize_frames(src_sample, tgt_sample)
    src_hash = _hash_dataframe(src_sample, cols)
    tgt_hash = _hash_dataframe(tgt_sample, cols)
    if src_hash == tgt_hash:
        return []
    return [{
        "check": "sample_hash",
        "sample_percent": sample_percent,
        "sample_rows": sample_size,
        "source_hash": src_hash,
        "target_hash": tgt_hash,
    }]


def _run_quality_checks_hybrid(table_name, validation_config, source_dialect, target_dialect,
                                src_db, tgt_db, source_query, target_query, columns,
                                src_total_rows, tgt_total_rows):
    """hybrid_v1 parity for quality_checks.run_quality_checks -- see module
    docstring above and docs/large-table-scalable-architecture §R/§S. Returns
    the same failure-dict shape the oracle does, so main.py's
    `quality_failures + grain_failures` handling needs no branch on which
    engine produced them."""
    checks = validation_config.get("quality_checks") or {}
    if not isinstance(checks, dict) or not checks or checks.get("enabled") is False:
        return []

    null_tolerance = _as_float(checks.get("null_rate_tolerance_pct"))
    distinct_tolerance = _as_float(checks.get("distinct_count_tolerance"))
    aggregate_tolerance = _as_float(checks.get("aggregate_tolerance_pct"))
    sample_percent = _as_float(checks.get("sample_hash_percent"))

    # Same "0.0-tolerance-means-active" trap as the oracle (quality_checks.py's
    # _as_float(None) == 0.0 >= 0) -- preserved intentionally, not an oversight
    # (§R.0): a table with only `quality_checks: {enabled: true}` runs
    # null_rate/distinct_count/aggregate at exact-match tolerance here too.
    null_active = null_tolerance >= 0
    distinct_active = distinct_tolerance >= 0
    aggregate_active = aggregate_tolerance >= 0

    failures = []
    if columns and (null_active or distinct_active or aggregate_active):
        distinct_columns, numeric_columns, round2_columns = [], [], set()
        if distinct_active or aggregate_active:
            src_sample = _fetch_sample(src_db, source_dialect, source_query, QUALITY_SAMPLE_ROWS)
            tgt_sample = _fetch_sample(tgt_db, target_dialect, target_query, QUALITY_SAMPLE_ROWS)

            if distinct_active:
                risky = _canonicalization_risk_columns(src_sample, columns) | _canonicalization_risk_columns(tgt_sample, columns)
                distinct_columns = [c for c in columns if c not in risky]
                skipped = sorted(set(columns) - set(distinct_columns))
                if skipped:
                    logger.info(
                        "hybrid_v1 distinct_count check skipped for table=%s canonicalization-risk "
                        "column(s) %s -- JSON/HStore/decimal-precision content can't reach exact SQL "
                        "parity with the Python oracle (docs/large-table-scalable-architecture §R.2.2)",
                        table_name, skipped,
                    )

            if aggregate_active:
                src_numeric = set(_numeric_sample_columns(src_sample, columns))
                tgt_numeric = set(_numeric_sample_columns(tgt_sample, columns))
                numeric_columns = sorted(src_numeric & tgt_numeric)
                # Oracle parity (§R.2.2 gap closed): canonicalize_frames rounds
                # plain-decimal-string columns to 2dp *before* the oracle's
                # SUM/MIN/MAX -- round these same columns in the SQL aggregate
                # or the two engines silently sum different precision.
                round2_columns = (
                    _numeric_string_round_columns(src_sample, numeric_columns)
                    | _numeric_string_round_columns(tgt_sample, numeric_columns)
                )

        src_stats = _run_quality_aggregate(src_db, source_dialect, source_query, columns, distinct_columns, numeric_columns, round2_columns)
        tgt_stats = _run_quality_aggregate(tgt_db, target_dialect, target_query, columns, distinct_columns, numeric_columns, round2_columns)

        if null_active:
            failures += _null_rate_failures(src_stats, tgt_stats, columns, null_tolerance)
        if distinct_active:
            failures += _distinct_count_failures(src_stats, tgt_stats, distinct_columns, distinct_tolerance)
        if aggregate_active:
            failures += _aggregate_value_failures(src_stats, tgt_stats, numeric_columns, aggregate_tolerance)

    failures += _sample_hash_failures(
        src_db, tgt_db, source_dialect, target_dialect, source_query, target_query,
        columns, sample_percent, src_total_rows, tgt_total_rows,
    )
    return failures


def _fetch_batch(db_obj, dialect, query, pk_col, key_values):
    if not key_values:
        return None
    in_list = ", ".join(_sql_literal(v) for v in key_values)
    # pk_col is a name projected by the wrapped query's own aliasing, not a
    # literal of this outer WHERE -- must be quoted the same way (see
    # _quote_ident's docstring) or Snowflake resolves it to the
    # uppercase-folded name instead of the quoted-lowercase one the inner
    # query actually produced.
    wrapped = (
        f"SELECT * FROM ({_strip_trailing_semicolon(query)}) AS t "
        f"WHERE {_quote_ident(dialect, pk_col)} IN ({in_list})"
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

    # run_quality_checks parity (§R above) — same column set the oracle's
    # _column_pairs uses (source ∩ target columns, PK included, nothing
    # excluded) computed from the *main* sourcequery/targetquery, never the
    # narrower row_hash.columns subset (quality checks are independent of
    # what Tier 1 hashes).
    quality_columns = [c for c in src_probe_cols if c in set(tgt_probe_cols)]
    quality_failures = _run_quality_checks_hybrid(
        table_name, validation_config, source, target, src_db, tgt_db,
        source_query, target_query, quality_columns, source_rows, target_rows,
    )
    for quality_failure in quality_failures:
        logger.warning("Quality check failed for %s: %s", table_name, quality_failure)

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
        s_df = _fetch_batch(src_db, source, source_query, pk_col, batch)
        t_df = _empty_frame(tgt_empty_cols)
        s_df, t_df = canonicalize_frames(s_df, t_df)
        chunk_result = compare_indexed_frames(s_df, t_df, pk_col, pk_col, transformation_specs)
        _append_result_batch(chunk_result, filepath, failed_filepath, wrote_header)

    for batch in _batches(target_only_keys):
        s_df = _empty_frame(src_empty_cols)
        t_df = _fetch_batch(tgt_db, target, target_query, pk_col, batch)
        s_df, t_df = canonicalize_frames(s_df, t_df)
        chunk_result = compare_indexed_frames(s_df, t_df, pk_col, pk_col, transformation_specs)
        _append_result_batch(chunk_result, filepath, failed_filepath, wrote_header)

    for batch in _batches(hash_mismatch_keys):
        s_df = _fetch_batch(src_db, source, source_query, pk_col, batch)
        t_df = _fetch_batch(tgt_db, target, target_query, pk_col, batch)
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

    if grain_failures or quality_failures:
        is_match = False

    return {
        "source_rows": source_rows,
        "target_rows": target_rows,
        "is_match": is_match,
        "grain_failures": grain_failures,
        "quality_failures": quality_failures,
    }
