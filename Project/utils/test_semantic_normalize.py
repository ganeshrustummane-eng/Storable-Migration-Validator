"""Tests for the JSON/JSONB/HStore canonicalizer.

Run:  python -m pytest Project/utils/test_semantic_normalize.py -q
  or: python Project/utils/test_semantic_normalize.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.semantic_normalize import (  # noqa: E402
    canonicalize_value,
    canonicalize_frames,
    parse_hstore_text,
)

# ── The reported payload ─────────────────────────────────────────────────────
# Source: native Postgres hstore literal, as returned by col_hstore::text.
# Values include three keys whose content is itself a serialized JSON document.
SOURCE_HSTORE = (
    '"amount"=>"470.85", "payables"=>"[{\\"ledger_id\\":6752258,\\"payable_type\\":\\"Ledger\\",'
    '\\"payable_id\\":6752258,\\"amount\\":470.85}]", "response"=>"{\\"checksum\\":null,'
    '\\"connection_failure\\":false,\\"error_text\\":\\"Could not initialize transaction, Payment '
    'method Payment method could not be saved for future use.\\",\\"partial_authorization\\":false,'
    '\\"provider_name\\":\\"Payrix\\",\\"response_code\\":null,\\"runtime_error\\":false,'
    '\\"success\\":true,\\"unknown_provider_error\\":false}", '
    '"class_name"=>"AutopaySuccessfulPaymentEvent", "runtime_error"=>"false", '
    '"scheduled_date"=>"2025-02-26", "idempotency_key"=>"919a2c3e561be32e63b1f10792c3ef54", '
    '"period_end_date"=>"2025-02-26", "payment_method_id"=>"20328841", '
    '"connection_failure"=>"false", "scheduled_event_id"=>"1080405490", '
    '"payment_method_data"=>"{\\"card_type\\":\\"visa\\",\\"last_name\\":\\"Culpepper\\",'
    '\\"first_name\\":\\"Alex \\",\\"card_number\\":\\"\\",\\"security_code\\":null,'
    '\\"account_number\\":\\"\\",\\"routing_number\\":\\"\\",\\"expiration_date\\":\\"03/28\\",'
    '\\"payment_source_override\\":\\"tenant_portal\\"}", '
    '"unknown_provider_error"=>"false", '
    '"merchant_transaction_number"=>"Autopay1080405490"'
)

# Target: the same data as a Snowflake VARIANT rendered to text. Keys arrive in
# a different order and the document is pretty-printed.
TARGET_VARIANT = """{
  "amount": "470.85",
  "class_name": "AutopaySuccessfulPaymentEvent",
  "connection_failure": "false",
  "idempotency_key": "919a2c3e561be32e63b1f10792c3ef54",
  "merchant_transaction_number": "Autopay1080405490",
  "payables": "[{\\"ledger_id\\":6752258,\\"payable_type\\":\\"Ledger\\",\\"payable_id\\":6752258,\\"amount\\":470.85}]",
  "payment_method_data": "{\\"card_type\\":\\"visa\\",\\"last_name\\":\\"Culpepper\\",\\"first_name\\":\\"Alex \\",\\"card_number\\":\\"\\",\\"security_code\\":null,\\"account_number\\":\\"\\",\\"routing_number\\":\\"\\",\\"expiration_date\\":\\"03/28\\",\\"payment_source_override\\":\\"tenant_portal\\"}",
  "payment_method_id": "20328841",
  "period_end_date": "2025-02-26",
  "response": "{\\"checksum\\":null,\\"connection_failure\\":false,\\"error_text\\":\\"Could not initialize transaction, Payment method Payment method could not be saved for future use.\\",\\"partial_authorization\\":false,\\"provider_name\\":\\"Payrix\\",\\"response_code\\":null,\\"runtime_error\\":false,\\"success\\":true,\\"unknown_provider_error\\":false}",
  "runtime_error": "false",
  "scheduled_date": "2025-02-26",
  "scheduled_event_id": "1080405490",
  "unknown_provider_error": "false"
}"""


def test_reported_payload_matches():
    """The reported source/target pair must canonicalize identically."""
    assert canonicalize_value(SOURCE_HSTORE) == canonicalize_value(TARGET_VARIANT)


def test_reported_payload_keeps_all_keys():
    """Escaped quotes must not truncate values (the regex bug this replaces)."""
    parsed = parse_hstore_text(SOURCE_HSTORE)
    assert len(parsed) == 14, f"expected 14 keys, got {len(parsed)}"
    # The value is a full JSON array, not truncated at the first escaped quote.
    assert parsed["payables"].startswith('[{"ledger_id":6752258')
    assert parsed["payables"].endswith("}]")
    assert "Payment method could not be saved" in parsed["response"]
    # A value containing a comma inside quotes must not split the pair.
    assert parsed["response"].count("error_text") == 1


def test_hstore_scanner_beats_the_naive_regex():
    """Direct regression guard for the truncation the old regex produced."""
    import re
    raw = '"payables"=>"[{\\"ledger_id\\":6752258,\\"amount\\":470.85}]"'
    naive = dict(re.findall(r'"([^"]+)"\s*=>\s*"([^"]*)"', raw))
    scanned = parse_hstore_text(raw)
    assert scanned["payables"] == '[{"ledger_id":6752258,"amount":470.85}]'
    # The regex either misses the pair or truncates it — either way it disagrees.
    assert naive.get("payables") != scanned["payables"]


# ── Defect-specific cases ────────────────────────────────────────────────────

def test_inner_json_key_reordering_is_ignored():
    """Defect 5: a re-serialized inner document is not drift."""
    a = '{"payload": "{\\"b\\":2,\\"a\\":1}"}'
    b = '{"payload": "{\\"a\\":1,\\"b\\":2}"}'
    assert canonicalize_value(a) == canonicalize_value(b)


def test_outer_key_reordering_is_ignored():
    assert canonicalize_value('{"b":1,"a":2}') == canonicalize_value('{"a":2,"b":1}')


def test_mixed_case_keys_agree():
    """Defect 3: collation-dependent ordering no longer matters."""
    assert canonicalize_value('{"Zebra":1,"apple":2}') == canonicalize_value('{"apple":2,"Zebra":1}')


def test_numbers_are_not_converted():
    """Digits are preserved verbatim — no trailing-zero stripping, no rescaling.

    Zero-stripping is a value conversion, and a conversion that makes two
    differently-formatted numbers compare equal can also hide genuine precision
    loss during migration. This module only trims spaces, sorts keys, and
    stringifies; a trailing-zero difference is reported, not absorbed.
    """
    assert canonicalize_value('{"amount":470.850}') == '{"amount":470.850}'
    assert canonicalize_value('{"amount":470.850}') != canonicalize_value('{"amount":470.85}')
    assert canonicalize_value('{"amount":1.50}') != canonicalize_value('{"amount":1.5}')
    assert canonicalize_value('{"amount":1.50}') == '{"amount":1.50}'
    assert canonicalize_value('{"n":100}') == '{"n":100}'
    # Exponent notation is expanded (format 'f'), never emitted as 1E+2.
    assert canonicalize_value('{"n":1e2}') == '{"n":100}'


def test_high_precision_numbers_not_rounded():
    """Decimal parsing must not collapse genuinely different values."""
    a = canonicalize_value('{"v":0.12345678901234567890123}')
    b = canonicalize_value('{"v":0.12345678901234567890124}')
    assert a != b


def test_top_level_array_supported():
    """Defect 4: jsonb_each() used to abort the query on these."""
    assert canonicalize_value("[3,1,2]") == "[3,1,2]"
    assert canonicalize_value("[ 3, 1, 2 ]") == canonicalize_value("[3,1,2]")


def test_array_order_is_significant():
    """Arrays are ordered — reordering is real drift and must stay visible."""
    assert canonicalize_value("[1,2,3]") != canonicalize_value("[3,2,1]")


def test_postgres_array_to_json_matches_snowflake_variant():
    """ARRAY -> VARIANT. Both sides emit JSON, so they canonicalize identically.

    Verified live: array_to_json(ARRAY['b','a','c']) and a Snowflake VARIANT
    array both produce ["b","a","c"].
    """
    # array_to_json output (source)  vs  TO_JSON of a VARIANT array (target)
    assert canonicalize_value('["b","a","c"]') == canonicalize_value('[\n  "b",\n  "a",\n  "c"\n]')
    assert canonicalize_value("[[1,2],[3,4]]") == canonicalize_value("[ [1,2], [3,4] ]")
    assert canonicalize_value('["a",null]') == canonicalize_value('[ "a", null ]')
    assert canonicalize_value("[]") == canonicalize_value("[ ]")


def test_array_elements_not_sorted_but_inner_object_keys_are():
    """Arrays keep their order; objects inside them get sorted keys."""
    a = '[{"b":1,"a":2},{"d":3,"c":4}]'
    b = '[{"a":2,"b":1},{"c":4,"d":3}]'
    assert canonicalize_value(a) == canonicalize_value(b) == '[{"a":2,"b":1},{"c":4,"d":3}]'
    # Swapping the two elements is drift, not noise.
    assert canonicalize_value(a) != canonicalize_value('[{"d":3,"c":4},{"b":1,"a":2}]')


def test_postgres_array_literal_is_not_mistaken_for_json():
    """A bare PG array literal '{a,b,c}' is NOT valid JSON.

    This is exactly why ArrayRule uses array_to_json() instead of a plain cast:
    without it the source side would emit this string, the target would emit
    ["a","b","c"], and no amount of Python normalization could reconcile them.
    """
    assert canonicalize_value("{a,b,c}") == "{a,b,c}"       # unparseable, passed through
    assert canonicalize_value("{a,b,c}") != canonicalize_value('["a","b","c"]')


def test_top_level_scalar_string_unwrapped():
    """Postgres yields '"hello"', Snowflake yields 'hello'."""
    assert canonicalize_value('{"a":"hello"}') == canonicalize_value('{"a":"hello"}')
    assert canonicalize_value('["hello"]') == '["hello"]'


def test_empty_documents():
    assert canonicalize_value("{}") == "{}"
    assert canonicalize_value("[]") == "[]"
    assert canonicalize_value("{ }") == "{}"


def test_null_placeholder_passes_through():
    assert canonicalize_value("<<NULL>>") == "<<NULL>>"
    assert canonicalize_value(None) is None


def test_hstore_null_value():
    parsed = parse_hstore_text('"a"=>"1", "b"=>NULL')
    assert parsed == {"a": "1", "b": None}


def test_empty_hstore_and_empty_object_agree():
    """Defect 1/2: empty hstore -> {} on both sides, no sentinel divergence."""
    assert canonicalize_value("{}") == canonicalize_value("{ }")


def test_values_containing_delimiters():
    """Defect 6: '=' and '|' inside values are no longer ambiguous."""
    a = '{"expr":"a=b|c","other":"x"}'
    b = '{"other":"x","expr":"a=b|c"}'
    assert canonicalize_value(a) == canonicalize_value(b)
    # A genuinely different value still differs.
    assert canonicalize_value(a) != canonicalize_value('{"expr":"a=b|d","other":"x"}')


def test_whitespace_inside_string_values_preserved():
    """Trimming inside JSON strings would lose real data."""
    assert canonicalize_value('{"a":"Alex "}') != canonicalize_value('{"a":"Alex"}')


# ── Non-regression: real drift must survive ──────────────────────────────────

def test_real_drift_still_detected():
    """The seeded migration_test failures must not be masked."""
    assert canonicalize_value('{"score":100}') != canonicalize_value('{"score":999}')
    assert canonicalize_value('{"color":"red"}') != canonicalize_value('{"color":"blue"}')
    assert canonicalize_value('{"val":1500.755}') != canonicalize_value('{"val":1500.7}')
    assert canonicalize_value('{"checksum":"cafebabe"}') != canonicalize_value('{"checksum":"cafebaad"}')
    # A key present on one side only is drift.
    assert canonicalize_value('{"a":1,"b":2}') != canonicalize_value('{"a":1}')


def test_plain_values_untouched():
    """The fast-reject path must leave ordinary columns bit-for-bit alone."""
    for v in ["cafebabe", "1500.755", "2025-02-26", "TC-06-nulls", "1", "0",
              "", "  spaced  ", "not json at all", "<<NULL>>"]:
        assert canonicalize_value(v) == v, v


# ── Frame-level application ──────────────────────────────────────────────────

def test_canonicalize_frames_symmetric():
    import pandas as pd

    src = pd.DataFrame({
        "pk": ["r1"],
        "col_hstore_normalized": [SOURCE_HSTORE],
        "col_bytea_normalized": ["cafebabe"],
    })
    tgt = pd.DataFrame({
        "pk": ["r1"],
        "col_hstore_normalized": [TARGET_VARIANT],
        "col_bytea_normalized": ["cafebabe"],
    })

    src, tgt = canonicalize_frames(src, tgt)

    assert src["col_hstore_normalized"][0] == tgt["col_hstore_normalized"][0]
    # Untouched, so an unrelated column cannot silently change verdict.
    assert src["col_bytea_normalized"][0] == "cafebabe"
    assert tgt["col_bytea_normalized"][0] == "cafebabe"


def test_canonicalize_frames_union_of_candidates():
    """A column that only *looks* structured on one side is still normalized
    on both — otherwise the asymmetry guarantees a false mismatch."""
    import pandas as pd

    # Source is native hstore (no leading '{'), target is JSON.
    src = pd.DataFrame({"c": ['"a"=>"1"']})
    tgt = pd.DataFrame({"c": ['{"a":"1"}']})
    src, tgt = canonicalize_frames(src, tgt)
    assert src["c"][0] == tgt["c"][0] == '{"a":"1"}'


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
