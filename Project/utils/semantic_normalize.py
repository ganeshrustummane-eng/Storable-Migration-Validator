"""
Semi-structured value canonicalization (JSON / JSONB / HStore -> VARIANT)
========================================================================
Postgres json/jsonb/hstore columns land in Snowflake as VARIANT. The *semantics*
survive the migration but the *serialization* does not: key order, whitespace,
number formatting and the representation of nested documents all differ.

Earlier versions of this tool tried to produce a byte-identical canonical string
inside SQL on both sides (a recursive CTE + string_agg on Postgres, LATERAL
FLATTEN + LISTAGG on Snowflake). Two different engines cannot be relied on to
agree, and they did not:

  * Snowflake LISTAGG returns '' (not NULL) when every input is NULL, so the
    NULL/empty sentinel fallback was dead code.
  * Postgres ORDER BY uses the database collation (en_US.UTF-8 ignores '_' and
    case at the primary level); Snowflake orders by codepoint. Mixed-case keys
    sort as 'apple, Zebra' on one side and 'Zebra, apple' on the other.
  * jsonb_each() hard-errors on a top-level array or scalar, so a single such
    row aborted the entire source query.
  * A value that is a *string containing JSON* was compared byte-for-byte, so
    any re-serialization by the loader read as a mismatch.

So SQL now emits the raw value as text on both sides and canonicalization
happens here, once, in Python. Symmetry is structural rather than something we
hope two engines agree on.

Canonicalization rules applied to every semi-structured value:
  * Object keys  — sorted by Unicode codepoint (Python's native str sort).
  * Array elements — sorted by their serialized string form before comparison.
                     A reordered array is treated as equivalent data, not drift
                     (e.g. Snowflake VARIANT arrays don't guarantee element
                     order survives a round trip the way Postgres text[] does).
  * Booleans     — serialized as "True" / "False" (Python-style capitalization)
                   so that the JSON lowercase true/false and any loader variant
                   all produce the same comparison token.
  * Numbers      — rounded to 2 decimal places before serialization.
                   470.850 and 470.85 are treated as equivalent.
  * Strings      — returned as-is (whitespace inside values is preserved).
  * JSON null    — serialized as the literal token "null".
  * SQL NULL     — becomes '<<NULL>>' via the COALESCE wrapper in SQL; never
                   reaches this module.

Applied from Project/main.py after the source/target frames are fetched and
before they are compared.
"""

import json
from decimal import Decimal, ROUND_HALF_UP

# Number of decimal places all numeric values are rounded to before comparison.
_DECIMAL_PLACES = Decimal("0.01")  # i.e. 2 d.p.

# Emitted by the SQL COALESCE wrapper for SQL NULL; must pass through untouched.
NULL_PLACEHOLDER = "<<NULL>>"

# Cells are only parsed when they look semi-structured. This fast reject is what
# keeps the per-cell cost acceptable on wide tables: a plain text/numeric/date
# column never reaches the JSON parser at all.
_JSON_PREFIXES = ("{", "[", '"')
_HSTORE_SEPARATOR = "=>"

# Number of non-null cells inspected per column when deciding whether a column
# holds semi-structured data.
_SNIFF_SAMPLE = 200


def looks_semi_structured(text):
    """True when text could be a JSON document or a native hstore literal.

    Deliberately cheap and permissive — a false positive costs one failed parse
    (the value is then returned unchanged), a false negative would leave one
    side of the comparison un-canonicalized.
    """
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    return s.startswith(_JSON_PREFIXES) or _HSTORE_SEPARATOR in s


# ── Native hstore parsing ────────────────────────────────────────────────────

def parse_hstore_text(text):
    """Parse a native Postgres hstore literal into a dict, or None on failure.

    Format:  "key"=>"value", "key2"=>NULL, barekey=>"v"

    This is a character scanner rather than a regex on purpose. The obvious
    regex — r'"([^"]+)"\\s*=>\\s*"([^"]*)"' — cannot parse real hstore data,
    because [^"]* stops at the first escaped quote. A value such as

        "payables"=>"[{\\"ledger_id\\":6752258,\\"amount\\":470.85}]"

    would be silently truncated to '[{\\' and the mismatch would look like real
    data drift. Values holding embedded JSON are exactly the case this module
    exists to handle, so the escape handling has to be correct.

    Only needed as a fallback for targets migrated in native hstore format —
    the Postgres side emits hstore_to_json(col)::text, which is real JSON.
    """
    if not isinstance(text, str):
        return None

    s = text.strip()
    if _HSTORE_SEPARATOR not in s:
        return None

    result = {}
    i = 0
    n = len(s)

    while i < n:
        # Skip whitespace and pair separators.
        while i < n and (s[i].isspace() or s[i] == ","):
            i += 1
        if i >= n:
            break

        key, i = _scan_hstore_token(s, i)
        if key is None:
            return None

        # Expect '=>'
        while i < n and s[i].isspace():
            i += 1
        if not s.startswith(_HSTORE_SEPARATOR, i):
            return None
        i += len(_HSTORE_SEPARATOR)
        while i < n and s[i].isspace():
            i += 1

        # A bare (unquoted) NULL is hstore's SQL NULL and scans to None.
        value, i = _scan_hstore_token(s, i)
        result[str(key)] = value

    return result or None


