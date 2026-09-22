# Scalable Architecture Design — Migration Validator at 200–300M Row Scale

**Status:** Design-only. No code changed, no code will be changed until this is approved.
**Builds on:** [`docs/performance-audit-large-tables/README.md`](../performance-audit-large-tables/README.md) (the audit that identified the bottlenecks this document redesigns around).
**Correctness oracle:** the current `Project/main.py` implementation, as it exists on branch `version1.4` today. Every candidate architecture below is judged against it, not against an idealized spec.

Labeling used throughout: **FACT** (observed directly in code, cited by file:line) / **INFERENCE** (a conclusion derived from those facts, not itself directly observed) / **PROPOSAL** (a future design choice, not yet decided or built).

---

## A. Existing validation semantics (traced from code, not comments)

### A.1 Source/target selection
**FACT** — `Project/main.py:178-192` reads `source`, `source_query` (`sourcequery`), `target`, `target_query` (`targetquery`) directly out of the per-table YAML block. There is no query construction inside `main.py` — it executes exactly the SQL string that was baked into the YAML at generation time by `src/generated_queries/sql_query_generator.py` / `ai_sql_generator.py` / `yaml_config_writer.py`. `main.py` has zero knowledge of what filters, joins, or exclusions that SQL string contains.

### A.2 Columns retrieved
**FACT** — Whatever the `SELECT` list in `sourcequery`/`targetquery` specifies. `main.py:247-248` lower-cases and strips whitespace from both frames' column names immediately after fetch — this is the only column-name normalization `main.py` performs itself.

### A.3 Fivetran/source/target filters
**FACT** — `_FIVETRAN_ACTIVE = TRUE` is appended to the Snowflake-side query by `sql_query_generator.py:169,236,501` at YAML-generation time, not at run time. `main.py` never adds, checks, or is even aware of this filter — it just runs the query string. Same for any `source_filter`/`target_filter` the user configured through the UI: those are compiled into the SQL text before `main.py` ever sees it (`validation_config.get("source_filter")`/`target_filter"` at `main.py:506-507` are only read back out for the audit-log record, never applied).

