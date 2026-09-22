"""PK-indexed row comparison core.

Extracted verbatim from Project/main.py's inline comparison block so both the
default engine (main.py) and the hybrid Tier-1/Tier-2 engine (tiered_runner.py)
run the exact same algorithm — one implementation, not two copies that could
drift apart. No behavior change versus the code this replaced.

Callers are responsible for running canonicalize_frames() on both frames
first (main.py does this before indexing, same as before this extraction).
"""

import pandas as pd

from utils.utility import get_logger

logger = get_logger(__name__)


def _row_key_str(pk_val):
    if isinstance(pk_val, tuple):
        return "|".join(str(v) for v in pk_val)
    return str(pk_val)


def _cell_str(v):
    """Normalize a cell for comparison:
    - None/NaN            → '<<NULL>>'
    - float/Decimal       → 2-dp string (matches COALESCE CAST output)
    - everything else     → str()
    """
    if v is None or (isinstance(v, float) and v != v):
        return "<<NULL>>"
    if isinstance(v, float):
        return f"{v:.2f}"
    try:
        from decimal import Decimal as _Dec
        if isinstance(v, _Dec):
            return f"{float(v):.2f}"
    except Exception:
        pass
    return str(v)


def _to_df(frame, pk_val):
    if pk_val not in frame.index:
        return pd.DataFrame(columns=frame.columns)
    chunk = frame.loc[pk_val]
    return chunk if isinstance(chunk, pd.DataFrame) else chunk.to_frame().T


def compare_indexed_frames(source_df, target_df, pk_src, pk_tgt, transformation_specs=None):
    """Compare two already-canonicalized frames by PK (scalar or composite/list).

    Returns a result DataFrame with one row per unique PK: row_key, status
    (PASS/FAIL/SOURCE_ONLY/TARGET_ONLY), {col}__source/{col}__target for every
    display column, and optional {name}__expected/__actual/__difference/__status
    per configured transformation spec. Sorted by row_key.

    Duplicate PKs are compared as sorted multisets of rendered rows (row order
    within a duplicate-PK group never causes a false FAIL; a real duplicate-count
    mismatch still does, by list-length inequality after sort).
    """
    transformation_specs = transformation_specs or []

    composite = isinstance(pk_src, list)
    src = source_df.set_index(pk_src).sort_index()
    tgt = target_df.set_index(pk_tgt).sort_index()
    if composite:
        tgt.index.names = src.index.names
    else:
        tgt.index.name = src.index.name

    all_src_cols = list(src.columns)
    all_tgt_cols = list(tgt.columns)
    tgt_col_set = set(all_tgt_cols)
    src_col_set = set(all_src_cols)

    # Columns present in both sides — comparison happens only here.
    common_cols = [c for c in all_src_cols if c in tgt_col_set]
    # Schema drift — reported as warnings, not row-level FAILs.
    src_only_cols = [c for c in all_src_cols if c not in tgt_col_set]
    tgt_only_cols = [c for c in all_tgt_cols if c not in src_col_set]
    if src_only_cols:
        logger.warning(
            "Schema drift: column(s) %s exist in SOURCE but not in TARGET — "
            "excluded from row comparison; check target schema.",
            src_only_cols,
        )
    if tgt_only_cols:
        logger.warning(
            "Schema drift: column(s) %s exist in TARGET but not in SOURCE — "
            "excluded from row comparison.",
            tgt_only_cols,
        )

    # All columns from both sides appear in the output CSV for traceability.
    display_cols = all_src_cols + [c for c in all_tgt_cols if c not in src_col_set]

    def _rec(pk_str, status, s_row=None, t_row=None):
        rec = {"row_key": pk_str, "status": status}
        for col in display_cols:
            rec[f"{col}__source"] = (s_row[col] if s_row is not None and col in s_row.index else "")
            rec[f"{col}__target"] = (t_row[col] if t_row is not None and col in t_row.index else "")
        if transformation_specs:
            rec["validation_type"] = "TRANSFORMATION"
            for spec in transformation_specs:
                name = str(spec.get("name", ""))
                value_column = f"{name}_value"
                expected = s_row.get(value_column, "") if s_row is not None else ""
                actual = t_row.get(value_column, "") if t_row is not None else ""
                rec[f"{name}__expected"] = expected
                rec[f"{name}__actual"] = actual
                try:
                    rec[f"{name}__difference"] = float(actual) - float(expected)
                except (TypeError, ValueError):
                    rec[f"{name}__difference"] = "" if actual == expected else "MISMATCH"
                rec[f"{name}__status"] = "PASS" if expected == actual else "FAIL"
        return rec

    # Single loop over all unique PKs — handles duplicates as sorted multisets
    # so row-order differences between engines never cause false FAILs.
    # Comparison is on common_cols only; schema drift is logged above.
    # ponytail: O(n log n) sort per PK group; fine for migration data volumes.
    all_pks = sorted(
        set(src.index.unique()) | set(tgt.index.unique()),
        key=lambda x: str(x),
    )
    rows = []
    for pk_val in all_pks:
        s_df = _to_df(src, pk_val)
        t_df = _to_df(tgt, pk_val)
        pk_str = _row_key_str(pk_val)

        if s_df.empty:
            rows.append(_rec(pk_str, "TARGET_ONLY", t_row=t_df.iloc[0]))
        elif t_df.empty:
            rows.append(_rec(pk_str, "SOURCE_ONLY", s_row=s_df.iloc[0]))
        else:
            s_sorted = sorted(
                s_df[common_cols].apply(
                    lambda r: "|".join(_cell_str(r[c]) for c in common_cols), axis=1
                ).tolist()
            )
            t_sorted = sorted(
                t_df[common_cols].apply(
                    lambda r: "|".join(_cell_str(r[c]) for c in common_cols), axis=1
                ).tolist()
            )
            row_status = "PASS" if s_sorted == t_sorted else "FAIL"
            rows.append(_rec(pk_str, row_status, s_row=s_df.iloc[0], t_row=t_df.iloc[0]))

    return pd.DataFrame(rows).sort_values("row_key").reset_index(drop=True)