def _scan_hstore_token(s, i):
    """Scan one hstore key or value starting at i.

    Returns (token, next_index). token is a str for quoted/bare text, None for
    a bare NULL, or (None, i) signalling a parse failure — distinguished by the
    caller only for keys, where None is never valid.
    """
    n = len(s)
    if i >= n:
        return None, i

    if s[i] == '"':
        i += 1
        chars = []
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n:
                # Preserve the escaped character itself, not the backslash.
                chars.append(s[i + 1])
                i += 2
                continue
            if c == '"':
                return "".join(chars), i + 1
            chars.append(c)
            i += 1
        # Unterminated quote.
        return None, i

    # Bare token: runs until whitespace, ',' or the start of '=>'.
    start = i
    while i < n and not s[i].isspace() and s[i] != "," and not s.startswith(_HSTORE_SEPARATOR, i):
        i += 1
    raw = s[start:i]
    if not raw:
        return None, i
    if raw.upper() == "NULL":
        return None, i
    return raw, i


# ── Canonicalization ─────────────────────────────────────────────────────────

def _parse_document(s):
    """Parse s as JSON or native hstore. Returns (parsed, ok)."""
    try:
        # parse_float=Decimal, not float: float() would silently round a
        # high-precision numeric, which could mask genuine data drift.
        return json.loads(s, parse_float=Decimal), True
    except (ValueError, TypeError):
        pass
    if _HSTORE_SEPARATOR in s:
        parsed = parse_hstore_text(s)
        if parsed is not None:
            return parsed, True
    return None, False


def _round_number(node):
    """Round any numeric value to _DECIMAL_PLACES (2 d.p.), returned as Decimal."""
    try:
        return Decimal(str(node)).quantize(_DECIMAL_PLACES, rounding=ROUND_HALF_UP)
    except Exception:
        return node


def _canonicalize_node(node):
    """Recursively canonicalize a parsed document (returns new value)."""
    if isinstance(node, dict):
        # Keys sorted at serialization time; values recursed.
        return {str(k): _canonicalize_node(v) for k, v in node.items()}

    if isinstance(node, list):
        # Array order is not treated as data — sort by serialized form so a
        # reordered array (e.g. Fivetran/VARIANT round trip) isn't reported
        # as drift. Nested values are canonicalized before sorting so nested
        # dict key order doesn't affect the sort key.
        canonicalized = [_canonicalize_node(v) for v in node]
        return sorted(canonicalized, key=_serialize)

    if isinstance(node, str):
        # Normalize boolean-like strings so that the JSON literal true and the
        # loader string "true" (or "True", "TRUE") produce the same token.
        # This is the only value coercion applied to strings; numeric-looking
        # strings like "470.85" are intentionally left as strings.
        _low = node.strip().lower()
        if _low == "true":
            return True
        if _low == "false":
            return False
        # Recurse into strings that hold a JSON/hstore document.
        if looks_semi_structured(node):
            parsed, ok = _parse_document(node.strip())
            if ok:
                return _canonicalize_node(parsed)
        return node

    # bool before numeric: bool is a subclass of int in Python.
    if isinstance(node, bool):
        return node  # _serialize renders as "True" / "False"

    # Only fractional values need rounding; a bare JSON integer (parsed as
    # Python int, since parse_float=Decimal doesn't affect ints) has no
    # trailing-zero ambiguity and is left as-is.
    if isinstance(node, (Decimal, float)):
        return _round_number(node)

    if isinstance(node, int):
        return node

    return node


def _serialize(node):
    """Render a canonicalized document to a compact, key-sorted string.

    All values are converted to a common text form so the final comparison is
    always a plain string equality check:
      - dict keys  : sorted by codepoint (Python's str sort)
      - numbers    : Decimal with 2 d.p., rendered without exponent ("f" format)
      - booleans   : "True" / "False"  (Python-style, matches str(True))
      - null / None: "null"
      - strings    : JSON-quoted (preserves escaping and distinguishes 1 vs "1")
    """
    if isinstance(node, dict):
        return "{" + ",".join(
            json.dumps(k, ensure_ascii=False) + ":" + _serialize(v)
            for k, v in sorted(node.items())
        ) + "}"

    if isinstance(node, list):
        return "[" + ",".join(_serialize(v) for v in node) + "]"

    # bool before int: bool is a subclass of int in Python.
    if isinstance(node, bool):
        return "True" if node else "False"

    if node is None:
        return "null"

    if isinstance(node, Decimal):
        # 'f' expands exponent notation while preserving decimal precision.
        return format(node, "f")

    if isinstance(node, int):
        return str(node)

    if isinstance(node, float):
        return format(node, "f")

    return json.dumps(node, ensure_ascii=False)