### A.4 Exclusions
**FACT** — Same story as A.3: column exclusions (`config/*_exclusions.yaml`, table-specific runtime exclusions) are applied when the SQL `SELECT` list is generated (outside `main.py`'s scope). By the time `main.py` runs, an excluded column simply never appears in `source_df`/`target_df`'s columns — there's no separate exclusion-filtering step inside `main.py`. Note the staleness guard at `main.py:212-223`: it *logs a warning* if `config/<source>_exclusions.yaml` was edited after the YAML was generated, but does not re-apply anything.

### A.5 Semantic normalization
**FACT** — `canonicalize_frames(source_df, target_df)` (`main.py:256`, defined in `Project/utils/semantic_normalize.py:378-420`) runs unconditionally on both frames, once, after fetch and before any comparison:
1. **Numeric-string normalization** — for every column shared by name between the two frames, if *either* side has at least one value matching `^-?\d+\.\d+$` as a string, both sides get that column reformatted via `f"{float(s):.2f}"` (2 decimal places). Applied even to columns that already match.
2. **Semi-structured (JSON/JSONB/HStore) canonicalization** — for every shared column where a 200-row sample on *either* side looks semi-structured (`looks_semi_structured`: starts with `{`/`[`/`"` or contains `=>`), every cell is parsed (JSON first, native hstore literal as fallback) and re-serialized via `_serialize()`: dict keys sorted by codepoint, array elements sorted by their serialized form, booleans as `"True"`/`"False"`, numbers rounded to 2dp, JSON `null` as the literal string `"null"`, everything else as `str()`.
3. Values that fail to parse, or aren't strings, pass through unchanged (`canonicalize_value`, semantic_normalize.py:292-338).
4. This step touches **both frames symmetrically always** — never one side only.

### A.6 PK-based matching
**FACT** — `main.py:286-296`: if `pksourcecolumn`/`pktargetcolumn` isn't configured, both are forced to `"row_hash"`. Otherwise they're lower-cased (scalar) or lower-cased element-wise (list, for composite PK). `main.py:332-333`: `source_df.set_index(pk_src).sort_index()` and same for target — this is a **pandas index sort**, not a SQL `ORDER BY`; the two frames are independently sorted by their own PK values after arriving fully materialized.

### A.7 Composite PKs
**FACT** — `isinstance(pk_src, list)` (`main.py:331`) triggers `set_index()` on multiple columns, producing a `MultiIndex`. `main.py:334-335` aligns the target's MultiIndex names to the source's so cross-frame lookups don't fail on name mismatch. `_row_key_str` (`main.py:365-368`) joins a tuple PK's parts with `|` for the human-readable `row_key` column in the CSV.

### A.8 No-PK tables
**FACT** — Falls to the `row_hash` path (A.6). This is the *only* mechanism for PK-less tables; there is no separate "PK-less" code branch — `row_hash` is treated as if it were a real PK column once computed.

### A.9 Exact row_hash generation — **this is the critical piece for §F below**
**FACT** — `main.py:298-324`, only triggered when `pk_src == "row_hash"` **and** `"row_hash" not in source_df.columns`:
1. Column set: `_hash_spec = validation_config["validation_plan"]["row_hash"]` (if present) supplies an explicit `columns` list; if given, `_common` = the intersection of that list with columns present in **both** frames, in the **order given in the YAML config**, not sorted. If not given, `_common` = `[c for c in source_df.columns if c in set(target_df.columns)]` — **iteration order follows `source_df.columns`'s order**, i.e. the order columns appear in `sourcequery`'s `SELECT` list.
2. Algorithm: `validation_plan.row_hash.algorithm`, default `"MD5"`; only `"SHA256"` or anything else (falls to `md5`) — read via `hashlib.new(_hash_name, ...)`.
3. Per-cell stringification (`_hash_row._v`, `main.py:315-320`):
   - `None` or NaN float → literal string `"<<NULL>>"`.
   - Python `str` → `.strip()`'d.
   - everything else → `str(v)` unchanged (no type-specific formatting — an `int` and `float` that print differently, e.g. `5` vs `5.0`, hash differently).
4. Concatenation: `"|".join(_v(c) for c in cols)` — pipe-delimited, **no escaping** of a literal `|` inside a value, **no per-column length/type tagging** — `("a|b", "c")` and `("a", "b|c")` hash identically.
5. `hashlib.new(name, joined.encode()).hexdigest()` — one hash per row, computed **independently and identically on both `source_df` and `target_df`** (same function object, same `cols` list) — this is what makes the fallback symmetric.
6. Both frames get a `row_hash` column added; `pk_src = pk_tgt = "row_hash"` from then on, and the rest of the pipeline (A.6-A.15) treats it exactly like a real PK.

**INFERENCE**: this hash is computed from **already-fetched, already-Python-typed, already-canonicalized (semantic_normalize ran first, at `main.py:256`, before this code at `main.py:298`)** values. It is *not* a hash of the raw SQL result — it's a hash of the post-canonicalization Python representation. This matters enormously for §F: any SQL-side hash would need to reproduce not just Postgres/Snowflake type formatting but also the JSON-canonicalization and numeric-2dp-rounding rules from `semantic_normalize.py`, or it will disagree with the current oracle on exactly the rows that most need catching (JSON/decimal drift).

### A.10 Columns participating in row_hash
**FACT** — See A.9.1. Configurable via YAML (`validation_plan.row_hash.columns`); defaults to the full shared-column set in source-column order. Excluded columns never reach `source_df`/`target_df` in the first place (A.4), so they're automatically excluded from the hash too — but there is no *additional* row_hash-specific exclusion list.

### A.11 NULL representation in hashing/comparison
**FACT** — Two independent, near-identical, but not code-shared implementations of the same sentinel:
- Hashing: `_hash_row._v` (`main.py:317-318`) → `"<<NULL>>"` for `None` or NaN.
- Cell comparison (non-hash path): `_cell_str` (`main.py:376-377`) → `"<<NULL>>"` for `None` or NaN, identical string.
- `semantic_normalize.py`'s `NULL_PLACEHOLDER = "<<NULL>>"` (line 54) is the same literal, documented as "emitted by the SQL COALESCE wrapper for SQL NULL" — i.e. the SQL generator side is *expected* to already emit this exact string for a NULL column value in some code paths, and Python-side NULL handling was written to match it. This sentinel string is a three-way convention (SQL generator, semantic_normalize, main.py) that must stay textually identical everywhere or NULL rows silently stop matching.

### A.12 Missing source rows
**FACT** — `main.py:429-430`: for a PK present in the union (`all_pks`) but absent from `src.index` (`_to_df` returns an empty frame when `pk_val not in frame.index`, `main.py:388-390`), the row status is `TARGET_ONLY` (source-missing = present-only-in-target).

### A.13 Extra target rows
Same code path, symmetric: `main.py:431-432` — PK present in `tgt.index` but not `src.index` → `SOURCE_ONLY`.

### A.14 Changed non-PK values
**FACT** — `main.py:433-445`: for a PK present on both sides, **all rows sharing that PK on each side** are pulled (`_to_df` can return >1 row per PK — see A.15), each row is rendered to a single pipe-joined string over `common_cols` only (`_cell_str` per cell, A.11's NULL rule + 2dp float formatting), the **list of per-row strings is sorted independently on each side**, and the two sorted lists are compared for exact list equality. Equal → `PASS`; not equal → `FAIL`. This is a **multiset comparison**, not a positional row comparison — row order within a duplicate-PK group never causes a false FAIL.

### A.15 Duplicate PKs
**FACT** — Handled by A.14's multiset design: `_to_df` (`main.py:388-392`) returns every row matching that PK value as a DataFrame (not just the first). If source has 2 rows for PK=X and target has 2 rows for PK=X, both groups are rendered to sorted string-lists and compared as sets-of-strings (with multiplicity, since it's a sorted list, not a `set()` — two identical duplicate rows plus one different row would still correctly show as unequal length... **actually not fully true**: Python list equality requires same length AND same elements at same positions after sort, so a genuine duplicate-count mismatch, e.g. source has PK=X twice identically and target has it once, produces a length mismatch → `FAIL`, which is correct behavior). The CSV record for that PK, however, only ever stores `s_df.iloc[0]` / `t_df.iloc[0]` (`main.py:445`) — **only the first physical row of a duplicate-PK group is shown in the output CSV's `__source`/`__target` columns**, even though all rows for that PK were used to compute PASS/FAIL. This is a **display limitation, not a correctness bug** in the FAIL/PASS decision itself, but it means a human reviewing a FAILed duplicate-PK row in the CSV may not see *which* of the N duplicate rows actually differed.

### A.16 Final row-level result construction
**FACT** — `_rec()` (`main.py:408-427`) builds one dict per unique PK with: `row_key`, `status`, then for every column in `display_cols` (= all source columns + any target-only columns, A.16 below) both a `{col}__source` and `{col}__target` field (empty string `""` if that side has no row for this PK or the column isn't present on that side), then — only if the YAML declares `transformation_specs` — additional `{name}__expected/__actual/__difference/__status` fields per configured transformation (e.g. currency conversion checks). All dicts collected into `rows` (`main.py:402`), converted to `pd.DataFrame(rows)` (`main.py:447`), sorted by `row_key`, and reset-indexed.

### A.17 Result columns required by UI/CSV consumers
**FACT** — `webapp/app.py`'s Output Files tab and `runner.py:114-115` glob for `*_result_*.csv` (all rows) and `*_failed_*.csv` (`result_df[status != "PASS"]`, `main.py:452-456` — a **boolean-mask copy**, not a view) by filename pattern only; neither reads specific column names beyond generic display, so no column in `_rec()`'s output is a hard dependency of the UI beyond `status` (used for `result_df["status"] != "PASS"` filtering) and `row_key` (used for the final sort). The **summary CSV** (`create_summary`, written once per validation_name/table via `main.py:522`, read back by `runner.py:110-112`) is a separate, much smaller aggregate: run_at, run_id, validation_name, source/target table+type, source_rows, target_rows, status, timings — this is what the webapp's dashboard actually renders as pass/fail per table; the per-row CSVs are drill-down detail, not the primary signal.

---

## B. Non-negotiable correctness invariants

Any redesign MUST preserve every one of these exactly, because each is directly load-bearing per §A:

1. **Query text is the sole filter/exclusion boundary.** `main.py` (and any replacement) must keep treating `sourcequery`/`targetquery` as pre-filtered, pre-excluded, complete SELECT statements. A redesign must never re-derive or duplicate Fivetran/exclusion logic — it must execute (or push down) *exactly* those strings, unmodified, on both sides.
2. **`canonicalize_frames` semantics run before comparison, symmetrically, on every table** — numeric 2dp rounding + JSON/hstore canonicalization (§A.5). A redesign that pushes comparison into SQL must either (a) reproduce this exact canonicalization in SQL/UDF form with byte-identical output for every input class documented in `semantic_normalize.py`'s docstring (json/hstore/timestamp/decimal), or (b) keep this step in Python and only push the *simple, already-canonicalized* comparison downstream.
3. **`row_hash` fallback columns, ordering, algorithm, NULL sentinel, and delimiter must be reproduced exactly** (§A.9) if row_hash computation itself moves — see §F, this is flagged as NOT guaranteed reproducible in SQL without extra work.
4. **PASS decision is a sorted-multiset-of-rendered-rows comparison over `common_cols`, per unique PK, not a positional/vectorized cell-by-cell diff** (§A.14). This exact semantic — which absorbs duplicate-PK-group row-order differences — must be preserved by any vectorized/merge-based redesign; a naive `pd.merge` + column-wise `!=` changes behavior on any table with duplicate PKs.
5. **SOURCE_ONLY / TARGET_ONLY classification is symmetric set-membership on the PK/row_hash index**, independent of row content (§A.12-13).
6. **Duplicate-PK count mismatches are real FAILs** — a redesign that de-duplicates before comparing (e.g. `DISTINCT` in a pushdown query) would silently turn a duplication migration bug into a false PASS. This is the single easiest invariant to accidentally break with a SQL-side JOIN/EXCEPT rewrite.
7. **NULL sentinel string `"<<NULL>>"` must stay textually identical** across every comparison surface (Python cell comparison, Python hash, and any future SQL-side equivalent) — it is already a three-way convention (§A.11) and a fourth (SQL-pushdown) implementation must match it exactly or every legitimately-NULL row silently mismatches.
8. **All rows get a status row — PASS included — not only failures** (per `CLAUDE.md`'s explicit requirement, and confirmed structurally: `result_df` holds every PK, `failed_df` is a filtered subset written *in addition to*, not instead of, the full result).
9. **Every column pair appears in the output CSV as `{col}__source`/`{col}__target`** for every PK, even for SOURCE_ONLY/TARGET_ONLY rows (empty string on the absent side) — this is what makes the CSV usable for manual triage; a redesign that only returns mismatched cells would be a behavior change, not just a performance change.
10. **Schema-drift columns (present on only one side) are excluded from comparison but still appear in the display columns** (`display_cols`, §A.16) and are logged as warnings, not folded into FAIL — a redesign must not start treating a source-only or target-only *column* the same as a source-only or target-only *row*.
11. **Composite-PK identity and ordering rules** (§A.7) — MultiIndex alignment by name, not position.
12. **The transformation-spec columns** (`{name}__expected/__actual/__difference/__status`) are computed per-PK from `s_row`/`t_row`'s `{name}_value` fields — this is currently only computed off `.iloc[0]` of a PK group (same first-row limitation as §A.15) and must not silently change scope.

### Ambiguous/potentially-incorrect existing behavior (flagged separately, per instructions — NOT to be "fixed" as part of this redesign)

- **§A.15 duplicate-PK display**: only the first physical row per PK is shown in `__source`/`__target` CSV columns even when the group has multiple differing rows. This may already under-inform triage today; a redesign should reproduce this exactly (bug-compatible), not silently improve it, since "improve" here is a semantic change relative to the oracle.
- **§A.9.3 row_hash cell stringification**: `str(v)` with no type tag means an `int` `5` and a `float` 5.0 that happen to arrive with different Python types (e.g. one side's driver returns `int64`, the other `float64` for the same nullable integer column) hash differently even though `_cell_str`'s non-hash comparison path formats floats to 2dp specifically to avoid this class of false mismatch. **This looks like a real asymmetry between the two comparison paths** (row_hash path vs. PK path) but is explicitly out of scope to fix here — flag and preserve.
- **`aggregate_tolerance_pct` sum/min/max checks** (`quality_checks.py:90-104`) divide by `max(abs(source_value), 1.0)` — for a genuinely-near-zero source aggregate this floors the denominator at 1.0, which can mask a large *relative* drift when the absolute values are small. Not part of the row-level PASS/FAIL oracle (`quality_failures` only flips `is_match` at the table level, `main.py:482-483`), so lower priority, but worth flagging since it does gate `is_match`.

---

## C. Current scalability bottlenecks

Already fully catalogued in the audit (`docs/performance-audit-large-tables/README.md`, sections B–E) — not re-deriving here. Summary for reference against the architectures below:

1. Full `fetchall()` on every connector, both sides (memory + network root cause).
2. Python `for pk_val in all_pks` loop + `.loc` + `.apply(axis=1)` (CPU root cause).
3. `list[dict]` → `pd.DataFrame(rows)` double materialization (peak-memory moment).
4. Athena 1000-rows/page hard cap (network/latency, AWS-imposed, connector-specific).
5. `subprocess.run(timeout=900)` with no partial-result recovery (operational, not architectural).

---

## D. Candidate architectures — evaluated against §A/§B, not preselected

### A. Chunked/streaming Python comparison (`fetchmany()`, server-side cursors, Snowflake Arrow batches)
Fetches in bounded-size pages instead of one `fetchall()`. **Does not by itself change the comparison algorithm** — the PK-union/loop/hash logic (§A.6-A.15) still needs the *complete* key space before it can classify SOURCE_ONLY/TARGET_ONLY, so naive chunking only helps the fetch stage, not the compare stage, unless combined with an external sort/merge (see "Ordered synchronized chunks" below) or a hash-based partition scheme.
**Verdict: SAFE** for the fetch stage in isolation — reduces peak memory of stage 1 (B.1 in the audit) without touching comparison semantics at all, since it's purely an I/O strategy. **Does not solve** the CPU bottleneck (bottleneck #2) or the double-materialization bottleneck (#3) on its own.

### B. Key-range partitioning (numeric PK ranges, composite PKs, UUID/string keys, skewed keys, key-space gaps)
Splits the PK domain into ranges (`WHERE pk BETWEEN x AND y`), runs comparison per range, accumulates only per-range results. Preserves exact §A semantics **if and only if** every range boundary is applied identically to both `sourcequery` and `targetquery` (§B.1 invariant) and ranges are contiguous/exhaustive with no gaps at the boundaries (an off-by-one at a range edge silently drops or duplicates rows crossing that boundary — this is exactly the kind of asymmetric-filter bug `CLAUDE.md`'s "Consistency" dimension warns about).
Numeric/sequential PKs: straightforward (`WHERE pk >= n AND pk < n + chunk_size`). Composite PKs: requires a stable total ordering across all PK columns, harder to express as a single range predicate portably across Postgres/MSSQL/Athena/Snowflake SQL dialects — likely needs a computed row-number/rank window function per connector. UUID/string keys: no natural numeric range; requires either a hash-bucket scheme (`WHERE MOD(hash(pk), N) = i`) or an offset/order-by pagination (fragile under concurrent writes, and Athena has no native row-level cursor). Skewed keys (e.g. 90% of rows in one numeric decile): fixed-width ranges produce wildly uneven chunk sizes; needs pre-sampling (`APPROX_PERCENTILE`/histogram query) to build balanced ranges, which is extra pre-work per table, not free.
**Verdict: POTENTIALLY SAFE WITH VALIDATION** for numeric/sequential PK tables. **SEMANTICALLY RISKY** for composite/UUID/string PKs and skewed distributions without materially more engineering (balanced range discovery, boundary-exactness testing) than the numeric case — and it does nothing for PK-less (`row_hash`) tables, which have no key space to range over until a hash is already computed somewhere.

### C. Ordered synchronized chunks (`ORDER BY` PK independently on both sides, walk in lockstep)
Classic external merge-join pattern: both sides query `ORDER BY pk` (or `ORDER BY row_hash` for PK-less), fetched in bounded pages, and a merge-cursor walks both streams in lockstep comparing current keys — the same conceptual algorithm as `main.py`'s `sorted(set(...) | set(...))` (§A.6/A.14) but streamed instead of materialized.
This is the architecture that most directly generalizes the *existing* algorithm without changing its semantics: duplicate-PK groups still need to be buffered as a group (not a single row) before the multiset comparison in §A.14 can run, so the merge cursor must buffer by "all rows sharing the current key," not by a fixed page size — a page boundary that splits a duplicate-PK group in half would corrupt the multiset comparison. Requires the database to guarantee stable, repeatable ordering across multiple paginated `fetchmany()` calls on the *same* query execution (true for a single open cursor; NOT guaranteed if pagination re-runs the query per page without a stable `ORDER BY` + keyset pagination, since two different query executions of a query without an `ORDER BY` tiebreaker over a duplicate-key column can return that duplicate group in different relative order between calls — usually harmless here since §A.14 already sorts within a group, but the *grouping itself* — "have I seen all rows for this key yet" — depends on the ordering being stable and complete before moving on).
**Verdict: SAFE** *if* keyset pagination is implemented correctly (stable `ORDER BY <pk>, <tiebreaker>` and page boundaries never split a key group) — this is the natural incremental evolution of the current algorithm, preserves the exact multiset/duplicate/composite-PK/SOURCE_ONLY/TARGET_ONLY semantics of §A.6-15, and bounds memory to O(largest single duplicate-PK group) instead of O(N). Implementation complexity is real (keyset pagination + group-boundary buffering, per connector) but the semantic risk is low because it's the same algorithm, just streamed.

### D. Database-side hashing (row-level hash computed in SQL on each side, hash sets compared)
Compute `MD5(...)`/`HASH(...)` per row in the `sourcequery`/`targetquery` SQL itself, then compare hash sets — either in SQL (via E below) or by pulling only the (much smaller) `(pk, hash)` pairs into Python.
**This is exactly the case §5 of the user's brief calls out as not-to-assume.** See dedicated analysis in §F below — **short answer: NOT safely reproducible byte-for-byte across all four source connectors today**, because the current row_hash (§A.9) is computed *after* `canonicalize_frames` (JSON canonicalization + 2dp numeric rounding) in Python, and no single SQL dialect shared across Postgres/MSSQL/Athena/Snowflake reproduces that canonicalization identically. A SQL-side hash computed from raw column values would disagree with the current oracle on exactly the JSON/HStore/decimal-precision rows that `semantic_normalize.py` exists to handle correctly.
**Verdict: SEMANTICALLY RISKY as a direct replacement of the existing row_hash. POTENTIALLY SAFE WITH VALIDATION** as a *pre-filter*: a cheap SQL-side hash of raw (non-canonicalized) values can correctly identify rows that are **certainly identical** (hash matches → skip fetching that row's full content) while still requiring any hash-mismatch rows to be fetched and re-compared through the real canonicalization + `_cell_str` logic in Python — this preserves correctness because it only ever *narrows* what needs Python comparison, never *decides* FAIL/PASS itself for JSON/decimal columns. See hybrid architecture (G).

### E. Database-side set comparison / joins (`JOIN`, `EXCEPT`/`MINUS`, full outer join, anti-joins)
Cross-database `EXCEPT`/anti-join is not possible in a single query across four different source engines and Snowflake — there is no cross-database query engine here (FACT: each connector is a separate connection to a separate physical database; `main.py` never runs a single query spanning source+target). So "database-side set comparison" necessarily means: (1) stage/copy one side's data into the other's engine (see F, staging tables) and diff there, or (2) each side independently computes a set (e.g. of hashes or PKs) and Python does the *much smaller* set-difference.
Given (1) requires a cross-database data movement step of its own (which reintroduces network transfer, just relocated), and full row-level content diffing inside one engine after staging is architecturally the cleanest "true" database-side comparison — but only if the canonicalization problem (§F) is solved first, since staging raw values and diffing in SQL still needs the JSON/decimal canonicalization applied identically before the diff, and that canonicalization currently only exists as Python code.
**Verdict: POTENTIALLY SAFE WITH VALIDATION**, but staging-based full diff is the highest-complexity option here and its safety is entirely gated on solving §F first (canonicalization equivalence) — recommending this only for tables with no semi-structured/high-precision-decimal columns, where raw SQL comparison is already equivalent to the Python path.

### F. Staging/temporary tables
A `CREATE TEMP TABLE` (or Snowflake transient table) written from Python-canonicalized data, then diffed via E-style SQL — i.e. staging is a mechanism, not a standalone architecture; it only helps once you've decided *what* gets staged (raw or canonicalized values) and *where* the diff runs. Same gating as E.

### G. Hybrid architecture (database does large-scale matching, Python retrieves only mismatches/summaries)
Two-tier approach: **Tier 1 (SQL, cheap, high-confidence)** — compute row hashes of *raw* (non-canonicalized) values on both sides via the existing generated SQL, compare hash sets to produce three buckets: definitely-identical (hash match), source-only-key, target-only-key. **Tier 2 (Python, exact, only for what Tier 1 couldn't resolve)** — for hash-mismatched keys (which includes every row that has any JSON/HStore/decimal-precision drift a raw hash can't distinguish from real drift, plus every genuine data mismatch), fetch just those rows' full column values from both sides and run the *exact existing* `canonicalize_frames` + `_cell_str` + multiset comparison (§A.5, A.14) unchanged.
This is the only candidate that: (a) preserves 100% of existing semantics for the rows it resolves exactly in Python (because it uses the *unmodified* existing comparison code for anything ambiguous), (b) meaningfully reduces network transfer and Python memory for the (expected) large majority of rows that are truly identical, (c) doesn't require solving "reproduce Python canonicalization in SQL" (§F) because canonicalization only ever happens in Python, on the narrowed row set, exactly as today.
**Verdict: SAFE**, and the strongest candidate — see §G recommendation below. The one design detail that must be gotten right: the raw-value SQL hash must be **collision-conservative in the direction of false-mismatch, never false-match** across JSON/decimal representation differences — i.e. it's fine (safe) if the SQL hash disagrees on rows that are actually equal (they just fall through to the accurate Tier 2 path, costing extra work but never costing correctness); it would be **unsafe** if the SQL hash could ever agree on rows that are actually different post-canonicalization (a real mismatch silently marked "definitely identical" and never checked in Tier 2). Because JSON key-reordering and decimal-precision differences are exactly the cases that make raw string/byte hashes *disagree* on equal data (never agree on unequal data — hashing more input never spuriously collapses a real difference), this direction is safe by construction: worst case is more Tier-2 work, never a missed mismatch. This must still be verified per connector (see §K).

### H. PK-less / row_hash validation at scale
Falls out of G naturally: for a PK-less table, "the key" fed into Tier 1 is a hash-of-raw-values computed in SQL per row (not `main.py`'s current Python-side `row_hash`, which stays reserved for Tier 2's narrowed-row exact-match path). This is the **same tiering**, just with the SQL-side hash also serving as the join key instead of a real PK. §F's caution about NOT trusting a SQL hash as the *final* answer still applies — Tier 2 (unchanged Python row_hash + comparison, §A.9) remains the actual oracle for any row the two SQL hash sets disagree on.

### I. Athena-specific architecture
`get_query_results()`'s 1000-row/page ceiling (audit §D, item 4) is an AWS API limit, not something any Python-side redesign changes — confirmed **FACT** from `athena.py:61-86`, no `fetchmany`-equivalent exists for Athena's API. The only lever that removes this ceiling: **CTAS to S3** (`CREATE TABLE ... AS SELECT ... WHERE ...` writing Parquet/CSV to S3, then reading the S3 objects directly via boto3/pyarrow, bypassing `get_query_results` entirely) — this is a materially different code path per connector (Athena needs its own extraction strategy; Postgres/MSSQL/Snowflake do not have this ceiling and don't need it). **INFERENCE**: this makes Athena the one connector where "one architecture for every connector" (explicitly warned against in the brief) is not just allowed but necessary — Athena's Tier-1 hash query (G) should itself be a CTAS-to-S3, and the resulting S3 objects read in Python for whatever narrow Tier-2 fetch is needed, rather than ever calling `get_query_results` on a 200M-row result set.

---

## E. Architecture comparison matrix

| | Correctness risk | Memory | Network | DB CPU | Python CPU | Complexity | Portability | Duplicate PK | Composite PK | PK-less | JSON/decimal |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A. Chunked fetch only | SAFE | fetch-stage only ↓ | none | none | unchanged | Low | High (per-connector API differs) | unaffected | unaffected | unaffected | unaffected |
| B. Key-range partition | POTENTIALLY SAFE (numeric) / RISKY (UUID/skew) | ↓↓ per chunk | ↓ (if pushed to WHERE) | ↑ (range scans) | ↓ per chunk | Medium-High | Low (per-connector range syntax + skew handling) | needs group-boundary care | hard (needs total order) | **not applicable** (no key space) | unaffected |
| C. Ordered sync chunks | SAFE (if keyset pagination correct) | ↓↓↓ (bounded by largest dup-key group) | ↓ (streamed, same total bytes) | ↑ (ORDER BY cost) | ↓ (still Python compare, but streamed) | Medium-High | Medium (ORDER BY portable; keyset pagination per-dialect) | preserved exactly | preserved exactly | works (order by row_hash) | unaffected (canonicalization still runs in Python per chunk) |
| D. DB-side hash as final answer | **RISKY** (canonicalization gap) | ↓↓ | ↓↓ | ↑ | ↓↓ | Medium | Low (hash function differs per dialect) | breaks unless hash includes duplication count | needs concat order agreement | works as key, not as final compare | **fails** — raw hash disagrees with canonicalized comparison |
| E. Staging + DB diff | POTENTIALLY SAFE (gated on F) | ↓ (Python), ↑ (DB storage) | moved, not removed | ↑↑ | ↓↓↓ | High | Low (staging mechanics differ per DB) | needs explicit COUNT(*) check | fine once staged | fine once staged | still needs canonicalization pre-stage |
| G. Hybrid (hash pre-filter + exact Python fallback) | **SAFE** (by construction — see D.G) | ↓↓↓ (only mismatches materialize) | ↓↓↓ (only mismatches cross the wire in full) | ↑ (hash computation) | ↓↓ (only on narrowed set) | Medium-High | Medium (Tier 1 hash query per-connector; Tier 2 reuses existing code) | preserved exactly (Tier 2 unchanged) | preserved exactly (Tier 2 unchanged) | works (Tier 1 hash = the key) | preserved exactly (canonicalization untouched, runs only in Tier 2) |
| I. Athena CTAS-to-S3 | SAFE (extraction-only change) | ↓ (streamed from S3) | ↓↓ (bulk vs. paginated API) | n/a (Athena is the compute) | unchanged | Medium (Athena-only) | N/A — Athena-specific | unaffected | unaffected | unaffected | unaffected |

---

## F. Row_hash equivalence analysis (do NOT skip — this gates architecture D/H)

**Exact current semantics** (repeating §A.9 in the specific terms the brief asks for):

- **Concatenation semantics**: `"|".join(_v(c) for c in cols)` then UTF-8 encode then `hashlib.new(name, bytes).hexdigest()`. Delimiter is a bare `|`, no escaping.
- **Column ordering**: source-column order (or YAML-configured explicit order) — must match between the two `_hash_row` calls, which it does because it's literally the same `cols` list closed over by both calls (`main.py:314,322-323`).
- **NULL representation**: sentinel string `"<<NULL>>"`.
- **Type conversion**: `str(v)` for everything except `None`/NaN and `str` (which gets `.strip()`'d). No numeric formatting, no timestamp formatting, no Decimal-specific handling — this is the raw `repr`-adjacent string of whatever Python type the DB driver returned for that cell **after** `canonicalize_frames` already ran on it.
- **Escaping/collision risk**: none — a value containing a literal `|` is not escaped, so `("a|b","c")` and `("a","b|c")` produce identical concatenated strings and thus identical hashes despite being different tuples. This is a pre-existing collision risk in the *current oracle itself*, not something a redesign introduces — any redesign reproducing this hash inherits the same risk, and any redesign fixing it changes the oracle's behavior (out of scope per the brief's "treat current implementation as correctness reference").
- **JSON/JSONB/HStore/VARIANT canonicalization**: happens via `semantic_normalize.py` **before** the hash function ever sees the value (§A.5 runs at `main.py:256`, hash fallback at `main.py:298+`) — the hash is over the *canonicalized* string form, not the raw driver value.
- **Timestamp formatting**: whatever `str()` produces for the DB driver's returned Python type — e.g. psycopg2 typically returns `datetime.datetime`/`date` objects (`str()` → `"2026-09-22 14:30:00"` or similar, driver/locale dependent), pyodbc similarly, Snowflake's connector returns its own datetime objects, Athena returns everything as a **string already** (`athena.py:80`: every cell is `VarCharValue`, i.e. `str` from the source). This means **the string form of a timestamp fed into the hash already varies by connector today**, before any redesign — Athena hashes the literal text Athena returned, Postgres hashes `str(datetime_object)`. If these two differ in formatting for what is semantically the same instant, the *current* oracle already reports a false mismatch — a redesign must reproduce this quirk, not "fix" it.
- **Numeric formatting**: same `str()` rule — a `float`/`Decimal`/`int` all stringify differently (`5.0` vs `5` vs `Decimal('5.00')`) and §A.9.3's flagged ambiguity (an int/float type mismatch between drivers hashes differently) is a real, pre-existing gap between the row_hash path and the PK-based `_cell_str` path's more careful 2dp float formatting.
- **Binary values**: not exercised anywhere in the traced code — no binary/bytea column handling visible in `_hash_row` or `_cell_str`; `str()` on a Python `bytes` object would produce its `b'...'` repr, which is very unlikely to match across connectors. **Flagged as untested/unknown**, not evaluated further here.
- **Cross-database differences**: confirmed structurally different at the driver level for at least timestamps (see above) — this means the *existing* row_hash oracle is not perfectly connector-symmetric even today for timestamp-containing tables without a PK; this is inherited risk, not new risk.

**Conclusion, directly answering the brief's question**: reproducing this exact hash in SQL is **NOT safely achievable** for any connector, because the hash is computed downstream of Python-only canonicalization logic (`semantic_normalize.py`) that has no SQL equivalent by design (the module's own docstring explains *why* it was deliberately moved out of SQL — two engines could not be made to agree). Any SQL-side `MD5(...)` over raw column values will disagree with the current Python row_hash on every row where canonicalization actually did something (JSON key reordering, array reordering, decimal rounding, numeric-string precision normalization) — which are precisely the rows most likely to represent real (or real-looking) migration drift.

**Design consequence**: SQL-side hashing can only be used as a **narrowing pre-filter** (architecture G), never as the final PASS/FAIL decision, unless a table is known in advance to have no semi-structured/high-precision-decimal/mixed-numeric-type columns — and even then, the timestamp-formatting and int/float-type asymmetries above mean a byte-identical SQL hash across four different SQL dialects is not a low-effort guarantee even for "simple" tables. Treat full row_hash SQL-side equivalence as **not guaranteed** and design around narrowing, not replacement.

---

## G. Recommended target architecture (PROPOSAL)

**Hybrid, two-tier, per the "G" candidate in §D, with Athena getting its own Tier-1 extraction mechanism (§D.I):**

- **Tier 1 — SQL-side narrowing (new).** For each table: compute a raw-value hash per row (or per configured `row_hash.columns` / configured PK) in the existing generated `sourcequery`/`targetquery` SQL (extends, doesn't replace, the existing SQL generation — same Fivetran/exclusion/filter logic stays baked in exactly as today, §B.1). Pull back only `(key, hash)` pairs — a fraction of the table's width — to Python, compare hash sets there (this part *can* be a vectorized pandas set-diff or even a SQL-side `EXCEPT` between two `(key,hash)` result sets if staged, since this comparison has no canonicalization dependency — it's comparing opaque hash strings, not values). Classifies every key into: `KEY_MISSING_FROM_SOURCE`, `KEY_MISSING_FROM_TARGET`, `HASH_MATCH` (provisionally identical), `HASH_MISMATCH` (needs Tier 2).
- **Tier 2 — exact Python comparison (existing code, unmodified).** Re-fetch full rows for `HASH_MISMATCH` keys only (plus the trivially-classified `SOURCE_ONLY`/`TARGET_ONLY` keys, which need no re-fetch — Tier 1 already answered them), run the **existing, unmodified** `canonicalize_frames` + PK-indexed multiset comparison (§A.5-A.16) on this narrowed set, exactly as `main.py` does today. This is where every invariant in §B is satisfied by construction, because it's literally the same code, just fed a smaller input.
- **`HASH_MATCH` rows are recorded as PASS directly** (no Tier 2 re-fetch) **only after the differential-testing phase (§K) proves, per table/connector, that Tier 1's raw hash never disagrees with a Tier 2 decision on rows that are actually equal after canonicalization** — until that's proven per table category, treat `HASH_MATCH` as "high-confidence PASS, spot-checked," not "trusted PASS," during rollout (§O).

This satisfies the brief's §8 requirement ("never require entire source + entire target + entire result to coexist in memory") because at steady state only `(key,hash)` pairs for the *whole* table and full rows for the *mismatch subset* are ever materialized — both are expected to be orders of magnitude smaller than the full table for a healthy migration (few actual mismatches) and degrade gracefully (worst case = every row mismatches = same cost as today, no worse) for an unhealthy one.

**Why not a pure vectorized merge (candidate C-adjacent) instead?** A `pd.merge(how="outer", indicator=True)` replacing the `for pk_val in all_pks` loop is a legitimate, lower-risk, *smaller* change that fixes bottleneck #2 (CPU) without touching bottlenecks #1/#3 (memory/materialization) — it's a valid **incremental step**, but it does not reduce network transfer or remove the "everything fetched fully" constraint, so it doesn't reach the 200-300M target on its own. **PROPOSAL**: do this vectorized-merge rewrite as a **fast-follow inside Tier 2**, once Tier 2 is only ever operating on the narrowed mismatch set — it becomes strictly easier to get right (smaller data, same duplicate-PK-group-aware multiset logic still required) once it's not also fighting the full-table CPU/memory problem.

---

## H. Connector-specific strategy

| Connector | Tier 1 hash query mechanism | Notes |
|---|---|---|
| Postgres | `MD5(...)` over raw concatenated columns, `WHERE` clause = existing `source_filter`, server-side cursor (`DECLARE ... CURSOR`) for bulk `(key,hash)` pull | Standard SQL, no pagination ceiling |
| Redshift ("TradeShift") | Same as Postgres (reuses the connector, `Project/db/factory.py`) — but Redshift's MPP architecture makes a leader-node-side `MD5` over a huge scan potentially slower than Postgres; benchmark separately (§I) rather than assuming Postgres numbers transfer | Same connector class, different execution profile |
| MSSQL (SiteLink) | `HASHBYTES('MD5', ...)` (T-SQL's equivalent, different function name and argument type rules — takes a single `varbinary`, so concatenation must be `CONCAT(...)` cast appropriately) | Different SQL syntax from Postgres — cannot literally reuse the same query template, needs its own builder |
| Snowflake | `MD5(...)`/`HASH(...)` (Snowflake has both — `HASH()` is Snowflake-proprietary and faster but not comparable to MD5 elsewhere; use `MD5` for any cross-side comparability, `fetch_pandas_batches()`/Arrow for the `(key,hash)` pull instead of `fetchall()`) | Already supports the batching primitive the audit flagged as unused (`snowflake.py:48-49`) |
| Athena | **No Tier-1 SQL hash query directly via `get_query_results`** — CTAS the hash query to S3 (Parquet), read the `(key,hash)` pairs from S3 objects directly (boto3 + pyarrow), bypassing the 1000-row API pagination entirely (§D.I) | The one connector needing a genuinely different extraction mechanism, not just a different hash SQL function |

**Common requirement across all four**: the Tier-1 hash SQL must be generated by extending the *same* `src/generated_queries/sql_query_generator.py`/`ai_sql_generator.py` code path that already bakes in Fivetran/exclusion/filter logic (§B.1) — never a parallel, independently-maintained query builder, per `CLAUDE.md`'s "no redundant implementations" ground rule.

---

## I. PK / composite PK / PK-less strategy

- **Single-column PK**: Tier 1 key = that column, hashed row content alongside it. Straightforward.
- **Composite PK**: Tier 1 key = concatenation of PK columns in a stable, documented order (must match `main.py`'s current `MultiIndex` column order, §A.7) — hash still computed over the full row content (all shared columns), key used only for the SOURCE_ONLY/TARGET_ONLY classification and for narrowing which full rows Tier 2 re-fetches.
- **PK-less (`row_hash`)**: Tier 1 key **is** the raw-value hash itself (no separate identity concept) — `HASH_MISMATCH` here doesn't mean "found a differing row for a known key," it means "no row-hash match found on the other side within tolerance," which for a PK-less table is structurally identical to how `main.py` already treats row_hash today (§A.9) — the redesign doesn't add a new case, it just moves *where* that hash is computed (SQL, cheaply, for narrowing) vs. *where* it's computed today (Python, expensively, on the full table). The final decision must still go through Tier 2's real `row_hash` (Python, canonicalized, §A.9) for any row that Tier 1 couldn't confidently match, exactly per §F's conclusion.
- **Duplicate PKs (any of the above)**: Tier 1 classification must be duplicate-count-aware — a naive "does this key exist on both sides" check would call a 2-vs-1 duplicate count `HASH_MATCH` if one of the two source rows happens to match the single target row's hash. **PROPOSAL**: Tier 1 must group by key and compare *multisets of hashes per key*, not just per-row hash membership, to correctly flag a duplicate-count mismatch as needing Tier 2 (which is where the real, existing, correct duplicate-handling logic in §A.15 lives).

---

## J. Proposed end-to-end processing flow (200–300M rows)

1. **Query generation** (unchanged) — existing `sql_query_generator.py`/`ai_sql_generator.py` produce `sourcequery`/`targetquery` with Fivetran/exclusion/filter logic baked in, **plus** a new Tier-1 hash-query variant of the same SELECT (same WHERE, same projection minus non-hashed columns, plus a computed hash column).
2. **Tier 1 extraction** — run the hash-query variant on both sides via a **streamed** fetch (server-side cursor / `fetchmany()` / Snowflake Arrow batches / Athena CTAS-to-S3 per §H), producing `(key, hash)` pairs only. Never materialize full-width rows in this step.
3. **Tier 1 classification** — stream-merge or vectorized-set-diff the two `(key,hash)` streams (external sort/merge per §D.C if key space doesn't fit in memory as a set; a plain in-memory set of `(key,hash)` pairs is far smaller than the full table and may fit even at 300M rows depending on key/hash width — measure, don't assume, per §L) into: `SOURCE_ONLY` keys, `TARGET_ONLY` keys, `HASH_MISMATCH` keys (includes duplicate-count mismatches per §I), `HASH_MATCH` keys.
4. **Tier 2 targeted re-fetch** — for `HASH_MISMATCH` keys (expected small set for a healthy migration) plus the trivial `SOURCE_ONLY`/`TARGET_ONLY` keys (no re-fetch needed, already classified), fetch full-width rows for just those keys from both sides (`WHERE pk IN (...)` batched to a safe IN-list size, or a joined temp-key-list table for very large mismatch sets).
5. **Tier 2 comparison** — run the **existing unmodified** `canonicalize_frames` + PK-multiset comparison (§A.5-15) on this narrowed frame. Output: PASS/FAIL/SOURCE_ONLY/TARGET_ONLY per key, identical shape to today's `_rec()` output.
6. **Result assembly, chunked** — write `HASH_MATCH` keys as PASS rows directly (with `__source`/`__target` populated from Tier 1's cheap read if needed for CSV completeness — a bounded incremental fetch, not a second full-table pass) and Tier 2's classified rows, appended to the result CSV in batches (`to_csv(mode="a")`) rather than one `pd.DataFrame(rows)` for the whole table (audit bottleneck #3).
7. **Failed-rows CSV** — write incrementally alongside step 6, filtered per batch, not as one final boolean-mask copy over the whole result.
8. **Summary generation** — unchanged shape, computed from running counters (source_rows, target_rows, n_fail, n_source_only, n_target_only) accumulated across all Tier 1 + Tier 2 batches, not from `len(result_df)` on one giant frame.
9. **Progress tracking** — PROPOSAL: emit a periodic log line (or `validation_audit.jsonl` interim record) after each Tier 1 batch and each Tier 2 batch — batch count / keys classified so far — since a 200-300M row run may take long enough that "still running vs. hung" needs a signal beyond total silence.
10. **Failure/retry**: Tier 1 batch failure (connection drop mid-stream) → retry that batch's key range only, not the whole table, since Tier 1 batches are naturally partitioned by fetch order/range. Tier 2 batch failure → retry that specific key subset. Both tiers should write nothing to the final result CSV until a batch fully succeeds (append-on-success, not append-then-hope), so a killed/retried run doesn't produce a partially-written, ambiguous CSV row.
11. **Cleanup**: any staged/temp `(key,hash)` intermediate structures (if implemented as actual DB temp tables per §D.E/F rather than in-Python streams) must be dropped in a `finally`, matching the existing `finally: conn.close()` pattern already used in every `Project/db/*.py` connector.
12. **Memory bounds**: PROPOSAL — cap in-flight Tier 2 mismatch batch size (e.g. 100K keys' worth of full rows at a time) so Tier 2 never re-introduces the full-table-in-memory problem even if a table turns out to have an unexpectedly large mismatch count.
13. **DB connection limits**: Tier 1 and Tier 2 can reuse a single long-lived connection per side per table (server-side cursor) rather than the current per-`execute_query()` connect/close pattern (`main.py:231-242` opens a fresh connection object each call) — reduces connection churn but is a minor optimization, not a correctness concern.
14. **CSV/result-file strategy**: same file naming/location convention as today (`{table}_{validation_name}_result_{run_id}.csv` / `_failed_`), just written incrementally — no change to what `runner.py`'s globbing (`runner.py:114-115`) expects.

---

## K. Differential correctness-testing strategy

**Reference/oracle**: current `Project/main.py`, run unmodified, on a controlled dataset.
**Candidate**: the Tier 1 + Tier 2 hybrid, run against the identical dataset and identical YAML config (same `sourcequery`/`targetquery`, same exclusions, same filters — the *only* difference is execution strategy).

**Required test dataset composition** (every case named in the brief, mapped to the exact code path it exercises):

| Injected case | Exercises |
|---|---|
| Identical rows (PK-matched, all columns equal) | Tier 1 `HASH_MATCH` path; must remain PASS in candidate |
| Changed non-PK value | Must land in Tier 1 `HASH_MISMATCH` → Tier 2 FAIL, identical to oracle |
| Missing source row (target-only) | Tier 1 `TARGET_ONLY` classification == oracle's `_to_df` empty-source path (§A.12) |
| Extra target row (source-only) | Symmetric, §A.13 |
| NULL vs. non-NULL | Must produce the same `"<<NULL>>"` sentinel behavior in both Tier 1's hash input and Tier 2's exact comparison — verify Tier 1's raw hash doesn't accidentally treat NULL and empty-string as the same input in a way the oracle doesn't |
| NULL vs. NULL (both sides) | Must remain PASS — both tiers |
| Duplicate PKs (2 identical rows, one side vs. other) | The case most likely to break a naive Tier 1 (§I) — must be forced into Tier 2 by construction, and Tier 2's existing multiset logic (§A.14-15) must reproduce the oracle exactly |
| Composite PKs | Tier 1 key concatenation order must match §A.7's MultiIndex order |
| PK-less tables | Tier 1 hash *is* the key (§I) — verify classification matches oracle's row_hash-as-PK treatment |
| Row_hash differences (a JSON column reordered, semantically equal) | **The critical case from §F** — Tier 1's raw hash MUST disagree here (safe direction) and route to Tier 2, where the *existing* `canonicalize_frames` correctly reports PASS. A candidate that reports this as Tier 1 HASH_MISMATCH → Tier 2 PASS is correct; a candidate that ever reports Tier 1 HASH_MATCH here by coincidence must still be checked against Tier 2 at least during validation-phase rollout (§G's caveat) |
| Excluded columns | Tier 1 hash query must select the exact same post-exclusion column set as today's `sourcequery` (§B.1) — verify by diffing the generated SQL, not just the result |
| Semantic normalization cases (hstore, jsonb, decimal precision, numeric-string precision) | Same as the row_hash-differences case above — must route through Tier 2 correctly |
| Fivetran filters | Verify the Tier 1 hash query includes `_FIVETRAN_ACTIVE = TRUE` identically to the existing generated target query (§A.3) — a missed filter here is exactly the "asymmetric filter → false mismatch" risk `CLAUDE.md` warns about |
| Large/wide rows | Verify Tier 1 hash computation and transfer stays bounded even when individual rows are wide (e.g. a large JSON blob column) — this is the case most likely to reveal a "the hash query itself still selects the wide column" mistake, since a lazy Tier-1 implementation might accidentally `SELECT *` for hashing instead of hashing without materializing the full column client-side |

**What outputs must match, exactly, between oracle and candidate**:
- Row-by-row: `status` per `row_key` (PASS/FAIL/SOURCE_ONLY/TARGET_ONLY) — 100% identical set of `(row_key, status)` pairs.
- Row-by-row: every `{col}__source`/`{col}__target` value for every FAIL/SOURCE_ONLY/TARGET_ONLY row (PASS rows' cell values matter less if `status` matches, but should still be spot-checked for the CSV-completeness requirement in §J.6).
- Summary counts: `source_rows`, `target_rows`, total FAIL/PASS/SOURCE_ONLY/TARGET_ONLY counts — identical.
- `quality_failures`/`grain_failures` gating of table-level `is_match` — identical (Tier 1/2 split shouldn't touch `run_quality_checks`/`validate_expected_grain` at all; they should keep running against whatever frame is available, likely the Tier-2-narrowed frame plus Tier-1 aggregate stats — **PROPOSAL, flagged as an open question in §P**, since these checks currently assume access to the *full* frame for null-rate/distinct-count/aggregate-sum checks, and a hybrid architecture that never materializes the full frame in Python needs its own strategy for these, likely computed via Tier-1-style SQL aggregates rather than pandas — this is real, unresolved scope, not covered by "just narrow to mismatches").

**Pass bar**: zero differences in row-level `(row_key, status)` set and in table-level summary counts, across every injected case above, before any production rollout phase (§O) proceeds past Phase 4.

---

## L. Benchmark plan

Ladder: 1M / 10M / 50M / 100M / 200M / 300M rows, run against **both** oracle and candidate at each rung (where the oracle can still complete at all — expect it to fail or become impractical well before 200M, which is itself a data point).

Per rung, measure (not estimate):
- Wall-clock runtime (fetch phase, compare phase, write phase — separately timed).
- Peak Python RSS (e.g. `resource.getrusage` or an external memory profiler sampled through the run, not just observed after the fact).
- Database-side CPU/warehouse credits consumed (Snowflake query history; Postgres/MSSQL server-side CPU if observable; Athena data-scanned bytes as a proxy).
- Python CPU time (distinct from wall-clock, to isolate GIL-bound stages).
- Network bytes transferred (packet capture or connector-level byte counters if available — this is the metric the audit explicitly says nothing pushes down on today, so it needs a real before/after number, not an assumption).
- Rows/sec, end to end.
- Mismatch count found (must match between oracle and candidate at ranks small enough for the oracle to still run).
- Result-file size on disk.
- Number of database queries issued (oracle: 2 per table; candidate: 2 + N batches per tier — quantify N).
- Number of chunks/partitions used by the candidate.
- Failure/retry behavior under an injected mid-run connection drop, at each rung.

**Do not publish or rely on any specific number (e.g. "candidate is Nx faster") until this ladder has actually been run** — nothing in this document should be read as a performance claim; it's the plan to produce one.

---

## M. Failure/recovery strategy

- **Tier 1 batch failure**: retry the specific fetch batch/range (bounded blast radius — not the whole table). Idempotent by construction since Tier 1 only produces classification data, not final output.
- **Tier 2 batch failure**: retry the specific key subset being re-fetched. Also idempotent — Tier 2 output for a given key subset doesn't depend on any other subset.
- **Whole-table failure** (e.g. DB connection lost entirely): existing `main.py` exception handling (`main.py:526-556`, catches `pyodbc.Error`/`psycopg2.Error`/generic `Exception`, writes an `ERROR` summary row, increments `failure_count`, continues to the next table) must be preserved at the table level — a table-level crash still must not silently disappear from the summary (per the existing `_write_error_summary` mechanism, §A already relies on this).
- **Partial results on subprocess timeout**: per the audit's item 5 — surface whatever result CSVs already landed on disk (per-batch incremental writes from §J.6 make this materially better than today, since a killed run at 200M rows would today have written *nothing* — no `to_csv` call happens until the entire in-memory comparison finishes — whereas the batched design would have real partial CSVs to show).
- **Cleanup**: any staged/temp resources dropped in `finally`, matching existing connector patterns.

---

## N. Memory and network safety model

- **Hard target**: at no point does "full source table + full target table + full result" coexist in Python memory (brief §8's explicit requirement) — satisfied by construction in the Tier 1/2 design, since Tier 1 only ever holds `(key,hash)` pairs (bounded, much smaller than full rows) and Tier 2 only ever holds the mismatch subset (bounded by migration health, worst case = full table, same as today, never worse).
- **Network target**: full-width data crosses the wire only for the mismatch subset + the trivially-classified SOURCE_ONLY/TARGET_ONLY keys — this is the one metric candidate G actually improves over "chunk the fetch" alone (candidate A), since A still moves 100% of bytes, just not all at once.
- **Bound on Tier 1 in-memory set size**: `(key,hash)` pairs for the full key space, times 2 sides — needs a real measurement (§L) of whether this fits comfortably in memory at 300M rows or itself needs streaming/external-merge (§D.C) instead of an in-memory `set()`. **PROPOSAL, not yet decided**: default to an external sort-merge (candidate C, applied to the Tier 1 hash stream specifically) rather than assuming an in-memory Python set of 300M-600M `(key,hash)` tuples is safe — that alone could be tens of GB depending on key/hash width, echoing the exact concern the audit raised about the *current* PK-union set (audit §B, "PK union" row).

---

## O. Incremental rollout plan

Following the brief's phases exactly:

- **Phase 1**: `Project/main.py` remains untouched, remains the reference/oracle, remains the only thing running in production. No behavior change ships in this phase.
- **Phase 2**: implement the Tier 1/2 hybrid behind a feature flag (e.g. an opt-in CLI arg or YAML `validation_plan.execution_strategy: hybrid_v1`, defaulting to today's behavior) — this is a **new, additive code path**, not a modification of `main.py`'s existing logic, consistent with `CLAUDE.md`'s "verify before asserting"/"no redundant implementation" guidance meaning *don't duplicate the filter/exclusion/canonicalization logic*, while the *execution strategy* around it is legitimately new.
- **Phase 3**: run oracle and candidate side-by-side against the §K differential dataset (synthetic, controlled, small enough for the oracle to complete quickly) — every injected case, every connector.
- **Phase 4**: compare row-level and summary results per §K's pass bar — zero tolerance for divergence.
- **Phase 5**: benchmark ladder (§L) on progressively larger **real or realistic** datasets, oracle running only up to whatever size it can still complete in reasonable time, candidate running the full ladder.
- **Phase 6**: only after Phase 5 produces real numbers (not estimates) showing the candidate meets the 200-300M target with resource usage within acceptable bounds, consider flipping the default for large tables — and even then, PROPOSAL: keep the oracle path available and default for small/medium tables where its simplicity is a feature, not a liability, and reserve the hybrid path for tables above a size threshold determined by the benchmark data, not an arbitrary guess.

---

## P. Risks and unresolved questions

1. **Quality-checks (`run_quality_checks`/`validate_expected_grain`) currently assume a fully-materialized frame** (`quality_checks.py:45-155`, pandas `.isna()`, `.nunique()`, `pd.to_numeric().sum()`, `.duplicated()` all run over the complete `source_df`/`target_df`). The hybrid architecture has no full frame to run these against without re-introducing the exact memory problem being solved. **Partially resolved (see §Q) for `validate_expected_grain` only, in the single-column-PK case** — `run_quality_checks`'s null-rate/distinct-count/aggregate/sample-hash checks remain unresolved and unimplemented for hybrid_v1; those still need their own SQL-side aggregate equivalents (COUNT/NULL-rate/SUM pushed to each database) computed alongside Tier 1, not a Python-side pass over a frame that no longer fully exists. This needs its own design pass before Phase 2 can claim full parity, since `quality_failures`/`grain_failures` directly gate `is_match` (`main.py:482-483`).
2. **Binary/bytea column hashing behavior is untested and unknown** in the current oracle (§F) — a redesign inherits this gap rather than resolving it; if any in-scope table has a binary column, this needs dedicated investigation before it can be called "preserved," because there's nothing in the current code to preserve *correctly*, only to preserve *identically*.
3. **Cross-connector timestamp string-formatting asymmetry already exists in the current oracle's row_hash path** (§F) — this is a pre-existing correctness gap in the reference/oracle itself, not something the redesign creates, but it means "byte-identical to the oracle" for timestamp-heavy PK-less tables is reproducing a known-imperfect baseline, not a clean target. Worth surfacing to the user as a separate finding, independent of this scalability work.
4. **In-memory Tier 1 `(key,hash)` set size at 300M rows is not yet measured** (§N) — the design defaults to "assume it needs external merge, not an in-memory set" but this should be confirmed with real numbers before committing engineering effort to the more complex external-merge path if it turns out an in-memory set is actually fine (e.g. if keys are small integers and hashes are truncated to 8 bytes, 300M×2×~24 bytes ≈ 14GB, which may or may not be acceptable depending on the validator host's actual RAM — unknown, not stated anywhere in the codebase or this investigation).
5. **`HASHBYTES` in MSSQL has input-size and type constraints** (single `varbinary(8000)` input pre-2016-ish versions) that may require chunked/multi-call hashing for very wide rows — needs a connector-specific spike, not assumed to "just work" by analogy to Postgres `MD5(text)`.
6. **Athena CTAS-to-S3 introduces new AWS cost/permission surface** (S3 write access, CTAS table cleanup, potential Glue catalog pollution if not using `WITH (external_location=..., format='PARQUET')` correctly) — this is a real infrastructure change, not just a code change, and needs its own sign-off path separate from the Python architecture decision.
7. **Duplicate-PK multiset hashing in Tier 1 (§I) is a new algorithm not present anywhere in the current codebase** — it must be designed and tested as carefully as the row_hash equivalence work in §F, since getting it wrong in the "unsafe" direction (treating a duplicate-count mismatch as HASH_MATCH) would be a silent correctness regression, not just a performance one.
8. **Feature-flag default and size-threshold for switching execution strategy** (§O Phase 6) is explicitly left undecided pending real benchmark data — resist the temptation to pick a round number (e.g. "10M rows") without evidence.

---

*Design investigation performed 2026-09-22 against branch `version1.4`, building on the same-day performance audit. No files modified. No implementation to proceed until explicitly approved.*

---

## Q. Implemented: `validate_expected_grain` parity for hybrid_v1 (single-column PK only)

**Status:** Implemented and tested. Scope: `expected_grain`/`grain_columns` only — §P.1's null-rate/distinct-count/aggregate/sample-hash gap is still open and out of scope for this change.

### Q.1 What was built

`Project/tiered_runner.py` gained two module-private functions:

- `_grain_duplicate_count(hash_multimap)` — sums the length of every hash list in the Tier-1 `{key: [hash, ...]}` multimap (§A of this doc / `_collect_hash_multimap`) where that list has more than one entry. Because the multimap already carries one list entry per *physical* row for a key (not per distinct hash), this is an exact structural match for `quality_checks.validate_expected_grain`'s `frame.duplicated(columns, keep=False).sum()` — `keep=False` marks every row in a duplicate group, and so does this: a key with 3 physical rows contributes 3, not 2.
- `_validate_expected_grain_hybrid(validation_config, pk_col, src_hash, tgt_hash)` — the hybrid_v1 entry point, called from `run_table_hybrid` once Tier 1's `src_hash`/`tgt_hash` multimaps exist and only on the non-PK-less path (see Q.3):
  1. If `validation_config["expected_grain"]` isn't `"one_row_per_key"`/`"one_row_per_driving_key"`, returns `[]` immediately — identical short-circuit to the oracle, so tables without this YAML key are completely unaffected.
  2. Resolves `grain_columns` (falling back to `pksourcecolumn`, same precedence as the oracle) and requires it to resolve to exactly `[pk_col]` — the same single column already serving as the Tier-1/Tier-2 key. If it resolves to anything else, raises `RuntimeError` naming the unsupported column(s) rather than attempting a derivation the multimap can't support.
  3. Otherwise, returns the same failure-dict shape as the oracle (`check`, `side`, `grain_columns`, `duplicate_rows`, `detail`) for each side whose duplicate count is nonzero.

`run_table_hybrid` calls this right after the PK-less early-return block (so PK-less tables never reach it — see Q.3), logs each failure the same way the non-hybrid path does, folds a nonempty `grain_failures` into `is_match = False` (mirroring `main.py`'s `if quality_failures or grain_failures: is_match = False` for the non-hybrid path), and returns `grain_failures` in its result dict.

`Project/main.py`'s hybrid dispatch branch now reads `grain_failures = hybrid_result.get("grain_failures", [])` instead of hardcoding `grain_failures = []`, so the existing `quality_failures + grain_failures` audit-log field (`main.py`'s `append_validation_audit` call) is populated for hybrid_v1 exactly as it already is for the non-hybrid path. `quality_failures` itself stays hardcoded to `[]` for hybrid_v1 — the null-rate/distinct-count/aggregate/sample-hash checks are unimplemented here, per scope.

### Q.2 Why the multimap is sufficient (no full frame needed)

The oracle's `.duplicated(columns, keep=False).sum()` only needs, per grain key: *how many physical rows share it*. It never inspects the row content itself for that computation. Tier 1's `_collect_hash_multimap` already computes exactly that as a side effect of collecting `(key, hash)` pairs for HASH_MATCH/HASH_MISMATCH classification (§A above) — no additional query, no additional fetch, no full-frame materialization. This is why the parity is derivable at all without re-opening §P.1's harder problem (null-rate/distinct/aggregate genuinely need column *values*, which Tier 1 deliberately never carries).

### Q.3 Deliberate non-goals (per instruction, not oversight)

- **PK-less tables**: untouched. The PK-less early-return block in `run_table_hybrid` (§A of this doc) still returns before grain-check code runs, so `grain_failures` is simply absent from its result dict (`main.py`'s `.get("grain_failures", [])` defaults to `[]`). No refusal, no computation — this path's existing behavior is bit-for-bit unchanged.
- **Composite PKs**: unreachable — `run_table_hybrid` already raises `NotImplementedError` for composite PKs before any grain logic runs (pre-existing behavior, §A).
- **`grain_columns` naming a non-PK column**: explicit `RuntimeError` refusal (Q.1.2), not a new implementation. The user-facing message tells the table owner to either point `grain_columns` at the PK or drop `execution_strategy: hybrid_v1` for that table.
- **null-rate, distinct-count, sample-hash, aggregate checks**: not implemented for hybrid_v1 in this change — remain exactly as before (`quality_failures` stays `[]`). Still tracked as open in §P.1.
- **`quality_checks.py` and the non-hybrid path in `main.py`**: byte-for-byte unmodified.

### Q.4 Tests

`Project/test_tiered_runner.py`, run via `python -m pytest Project/test_tiered_runner.py -q -k expected_grain`:

| Test | Covers |
|---|---|
| `test_expected_grain_no_duplicates_is_zero` | No duplicate keys → count 0 |
| `test_expected_grain_one_key_three_times_is_three` | One key, 3 physical rows → count 3 (not 2) |
| `test_expected_grain_multiple_groups_matches_oracle` | Two duplicate groups (3 + 2) plus one non-duplicate key → hybrid helper's count matches `quality_checks.validate_expected_grain`'s count exactly (5) |
| `test_expected_grain_source_target_duplicate_mismatch_fails_like_oracle` | End-to-end `run_table_hybrid` run where source has a 3x-duplicated key absent from target — asserts `is_match is False` and `grain_failures` matches the oracle's side/`duplicate_rows` exactly |
| `test_expected_grain_non_pk_grain_columns_refuses` | `grain_columns` pointing at a non-PK column raises `RuntimeError` naming the column |
| `test_expected_grain_absent_is_unchanged` | No `expected_grain` key → returns `[]`, no exception, even if `grain_columns` would otherwise be unsupported (gate short-circuits first) |

All 6 pass. Full existing suite (`test_tiered_runner.py` + `test_hybrid_dispatch.py`, 16 tests) still passes — no regression in the untouched PK-less/composite-PK/HASH_MATCH-batching/transformation-spec behavior.
