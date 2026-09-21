---
name: base-rules-datatypes
description: "Use when working on the static, immutable base type-mapping rules -- src/rules/rules_catalog.json (the 10 base rule entries) and src/rules/base_rules.py (the Python rule classes that actually execute). Covers what source->target datatype pairs are covered and how canonicalization moved from SQL to Python for JSON/HStore/Array. Distinct from learned-rules (the mutable, human-gated feedback loop). Files: src/rules/rules_catalog.json, src/rules/base_rules.py, src/rule_book.py."
---

# Base rules and datatypes -- the static, pre-tested rule catalog

## Scope boundary vs. learned-rules

This skill covers only the immutable base rules -- loaded from
`src/rules/rules_catalog.json` and implemented as Python classes in
`src/rules/base_rules.py`. These are trusted, pre-tested, and per `rule_book.py`'s
own module docstring: **do not edit them at runtime.** Anything mutable
(gap-filler rules, human corrections) belongs to the **learned-rules** skill, not
this one.

## The 10 base rules

`rules_catalog.json` (version 3.1) defines 10 rule entries, each with an
`id`/`enum_value`, `display_name`, `description`, `pg_type_pairs` (source type ->
target type matrix), `pg_expression`/`sf_expression` SQL templates (metadata for AI
prompts only -- see caveat below), `null_handling`, and `notes`:

| Rule | Source types (Postgres-family) | Target types (Snowflake) |
|---|---|---|
| `boolean` | boolean, bool | BOOLEAN, BOOL |
| `numeric` | numeric, decimal, float, real, double precision, money | NUMBER, NUMERIC, DECIMAL, FLOAT |
| `timestamp_ntz` | timestamp without time zone, timestamp, timestamp_ntz | TIMESTAMP_NTZ, TIMESTAMP |
| `timestamp_tz` | timestamp with time zone, timestamptz, timestamp_tz | TIMESTAMP_TZ |
| `date` | date | DATE |
| `text` | character varying, varchar, char, text, plus wildcard `*`->`*` fallback | TEXT, VARCHAR, STRING, CHAR |
| `uuid` | uuid | TEXT, VARCHAR, STRING, UUID |
| `integer` | integer, int, bigint, smallint, serial, bigserial | NUMBER, INTEGER |
| `json` | json, jsonb | VARIANT, VARCHAR, STRING, TEXT, ARRAY |
| `hstore` | hstore | TEXT, VARCHAR, STRING, VARIANT |
| `bytea` | bytea, binary, varbinary | BINARY, VARBINARY, VARCHAR, STRING |

Plus two special top-level entries: `null_rule` and `fivetran_filter` (the
`_FIVETRAN_ACTIVE = TRUE` filter metadata), and a `rule_application_order` list.

Corresponding Python classes in `base_rules.py`: `BooleanRule`, `NumericRule`,
`TimestampTZRule`, `TimestampNTZRule`, `DateRule`, `TextRule`, `UUIDRule`,
`IntegerRule`, `JSONRule`, `ByteaRule`, `HStoreRule`, plus `ArrayRule` and
`NullPlaceholderRule` (not separately listed in the JSON catalog, but present in
code).

## Important caveat: the JSON file's SQL templates are stale for JSON/HStore

`rules_catalog.json`'s embedded `pg_expression`/`sf_expression` for the `json` and
`hstore` rules describe an in-SQL recursive-flatten canonicalization. **The actual
`JSONRule`/`HStoreRule` classes in `base_rules.py` no longer do this** --
canonicalization for semi-structured types moved to Python
(`Project/utils/semantic_normalize.py`'s `canonicalize_frames()`/`canonicalize_value()`),
with documented, concrete reasons two SQL engines can't be trusted to agree:
Snowflake's `LISTAGG` returns `''` not `NULL`, collation differences affect
`ORDER BY`-dependent flattening, and Postgres's `jsonb_each()` hard-errors on a
top-level JSON array or scalar. `rule_book.py` only reads the JSON file's
expression fields for AI-prompt display text -- never for actual SQL generation.
**Do not "fix" `JSONRule`/`HStoreRule` to match the JSON file's stale templates --
the JSON file is what's out of date, not the Python classes.**

The real SQL bridge for VARIANT/JSON/HStore is a single expression both classes
emit: `COALESCE(TO_JSON(TRY_PARSE_JSON(CAST({col} AS STRING))), CAST({col} AS STRING))`
-- deliberately avoiding Snowflake's `TYPEOF()` because it rejects VARCHAR outright
(comment in code notes this was "verified against live Snowflake").

## Timestamp handling asymmetry (by design, not a bug)

`TimestampTZRule` converts to UTC first (`AT TIME ZONE 'UTC'` on Postgres,
`CONVERT_TIMEZONE('UTC', ...)` on Snowflake) then formats to microsecond precision.
`TimestampNTZRule` does **not** do timezone conversion -- only microsecond
formatting. Don't "fix" NTZ to also convert timezones; a naive-timestamp column has
no timezone to convert from.

## Known documentation drift -- do not trust blindly

`docs/rules/rule-book.md`'s "Transformation Rules" section lists only 5 rule types
(`text`, `timestamp`, `boolean`, `integer`, `json`) and doesn't split
`timestamp_ntz`/`timestamp_tz`. The actual base-rule set has 10 entries. Treat that
doc section as a simplified/stale summary, not a complete spec -- `rules_catalog.json`
+ `base_rules.py` are the ground truth for what rules actually exist.

There is no literal "exact-match / cast / normalize / currency-convert / custom-SQL
/ skip" taxonomy anywhere in code under those names -- if asked to categorize a rule
this way, be clear that's an informal description, not a real code construct.

## Verification checklist

- [ ] Never edit a base rule (JSON catalog entry or Python class) to "fix" a gap for
  one type pair -- that's what the learned-rules gap-filler mechanism is for.
- [ ] Don't trust `rules_catalog.json`'s `pg_expression`/`sf_expression` fields as
  the executing SQL for `json`/`hstore` -- check `base_rules.py`'s actual class.
- [ ] Confirm which of the 10 rules actually matches a type pair via
  `RuleBook.get_rule_for_type()`'s exact-match logic before assuming a "gap" exists.
- [ ] Cross-check `docs/rules/rule-book.md` claims against `rules_catalog.json`/
  `base_rules.py` before repeating them -- the doc is known to be simplified/stale.
- [ ] `py_compile` any touched `.py` file before calling the change done.