def canonicalize_value(value):
    """Return a canonical, comparable string for one cell.

    Non-semi-structured values (and anything that fails to parse) are returned
    unchanged, so this is safe to apply to a whole column.
    """
    if value is None:
        return value
    if not isinstance(value, str):
        return value
    if value == NULL_PLACEHOLDER:
        return value

    s = value.strip()
    if not looks_semi_structured(s):
        return value

    parsed, ok = _parse_document(s)
    if not ok:
        return value

    canonical = _canonicalize_node(parsed)

    # A top-level scalar is returned bare (not JSON-quoted) for symmetry:
    # Postgres '"hello"'::text yields "hello" (quoted) while Snowflake
    # CAST(variant AS STRING) yields hello (unquoted).
    #
    # JSON null is left as the original text: Snowflake collapses a null VARIANT
    # to SQL NULL (the SQL wrapper emits '<<NULL>>'), and rewriting it here
    # would blur that genuinely different case.
    if canonical is None:
        return value
    if isinstance(canonical, str):
        return canonical
    if isinstance(canonical, bool):
        return "True" if canonical else "False"
    if isinstance(canonical, Decimal):
        return format(canonical, "f")
    if isinstance(canonical, int):
        return str(canonical)
    if isinstance(canonical, float):
        return format(canonical, "f")

    try:
        return _serialize(canonical)
    except (TypeError, ValueError):
        return value


# ── DataFrame application ────────────────────────────────────────────────────

def _column_looks_semi_structured(series):
    """True when a sample of non-null cells in series looks semi-structured."""
    try:
        sample = series.dropna().head(_SNIFF_SAMPLE)
    except AttributeError:
        return False
    for cell in sample:
        if looks_semi_structured(cell):
            return True
    return False


import re as _re

_NUMERIC_STR_RE = _re.compile(r'^-?\d+\.\d+$')


def _normalize_numeric_str(val):
    """'400000.000000' → '400000.00', '620000.75' → '620000.75'.

    Only touches strings that look like plain decimals so JSON/timestamps/
    other strings are left alone.  Non-string or non-numeric values pass
    through unchanged.
    """
    if not isinstance(val, str):
        return val
    s = val.strip()
    if not _NUMERIC_STR_RE.match(s):
        return val
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return val


def looks_numeric_string(value):
    """True for a plain-decimal string like '400000.000000' -- the same shape
    _normalize_numeric_str rounds to 2dp. Used by hybrid_v1's SQL distinct_count
    (Project/tiered_runner.py) to identify columns where two differently-precise
    numeric-string representations would count as distinct in raw SQL but as
    one canonical value after this module's normalization -- see
    docs/large-table-scalable-architecture §R.2.2."""
    return isinstance(value, str) and bool(_NUMERIC_STR_RE.match(value.strip()))


def canonicalize_frames(source_df, target_df):
    """Canonicalize semi-structured and numeric columns in both frames, symmetrically.

    1. Numeric strings: both engines emit the same decimal value with different
       precision ('400000.00' vs '400000.000000').  Round all plain-decimal
       string columns to 2dp so they compare equal.
    2. Semi-structured (JSON/JSONB/HStore): canonicalize via canonicalize_value
       so key order and whitespace don't cause false mismatches.

    The union of candidates from both frames is processed on both sides — never
    one side only, as that guarantees a false mismatch.

    Returns the (possibly modified) frames. Mutates in place and returns for
    call-site convenience.
    """
    if source_df is None or target_df is None:
        return source_df, target_df

    shared = [c for c in source_df.columns if c in set(target_df.columns)]

    # ── 1. Numeric string normalization (2dp) ────────────────────────────────
    for col in shared:
        src_sample = source_df[col].dropna()
        tgt_sample = target_df[col].dropna()
        # Apply if either side has at least one plain decimal string
        src_has = src_sample.apply(lambda v: bool(_NUMERIC_STR_RE.match(str(v).strip())) if isinstance(v, str) else False).any()
        tgt_has = tgt_sample.apply(lambda v: bool(_NUMERIC_STR_RE.match(str(v).strip())) if isinstance(v, str) else False).any()
        if src_has or tgt_has:
            source_df[col] = source_df[col].map(_normalize_numeric_str)
            target_df[col] = target_df[col].map(_normalize_numeric_str)

    # ── 2. Semi-structured canonicalization ──────────────────────────────────
    candidates = [
        col for col in shared
        if _column_looks_semi_structured(source_df[col])
        or _column_looks_semi_structured(target_df[col])
    ]

    for col in candidates:
        source_df[col] = source_df[col].map(canonicalize_value)
        target_df[col] = target_df[col].map(canonicalize_value)

    return source_df, target_df
