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

---

## R. Investigation: `run_quality_checks` parity for hybrid_v1 (design-only — nothing implemented)

**Status:** Design-only, per explicit instruction. No code changed in this section. Scope: the four checks inside `run_quality_checks` (`quality_checks.py:26-122`) — `null_rate`, `distinct_count`, `sum`/`min`/`max` ("aggregate"), `sample_hash` — called from `main.py:378` in the **non-hybrid** path only, on the same `source_df`/`target_df` used for the row-level comparison. `validate_expected_grain` is out of scope here — that's §Q, already implemented.

**Method note:** every claim below is traced to a specific file:line, not inferred from a check's name. Several claims here overturn an assumption a from-memory analysis would make — see R.1.

### R.0 What actually gates each check today (`quality_checks.py:26-44`)

**FACT** — `run_quality_checks` returns `[]` immediately if `config["quality_checks"]` is missing, not a dict, empty, or has `enabled: False` (`quality_checks.py:32-38`). Otherwise, for each of `null_rate_tolerance_pct` / `distinct_count_tolerance` / `aggregate_tolerance_pct`, the gate is `_as_float(checks.get(key)) >= 0` — and `_as_float(None)` (key simply absent) returns `0.0`, which **is** `>= 0`. **Consequence, easy to miss:** a table configured with only `quality_checks: {enabled: true}` and no tolerance keys at all silently runs `null_rate`, `distinct_count`, and `sum`/`min`/`max` checks at **0% tolerance** (exact match required) — three of the four checks are "on by default" the moment the block exists, not opt-in per check. Only `sample_hash` is a true opt-in (`sample_percent > 0` required, and `_as_float(None)` = `0.0` fails that). Any hybrid_v1 design must replicate this exact activation logic, including the default-0.0-means-active trap, or a table that silently relied on it will silently stop being checked.

### R.1 Cross-cutting FACT that changes every check below: NULL frequently never reaches Python as NULL

Traced `main.py:378` backward through what actually populates `source_df`/`target_df`:

- `src/rules/base_rules.py` — **every one of the 10 base rules** wraps its column expression as `COALESCE(CAST(<col> AS TEXT/STRING/VARCHAR(MAX)), '<<NULL>>')` (`base_rules.py:120,125,130,134,708,712,716,720`, and the per-type classes at 225-654 restate the same rule in their docstrings). This same `rule.apply_source`/`apply_snowflake` machinery is used both for the row_hash columns (`sql_query_generator.py:304-305`) **and** the main `data_validation` comparison columns (`sql_query_generator.py:871,900`, the "③/④ normalised" SELECT that `yaml_config_writer.py` writes as `sourcequery`/`targetquery`).
- `semantic_normalize.py`'s own docstring already says this out loud: *"SQL NULL — becomes `'<<NULL>>'` via the COALESCE wrapper in SQL; never reaches this module."* (`semantic_normalize.py:40-41`).

**Consequence:** for any table whose YAML was generated by `src/generated_queries/sql_query_generator.py` + `yaml_config_writer.py` (the pipeline behind `run_with_plan()`), a SQL `NULL` in that column arrives at `main.py:378`'s `run_quality_checks` call **as the literal 9-character string `"<<NULL>>"`, not as Python `None`/`NaN`**. `pandas.Series.isna()` does not recognize that string as null. So **the `null_rate` check, as currently wired for generator-produced YAMLs, measures the rate of the literal string `"<<NULL>>"`, which happens to equal the real NULL rate only because nothing else produces that string — but `isna()` itself contributes nothing; a plain `(series == "<<NULL>>").mean()` would do the same job.** This isn't a hybrid_v1 problem — it's a pre-existing characteristic of the **current, non-hybrid oracle itself**, discovered while tracing rather than assumed. It is the single fact that most changes what "SQL parity for null_rate" even means (R.2.1 below).

**This is YAML-path-dependent, not universal — do not over-generalize it:**
- `src/generated_queries/sql_query_generator.py` / `yaml_config_writer.py` (the `run_with_plan()` backend): **always** COALESCE-wrapped, per above.
- `webapp/app.py`'s own independent YAML paths (RPJ/prompt tabs, per CLAUDE.md's documented duplication) expose an **explicit user-facing toggle**: *"Validation query (COALESCE / `<<NULL>>` normalized)"* vs. *"Simple query (plain readable SQL)"* (`webapp/app.py:2526,2531`, and the per-dialect COALESCE strings duplicated again at `webapp/app.py:2331-2358`). Only the first mode reproduces the sentinel; "Simple query" mode does not, and real `None` would reach pandas for those tables (subject to each connector's own NULL handling, R.1.1 below).
- `src/excel_batch_loader.py`'s `write_yaml()` takes `src_sql`/`tgt_sql` verbatim from the user's spreadsheet columns ("Legacy Query (Redshift)"/"Snowflake Query") — grepped for `COALESCE`/`NULL_PLACEHOLDER`/`base_rules` in that file: **no matches**. It does not impose the sentinel itself; whether NULL survives depends entirely on what the test lead typed.

So: whether `null_rate` is measuring anything real for a given table is a property of *which of the (at least 3 relevant) YAML-writing paths* produced that table's config — not a fixed fact about "the oracle." Any hybrid_v1 SQL-side null-rate design has to pick one semantic and say so explicitly (R.2.1, R.5).

#### R.1.1 Connector NULL/type behavior (traced, not assumed)

| Connector | File | NULL representation reaching pandas | Native typing |
|---|---|---|---|
| Postgres | `Project/db/postgres.py:37-50` | `psycopg2` returns Python `None` for SQL NULL (`cur.fetchall()`, no post-processing) | Typed (`int`/`float`/`Decimal`/`datetime`/`str`) — unless COALESCE'd away per R.1 |
| Redshift ("TradeShift") | `Project/db/factory.py:82-93` | Same as Postgres — **reuses the `Postgres` class verbatim**, only the default port differs (5439) | Same as Postgres |
| MSSQL (SiteLink) | `Project/db/mssqlserver.py:47-59` | `pyodbc` returns Python `None` for SQL NULL | Typed, same caveat |
| Snowflake (target) | `Project/db/snowflake.py:42-50` | `snowflake-connector-python`'s default (non-pandas) cursor returns Python `None` for SQL NULL | Typed, same caveat |
| Athena | `Project/db/athena.py:79-80` | **Every** cell is `row["Data"][i].get("VarCharValue", None)` — the key is present with an empty string for a real empty string and **absent** (→ `None`) for SQL NULL, so NULL vs. `''` is preserved correctly at this layer | **Everything is `str` or `None` — no native numeric/date/boolean type ever reaches pandas.** This is the one connector where a column that is numeric on every other side is `object`/text-dtype in the fetched frame. |

Two consequences for R.2 below: (a) once COALESCE (R.1) is *not* in play, all five sources agree on `None` for NULL, so a non-COALESCE `null_rate`/SQL-`IS NULL` comparison is not itself a cross-connector risk; (b) Athena's all-string typing means `distinct_count`/`aggregate` comparisons involving Athena depend entirely on `pd.to_numeric`/pandas' own string→number coercion already doing the normalization work Python-side — a SQL-side Athena aggregate would need to `CAST`/`TRY_CAST` explicitly to reach the same numeric domain the *other* four connectors get natively.

### R.2 Per-check analysis

#### R.2.1 `null_rate` (`quality_checks.py:49-59`)

1. **Exact current semantics.** For every column present (by lower-cased name) in both frames (`_column_pairs`, `quality_checks.py:21-23` — includes the PK/row_hash column too, nothing excludes it), `source_series.isna().mean() * 100` vs. same for target; failure if `abs(diff) > null_rate_tolerance_pct` (default effectively `0`, per R.0). `isna()` catches Python `None`, float `NaN`, `pandas.NaT` — nothing else.
2. **Reproducible from Tier-1 statistics?** **No.** Tier-1's `_collect_hash_multimap` (`tiered_runner.py`) produces exactly one opaque `(record_key, row_hash)` pair per physical row — there is zero per-column information in that structure. Unlike §Q's `expected_grain` (which only needed *row counts per key*, already present in the multimap), null-rate is inherently a **per-column** statistic; nothing about it can be derived from a structure that has already discarded column identity by hashing it away. A new SQL aggregate is required — there's no way to avoid it.
3. **SQL statistics needed, if built.** Per comparison column: `COUNT(*)` and `COUNT(*) - COUNT(<raw column>)` (SQL `COUNT(col)` already ignores NULL — no CASE/SUM needed) computed under the **same** `WHERE` scope as `sourcequery`/`targetquery` (source filter, exclusions already baked into that scope, and `_FIVETRAN_ACTIVE = TRUE` on the Snowflake side). One query per side, one row of output, analogous in shape to the dead `_distinct_count_pg`/`_distinct_count_sf` templates below. Crucially, this statistic must be computed on the **raw, pre-COALESCE column reference**, not the `sourcequery`'s already-wrapped `'<<NULL>>'`-sentinel text — because by design that text never reads as NULL to anything, SQL or Python.
4. **Per-connector applicability.** `COUNT(*)`/`COUNT(col)` is standard SQL, portable as-is across Postgres, Redshift (same connector), MSSQL, Snowflake, and Athena (Presto/Trino `COUNT` behaves identically). No dialect-specific rewrite needed — simpler than the existing `_hash_expression`'s per-dialect branching (`sql_query_generator.py:361-381`).
5. **Where SQL-side aggregation could disagree with the Python oracle.**
   - **NULL handling / semantic drift (the big one, R.1):** for any table where the YAML was generated with COALESCE active (the default for `run_with_plan()`-produced YAMLs), the *current Python oracle's* `null_rate` is not actually measuring `IS NULL` — it's measuring occurrence of the literal string `"<<NULL>>"`, which by construction only ever appears when the underlying value *was* NULL, so a raw SQL `COUNT(*) - COUNT(col)` computed on the pre-COALESCE column would, in that case, agree with what the Python check is *effectively* (if not literally) measuring. **But** for a "Simple query" (non-COALESCE) webapp-generated YAML, or an Excel-batch YAML with no COALESCE at all, the Python oracle's `isna()` *is* checking real Python `None` today — so a raw-SQL `IS NULL` aggregate would agree there too. The one genuinely risky case: a source value that happens to legitimately *contain* the literal text `"<<NULL>>"` — collision risk already flagged in the master doc §F for the row_hash sentinel, inherited here identically.
   - **Type conversion:** none relevant — `IS NULL`/`COUNT(col)` don't depend on the column's type.
   - **Filtering/exclusions:** must use the exact same `WHERE`/`_FIVETRAN_ACTIVE`/exclusion-scoped column list as `sourcequery`/`targetquery` (§B.1 invariant, master doc) — an easy place to introduce an asymmetric-filter bug if the null-rate query is hand-maintained separately rather than generated from the same builder.
   - **Driver behavior:** none — all five connectors agree on NULL→`None` at the raw-column level (R.1.1); the only difference is upstream of the driver, in whether COALESCE was applied by the generator.
6. **Cannot achieve exact parity, currently:** not a SQL-vs-Python gap at all — the *existing Python oracle's own behavior is generator-path-dependent* (R.1), so "exact parity with the oracle" is only a well-defined target once it's decided, per table, which of the (at least) two real behaviors ("measures string `'<<NULL>>'` occurrence" vs. "measures real Python `None`") that table's oracle run actually exhibits. A SQL design that always does `IS NULL` will match the *intended* semantic in both cases, but will not "match a broken measurement" if some downstream consumer has come to rely on the current near-always-zero result for COALESCE'd tables. Flag to the user before building, don't assume.

#### R.2.2 `distinct_count` (`quality_checks.py:61-71`)

1. **Exact current semantics.** `source_series.nunique(dropna=False)` vs. target; failure if `abs(diff) > distinct_count_tolerance` (default `0`). `dropna=False` means a NULL/NaN value counts as **one additional distinct category** if present — this is the opposite of SQL's `COUNT(DISTINCT col)`, which **excludes** NULL entirely.
2. **Reproducible from Tier-1 statistics?** **No**, same reasoning as R.2.1 — per-column, not derivable from the opaque hash multimap.
3. **SQL statistics needed, if built.** `COUNT(DISTINCT <raw column>)` **plus** a NULL-presence correction to match `dropna=False`: `COUNT(DISTINCT col) + CASE WHEN COUNT(*) > COUNT(col) THEN 1 ELSE 0 END`. Confirmed by tracing: the dead `⑦`/`⑧` templates already in this codebase (`sql_query_generator.py:671-728`, `_distinct_count_pg`/`_distinct_count_sf`) emit **plain** `COUNT(DISTINCT col)` with no such correction — i.e. even this prior-art SQL, if it were wired up as-is, would silently disagree with the Python oracle on any column containing at least one NULL. (These two functions are dead code today — `yaml_config_writer.py:428-429`'s YAML explicitly states *"null_pct_validation and distinct_count_validation have been removed"*, and the `distinct_source_yaml`/`distinct_target_yaml` parameters passed into `_write_data_validation_yaml_body`-equivalent are accepted but never referenced in the emitted `lines` — confirmed by reading the function body, not assumed.) Per CLAUDE.md's "no redundant implementations" rule, any real implementation must **extend/fix** these two existing functions, not write a third parallel distinct-count generator.
4. **Per-connector applicability.** `COUNT(DISTINCT col)` is standard SQL across all five; the NULL-correction `CASE` expression is equally portable. Athena/Presto's `COUNT(DISTINCT ...)` is a known-approximate operation only when `APPROX_COUNT_DISTINCT` is used — the *exact* `COUNT(DISTINCT ...)` form (used here and in the existing ⑦/⑧ templates) is exact on Athena too, just potentially more expensive on very high-cardinality columns.
5. **Where SQL-side aggregation could disagree with the Python oracle.**
   - **Semantic normalization (hard disagreement, not fixable by better SQL):** `distinct_count` runs on `source_df`/`target_df` **after** `canonicalize_frames` (`main.py:308`, before `run_quality_checks` at `main.py:378`). For any JSON/JSONB/HStore column, Python canonicalization key-sorts, rounds numbers to 2dp, and re-serializes (`semantic_normalize.py:205-338`) *before* `nunique()` runs — two JSON documents that differ only in key order or trailing-zero precision collapse to **one** canonical value in Python but remain **two** distinct raw strings in SQL. A SQL-side `COUNT(DISTINCT col)` on such a column will systematically over-count relative to the oracle. This is the exact same fundamental gap the master doc's §F already proved for row_hash — it applies identically here, for the same reason (SQL cannot reproduce this canonicalization, by the module's own design). Same for the numeric-string 2dp normalization (`semantic_normalize.py:360-375`) — `'400000.000000'` and `'400000.00'` are one distinct value after Python canonicalization, two before it.
   - **Type conversion / driver behavior:** low risk for plain scalar columns (int/text/date) with no JSON/decimal-precision ambiguity — `COUNT(DISTINCT col)` on the raw typed column should agree with `nunique()` on the same values once the NULL correction (point 3) is applied. Athena is the one connector where the *raw* column is text-typed by the driver either way, so no extra divergence there specifically for distinct_count (unlike aggregate, R.2.3).
   - **Filtering/exclusions:** same §B.1 requirement as R.2.1 — must reuse the generated `WHERE`, not a hand-copied one.
6. **Cannot achieve exact parity, currently:** any column that is JSON/HStore/semi-structured, or that needs the numeric-string 2dp normalization, **cannot** get exact `distinct_count` parity via SQL without solving the same canonicalization-equivalence problem the master doc's §F already declared NOT achievable ("two engines could not be made to agree" — `semantic_normalize.py:1-25`). For everything else, achievable with the NULL-count correction above, not achievable as dead-code-as-is (point 3).

#### R.2.3 `sum` / `min` / `max` ("aggregate") (`quality_checks.py:73-104`)

1. **Exact current semantics.** For every shared column: `pd.to_numeric(series, errors="coerce")` on **both** frames independently; skipped entirely (`source_numeric.notna().any() and target_numeric.notna().any()`, line 76) if either side coerces to all-NaN. Then `.sum()`, `.min()`, `.max()` (all NaN-skipping by default in pandas) compared with `abs(diff) / max(abs(source_value), 1.0) * 100 > aggregate_tolerance_pct`. Non-numeric-looking values are silently excluded from the aggregate (`errors="coerce"` → `NaN` → skipped), not treated as a mismatch.
2. **Reproducible from Tier-1 statistics?** **No** — per-column value aggregation needs the actual values; the hash multimap carries none.
3. **SQL statistics needed, if built.** Per numeric-looking comparison column: `SUM(<col>)`, `MIN(<col>)`, `MAX(<col>)`, computed with a `TRY_CAST`/safe-cast to a numeric type so non-numeric text doesn't error the whole query (dialect-specific: Snowflake/MSSQL/Athena(Presto) have `TRY_CAST`; vanilla Postgres/Redshift do not — would need a regex-guarded `CASE WHEN col ~ '^-?[0-9]+(\.[0-9]+)?$' THEN col::numeric ELSE NULL END` construct, bespoke work per the existing `_hash_expression` per-dialect pattern).
4. **Per-connector applicability.** Functionally portable everywhere with the per-dialect cast handling above; **but** which columns even get an aggregate clause generated must be decided **at YAML-generation time**, using each column's declared source type from schema extraction (`src/sql_extractor/extractors.py`) — not, as the Python oracle does today, by dynamically sampling the fetched values at runtime and testing whether they happen to parse as numeric. That's a structurally different decision mechanism (point 6).
5. **Where SQL-side aggregation could disagree with the Python oracle.**
   - **Type conversion / precision:** pandas' `.sum()` on a `to_numeric`-coerced column is float64 arithmetic (unless the source dtype is already `Decimal`-object, which `pd.to_numeric` would itself convert to float64) — the *current* oracle's own sum precision is bounded by IEEE-754 double precision, not by exact decimal arithmetic. A SQL-side `SUM(NUMERIC/DECIMAL)` could be **more** precise than the oracle, which means a genuinely-tiny rounding difference between the two could itself register as a new disagreement in the *other* direction (SQL "more correct" than the thing it's supposed to match). Needs an explicit decision: cast SQL-side to `DOUBLE`/`FLOAT` to deliberately match pandas' float64 behavior, or accept a documented precision divergence.
   - **NULL handling:** SQL `SUM`/`MIN`/`MAX` already skip NULL, matching pandas' NaN-skipping default — no correction needed here (unlike `distinct_count`).
   - **Column selection semantics (the real gap):** the Python oracle runs this check on *every* shared column and lets `pd.to_numeric` decide per-value whether it's numeric-looking, with no configuration. A SQL design must instead fix the column list at generation time from static type metadata. A column whose *declared* type is non-numeric (e.g. Postgres `VARCHAR`) but whose *actual* values happen to all be numeric-looking text would be included by the Python oracle and silently excluded by a schema-type-driven SQL design (or vice versa for a numeric-typed column that happens to hold occasional non-numeric noise, which `errors="coerce"` tolerates and a naive `TRY_CAST`-based SQL sum also tolerates the same way — that part *does* line up).
   - **Filtering/exclusions:** same §B.1 requirement as above.
   - **Driver behavior:** Athena's all-text typing (R.1.1) means every Athena-sourced numeric column needs an explicit cast in SQL to become summable at all — Python's `pd.to_numeric` already does this invisibly today, so this is pure added complexity, not a correctness risk, provided the cast is correct.
6. **Cannot achieve exact parity, currently:** exact byte-for-byte float precision matching against pandas' float64 sum is not free (point 5) — achievable with an explicit, documented precision-matching decision, not achievable "by default." The column-selection mechanism (declared-type-driven vs. runtime-sniffed) is a structural, not cosmetic, difference from the oracle and needs its own sign-off, independent of numeric precision.

#### R.2.4 `sample_hash` (`quality_checks.py:106-120`)

1. **Exact current semantics.** Only runs if `sample_hash_percent > 0` (the one true opt-in check, R.0). `sample_size = max(1, int(min(len(source_df), len(target_df)) * min(sample_percent, 100) / 100))` — **a percentage of the fetched row count, with no absolute cap.** Takes `.head(sample_size)` of each frame **in whatever order the DB driver returned rows** (this check runs *before* any `set_index()`/`sort_index()` in `main.py`/`row_compare.py` — confirmed by tracing call order: fetch → lower column names → `canonicalize_frames` → `run_quality_checks`/`validate_expected_grain` → *then* `compare_indexed_frames`, which is where indexing/sorting happens). Renders the sampled columns to CSV text (`.astype(str).to_csv(index=False)`) and SHA256-hashes each side's CSV blob; any difference fails.
2. **Reproducible from Tier-1 statistics?** **No** — this needs actual row content, and specifically the *first N rows in fetch order*, which Tier-1's `(key, hash)` multimap doesn't preserve any positional/ordering information about at all (it's collected into a dict, keyed by record key, with no fetch-order memory).
3. **SQL statistics needed, if built.** Not an aggregate at all — would need an additional bounded `SELECT ... LIMIT sample_size` fetch (no `ORDER BY`, matching the oracle's own lack of one) per side, hashed the same way in Python once fetched. This is fetch, not aggregation — closer in spirit to a small, capped Tier-2-style read than to the other three checks.
4. **Per-connector applicability.** `LIMIT n` is portable (Athena/Presto too). No dialect-specific SQL needed beyond a plain `LIMIT`.
5. **Where SQL-side aggregation could disagree with the Python oracle.** Not applicable in the same sense — the risk here isn't SQL-vs-Python disagreement, it's that **the oracle's own semantic is already order-fragile**: nothing in the traced pipeline guarantees the source and target's unordered `head(sample_size)` rows correspond to "the same" logical rows across two independently-executed queries against two different engines. This is a pre-existing weak assumption in the current, non-hybrid oracle — flagged here, per the master doc's convention (§B's "ambiguous/potentially-incorrect existing behavior" list), as something to reproduce faithfully, not silently fix, if hybrid_v1 ever implements this check.
6. **Cannot achieve exact parity, currently:** two separate, real blockers, not one:
   - **Scale:** `sample_size` is a *percentage* of row count with no cap — at 300M rows and even a 1% `sample_hash_percent`, that's 3,000,000 rows' full width, which reintroduces exactly the unbounded-materialization problem hybrid_v1 exists to avoid. Reproducing this check exactly, at scale, contradicts hybrid_v1's own premise unless the config is changed to also support an absolute row cap (a real, user-facing config change, not purely internal) — out of scope to decide unilaterally here.
   - **Determinism:** even ignoring scale, "first N rows in unordered fetch order, independently on two different database engines" was never a rigorously guaranteed-aligned sample to begin with; hybrid_v1 inherits that fragility rather than curing it.

### R.3 Consolidated: what cannot currently achieve exact oracle parity via SQL

1. **`distinct_count`/any column-level check on JSON/HStore/semi-structured or numeric-string-precision columns** — blocked on the same canonicalization-equivalence problem the master doc's §F already proved unsolvable in SQL. Not a hybrid_v1-specific gap; inherited from the whole architecture's foundational decision to keep canonicalization Python-only.
2. **`null_rate`'s "what does it actually mean" question** — not a SQL-parity problem so much as the discovery that the *current oracle's own measurement* is generator-path-dependent (R.1). Needs a human decision on intended semantic before any SQL equivalent can be judged "correct," let alone "parity."
3. **`sum`/`min`/`max` exact float precision matching** — achievable only with an explicit, deliberate precision-matching decision (cast to float64-equivalent vs. accept decimal-vs-float divergence); not free.
4. **`sum`/`min`/`max` column-selection mechanism** — runtime-value-sniffed (oracle) vs. generation-time-schema-typed (any SQL design) are structurally different decision points; can diverge on any column whose declared type disagrees with its actual value shape.
5. **`sample_hash` at scale** — the check's own percentage-based, uncapped sample size is fundamentally incompatible with hybrid_v1's no-full-materialization goal; needs a config change (absolute cap) before it can be attempted safely, independent of whether the SQL/fetch mechanics are correct.
6. **`sample_hash` determinism** — inherited fragility (row-order alignment across independently-executed queries on different engines) that no SQL rewrite fixes.

### R.4 Proposed design (PROPOSAL only — nothing built)

- **`null_rate` and `distinct_count`**: extend (not replace) the existing per-column aggregate-query mechanism the codebase already has prior art for (`_distinct_count_pg`/`_distinct_count_sf`, `sql_query_generator.py:671-728`, currently dead) into a single new "Tier-1 quality aggregate" query per side, generated by the *same* builder that already bakes in filters/exclusions/Fivetran (§B.1 invariant) — one `SELECT COUNT(*), COUNT(colA), COUNT(DISTINCT colA), COUNT(colB), COUNT(DISTINCT colB), ... FROM ... WHERE ...` per side, executed once per table alongside (not instead of) the existing Tier-1 hash query. Fix the `COUNT(DISTINCT ...)` NULL-correction gap (R.2.2.3) while extending it. Explicitly resolve the `null_rate` semantic question (R.1) before wiring this up — likely as a config-level or table-level declared choice, not a silent default.
- **`sum`/`min`/`max`**: a second small aggregate query, but only for columns whose *declared* type (from schema extraction) is numeric — computed with an explicit, documented precision choice (R.2.3.5) and per-dialect safe-cast handling.
- **`sample_hash`**: do not attempt until the config gains an absolute row cap; even then, treat as a bounded `LIMIT`-based fetch-and-hash, not an aggregate, and accept (or explicitly re-decide) the ordering fragility already present in the oracle.
- **Failure reporting**: whatever gets built should return the same failure-dict shape (`check`, `column`/`side`, `source`, `target`, `tolerance`) `run_quality_checks` already uses, and fold into `is_match` the same way §Q's `grain_failures` does — i.e. extend `tiered_runner.run_table_hybrid`'s result dict with a `quality_failures` key, mirrored into `main.py`'s hybrid branch exactly like `grain_failures` already is, not a new, separate reporting channel.
- **None of this touches `quality_checks.py` or the non-hybrid path** — same ground rule as §Q.

### R.5 Differential-test matrix (to run before any of R.4 is implemented)

| Case | Exercises |
|---|---|
| Column with zero NULLs, both sides | `null_rate` = 0 both ways, SQL and Python agree trivially |
| Column with NULLs, YAML generated via COALESCE-wrapped pipeline | Proves/disproves R.1: does the *current* Python oracle actually report nonzero, or does the sentinel mask it? Must be run against a live-generated YAML, not a hand-built test frame, or it repeats the same blind spot `test_quality_checks.py`'s current unit test has |
| Column with NULLs, YAML generated via webapp "Simple query" mode | Confirms real `None` reaches pandas in this mode; SQL `IS NULL` should agree with Python `isna()` here |
| Distinct-count column with one NULL among otherwise-unique values | Proves the `dropna=False` vs. `COUNT(DISTINCT)` off-by-one; SQL must apply the NULL-presence correction (R.2.2.3) to match |
| JSON column, same document reordered/reformatted on each side | SQL `COUNT(DISTINCT)` must be shown to *disagree* with the Python oracle here (expected divergence, not a bug to chase) — confirms R.2.2.5/R.3.1 rather than assuming it |
| Numeric-string column, differing trailing-zero precision (`'5.00'` vs `'5.0000'`) | Same disagreement class as above, for `distinct_count` and for numeric-string-typed `sum` columns |
| Aggregate column with one non-numeric noise value on one side | Confirms `errors="coerce"`-equivalent behavior (SAFE_CAST/TRY_CAST) reproduces "skip, don't fail the query" |
| Aggregate column whose declared SQL type is non-numeric but values are numeric-looking text | Proves the schema-type-driven column-selection gap (R.2.3.6) is real, not hypothetical |
| Large sum where float64 vs. DECIMAL rounding differ at the last digit | Confirms whether the chosen precision strategy (R.2.3.5) actually matches the oracle or introduces a new false mismatch |
| `sample_hash` at a row count large enough that percentage-based sample_size would exceed a reasonable Tier-2-style batch bound | Confirms R.2.4.6's scale blocker is real and measurable, not theoretical |
| Every check, config present but only `enabled: true` (no tolerance keys) | Confirms the SQL design reproduces the "0.0-tolerance-by-default" activation trap (R.0), not an assumed opt-in-per-check model |

### R.6 Recommendation

Do not implement R.4 yet. Two decisions block it, and both are product/data-quality-policy decisions, not implementation details: (1) what `null_rate` should *mean* going forward, given R.1's discovery that its current, real-world behavior already varies by which YAML-generation path produced a given table; (2) whether `sample_hash_percent` gets an absolute row cap before hybrid_v1 is allowed to attempt it at all. `distinct_count` and `sum`/`min`/`max` are buildable once those two are resolved, with the explicit caveats in R.2.2/R.2.3/R.3 accepted (JSON/decimal-precision columns will never reach exact SQL parity; that's inherited from the master doc's §F, not new).

---

## S. Implemented: `run_quality_checks` parity for hybrid_v1 (single-column PK only)

**Status:** Implemented and tested, per explicit instruction to proceed past §R.6's "do not implement yet" and resolve its two blocking decisions directly (documented below, not deferred). Scope: `null_rate`, `distinct_count`, `sum`/`min`/`max`, `sample_hash` — the four checks inside `run_quality_checks`. `validate_expected_grain` is unaffected (§Q, already implemented). Same scope restriction as §Q: single-column PK only; PK-less and composite-PK tables never reach this code (unchanged, see S.6).

### S.1 What was built

`Project/tiered_runner.py` gained `_run_quality_checks_hybrid()` and its helpers, called from `run_table_hybrid` right after the `row_hash.columns` coverage check, using the same `src_probe_cols`/`tgt_probe_cols` schema probe §A already computes (no new query for column discovery). Every check is pushed down to a bounded SQL aggregate or a capped `LIMIT`/`TOP` sample over the **existing, unmodified** `sourcequery`/`targetquery` text (the same subquery-wrapping trick `_probe_columns`/`_fetch_batch` already use) — every Fivetran/exclusion/filter clause baked into that text is reused exactly; no independent filter/WHERE logic was written.

Two small, behavior-preserving extractions from `Project/utils/quality_checks.py` (the non-hybrid oracle) make this possible without duplicating logic (`CLAUDE.md`'s "no redundant implementations" rule):
- `_pct_difference(source_value, target_value)` — the `abs(diff)/max(abs(source),1.0)*100` tolerance formula, previously inlined three times in `run_quality_checks`, now called from there and from hybrid_v1's aggregate check.
- `_hash_dataframe(df, columns)` — the `astype(str).to_csv(index=False)` → sha256 hashing, previously inlined once in `run_quality_checks`'s `sample_hash` block, now called from there and from hybrid_v1's `sample_hash` check.

Both are pure refactors verified against the existing `Project/utils/test_quality_checks.py` (unchanged assertions, still passing) — the non-hybrid path's behavior is byte-for-byte identical to before.

One small addition to `Project/utils/semantic_normalize.py`: `looks_numeric_string(value)`, reusing the existing `_NUMERIC_STR_RE` pattern `_normalize_numeric_str` already uses, so hybrid_v1's canonicalization-risk detection (S.3) doesn't duplicate that regex.

### S.2 `null_rate` — the §R.6 blocking decision, resolved

**Decision:** hybrid_v1's `null_rate` measures `{col} IS NULL OR CAST({col}) = '<<NULL>>'` — i.e. it treats *both* a real SQL NULL and the `base_rules.py` COALESCE sentinel as null-equivalent, computed via `COUNT(*)` vs. a `SUM(CASE WHEN ... THEN 1 ELSE 0 END)` in one aggregate query per side (`_quality_aggregate_sql`'s `{col}__nullish`). This resolves §R.1's ambiguity by picking the **intended** semantic (a value that was NULL in the source system) rather than a semantic that depends on which of the ≥3 YAML-generation paths produced this table's queries.

**Documented consequence, not hidden:** for a table whose YAML came from the COALESCE-wrapped `run_with_plan()` pipeline (the default), the *current non-hybrid oracle's* `isna()`-based check almost never sees a real `None` (COALESCE already turned it into the sentinel string before Python ever gets the row) — so hybrid_v1's null_rate can report a **nonzero** rate on a table where the non-hybrid oracle, run today, would report **zero**, purely because they're measuring different things on that table class. This is not a hybrid_v1 bug to "fix" — §R.1 already established that "exact parity with the oracle" isn't even a well-defined target for `null_rate` until this decision was made, and this section *is* that decision, made explicitly rather than left unresolved. For "Simple query" webapp-generated YAMLs (no COALESCE, real `None` reaches pandas), hybrid_v1's SQL `IS NULL` and the oracle's `isna()` already agree — no divergence there.

### S.3 `distinct_count`

`COUNT(DISTINCT col)` computed in the same aggregate query, **plus** the NULL-presence correction §R.2.2.3 identified as missing from this codebase's only prior art (the dead `_distinct_count_pg`/`_distinct_count_sf` templates): `+1` to the raw distinct count whenever `COUNT(*) > COUNT(col)` on that side, matching pandas' `nunique(dropna=False)` counting a real NULL as one more category that plain `COUNT(DISTINCT)` would otherwise exclude.

**Canonicalization-risk columns are excluded, not mismeasured.** Before building the aggregate query, a bounded (`QUALITY_SAMPLE_ROWS = 1000`), two-sided sample is fetched via `LIMIT`/`TOP` and scanned with the existing `looks_semi_structured` (JSON/HStore) and new `looks_numeric_string` (`'400000.00'` vs `'400000.000000'`) detectors. Any column either sample flags is dropped from `distinct_columns` entirely — its `distinct_count` check simply does not run under hybrid_v1 for that column, logged once at `INFO` (`"hybrid_v1 distinct_count check skipped for table=... canonicalization-risk column(s) ..."`). This is the §R.2.2.5 gap the master doc's §F already proved unfixable in SQL (canonicalization is Python-only by design) — hybrid_v1 does not attempt to solve it, and does not silently produce a wrong answer for it either.

**Documented sampling limitation:** classification uses the first ≤1000 rows per side, not the full column — a column that looks plain in the sample but contains semi-structured content later in a 300M-row table would not be excluded and could disagree with the oracle. Accepted at this scale; not silently claimed away.

### S.4 `sum` / `min` / `max`

Pushed down as `SUM`/`MIN`/`MAX` over a safe numeric cast in the same aggregate query, only for columns the two-sided sample classifies as numeric via `pd.to_numeric(sample, errors="coerce").notna().any()` on **both** sides (`_numeric_sample_columns`, mirroring `run_quality_checks`'s own `source_numeric.notna().any() and target_numeric.notna().any()` AND-test exactly — same runtime-sniffing semantic the brief required be preserved, just sampled instead of full-column, same documented limitation as S.3).

Cast (`_dialect_numeric_cast`): `TRY_CAST(col AS FLOAT/DOUBLE)` for MSSQL/Snowflake/Athena/Trino/Presto; a regex-guarded `CASE WHEN col ~ '^-?[0-9]+(\.[0-9]+)?$' THEN col::double precision ELSE NULL END` for Postgres/Redshift (no `TRY_CAST` there). `DOUBLE`/`FLOAT` was chosen deliberately to mirror pandas' float64 arithmetic (the oracle's own precision class), not to be "more correct" than the oracle — an intentional, not incidental, precision match.

Tolerance math is the extracted `_pct_difference` (S.1) — identical formula, identical near-zero-denominator floor (§B's already-flagged caveat), unchanged.

### S.5 `sample_hash` — the other §R.6 blocking decision, resolved

**Decision:** `SAMPLE_HASH_ABSOLUTE_CAP = 10_000` rows per side. `sample_size = min(percentage-implied size, 10_000)`, fetched via the same bounded `LIMIT`/`TOP` mechanism as S.3's classification sample, hashed with the extracted `_hash_dataframe` (S.1) — byte-identical hashing algorithm to the oracle, just capped input size. This directly resolves §R.2.4.6's scale blocker (an uncapped percentage at 300M rows could mean millions of full-width rows, defeating hybrid_v1's purpose).

**Inherited, not fixed:** the oracle's own ordering fragility (§R.2.4.5 — two independently executed, unordered queries against two different engines aren't guaranteed to return "the same" logical rows first) carries over unchanged. hybrid_v1 does not add `ORDER BY` where the oracle has none.

### S.6 Deliberate non-goals (per instruction, not oversight)

- **PK-less tables**: untouched — the PK-less early-return block in `run_table_hybrid` still returns before any quality-check code runs (same as §Q.3's `grain_failures`), so `result["quality_failures"]` is simply absent, and `main.py`'s `hybrid_result.get("quality_failures", [])` defaults to `[]`.
- **Composite PKs**: unreachable — `run_table_hybrid` already raises `NotImplementedError` for composite PKs before any of this code runs (pre-existing behavior).
- **`quality_checks.py` and the non-hybrid path in `main.py`**: behavior-preserving only — the two extracted helpers (S.1) produce identical output to the inline code they replaced, verified by the pre-existing `test_quality_checks.py` suite still passing unchanged.
- **The "0.0-tolerance-means-active" default trap (§R.0)**: preserved intentionally in hybrid_v1 too — a table with only `quality_checks: {enabled: true}` and no explicit tolerance keys runs `null_rate`/`distinct_count`/`aggregate` at exact-match tolerance under hybrid_v1 exactly as it does under the oracle.
- **No live 5-connector integration run**: this implementation was validated with a pandas-based fake-DB fixture (SQL-shape assertions + a hand-computed aggregate simulation, `Project/test_tiered_runner.py`), not against real Postgres/MSSQL/Snowflake/Athena/Redshift instances. The dialect-specific SQL (`TRY_CAST` vs. regex-guarded cast, `LIMIT` vs. `TOP`) is believed correct per each dialect's documented syntax but has not been benchmark-verified live — same open item §L already flags for the row-comparison side, now applying to the quality-check queries too.

### S.7 Memory and scalability

Every check is either one bounded SQL aggregate (`COUNT`/`SUM`/`MIN`/`MAX`/`COUNT DISTINCT` — one row of output, computed server-side, full-table scope) or a capped `LIMIT`/`TOP` fetch (≤1,000 rows for classification, ≤10,000 rows for `sample_hash`) per side. At no point does this code hold a full source frame, full target frame, or full result simultaneously — consistent with the hard requirement in §N. The classification sample (≤1,000 rows) and `sample_hash` sample (≤10,000 rows) are the only row-level fetches; both are hard caps independent of table size, so this scales flat from 1M to 300M rows in memory terms (aggregate query cost/latency at the database side is a separate, unmeasured concern per §L).

### S.8 Tests executed and results

`Project/test_tiered_runner.py`, run via `python -m pytest Project/test_tiered_runner.py -q`: **27 passed** (19 pre-existing + 8 new — the 8 new tests each cover multiple assertions/cases per the list below). New tests added:

| Test | Covers |
|---|---|
| `test_null_rate_failures_catches_real_null_and_sentinel_uniformly` | `{col}__nullish` tolerance math, exact shape match to oracle's failure dict |
| `test_null_rate_tolerance_boundary_is_strict_greater_than` | Tolerance boundary is `>`, not `>=` (matches oracle) |
| `test_distinct_count_null_presence_correction_matches_dropna_false` | The `+1` NULL-presence correction (§R.2.2.3) makes SQL agree with `dropna=False` |
| `test_distinct_count_real_mismatch_still_fails` | Correction doesn't mask a genuine distinct-count drift |
| `test_canonicalization_risk_columns_flags_json_and_numeric_string_not_plain` | JSON and numeric-string columns flagged, plain columns not |
| `test_numeric_sample_columns_matches_pd_to_numeric_any` | Sample-based numeric classification matches `pd.to_numeric(...).notna().any()` |
| `test_dialect_numeric_cast_uses_try_cast_or_regex_guard` | Per-dialect cast SQL shape (`TRY_CAST` vs. regex-guarded `CASE`) |
| `test_quality_aggregate_sql_only_includes_safe_columns` | Generated SQL includes distinct/numeric parts only for the safe/classified column sets |
| `test_sample_hash_failures_caps_absolute_size` | `sample_hash_percent=100` on a 1M-row table still issues `LIMIT 5` when the cap is patched to 5 — proves the cap, not just the result |
| `test_sample_hash_failures_noop_without_fetching_when_inactive` | No fetch at all when `sample_hash_percent<=0`, either side has 0 rows, or no shared columns |
| `test_quality_checks_hybrid_null_rate_drift_end_to_end` | End-to-end through `run_table_hybrid`: all keys HASH_MATCH (row-level alone would PASS), only the quality gate flips `is_match` |
| `test_quality_checks_hybrid_distinct_count_skips_canonicalization_risk_column` | A JSON column with raw-text key-reordering (raw distinct would disagree, canonical wouldn't) produces **no** false failure, because it's excluded |
| `test_quality_checks_hybrid_aggregate_sum_drift_end_to_end` | End-to-end `sum` check catches real numeric drift and flips `is_match` |
| `test_quality_checks_hybrid_empty_tables_no_crash` | Both sides 0 rows, all four checks configured — no exception, no false failures |
| `test_quality_checks_hybrid_pk_less_early_return_has_no_quality_failures_key` | PK-less clean-match path has no `quality_failures` key at all (§S.6) |

**Regression suite**: full `Project/` test tree (`python -m pytest Project/ -q`) — **82 passed**, 0 failed, including the pre-existing `test_hybrid_dispatch.py` (all dispatch-gating tests) and `Project/utils/test_quality_checks.py` (the non-hybrid oracle's own tests, confirming the S.1 extractions didn't change its behavior). No existing test was modified to make it pass.

### S.9 Python compilation

`python -m py_compile` passed on every touched file: `Project/tiered_runner.py`, `Project/main.py`, `Project/utils/quality_checks.py`, `Project/utils/semantic_normalize.py`, `Project/test_tiered_runner.py`.

### S.10 Remaining risks / open decisions

1. **No live database validation** (S.6) — dialect SQL syntax is believed correct but unverified against real Postgres/MSSQL/Snowflake/Athena/Redshift; should be part of the same benchmark/differential-testing pass §K/§L already call for before production rollout.
2. **`null_rate`'s new semantic (S.2) is a genuine behavior decision, not just an implementation detail** — worth confirming with whoever owns data-quality policy that "null-or-sentinel" is the right combined meaning going forward, independent of hybrid_v1 (it also quietly reveals that the non-hybrid oracle's own `null_rate` has been under-detecting on COALESCE'd tables all along, since §R.1's finding equally applies there — flagged, not fixed, in either engine here).
3. **Sample-based classification (S.3/S.4) can disagree with a full-column oracle decision** in a table whose first 1,000 rows are unrepresentative — same class of risk as §L's general "measure, don't assume" caution, not yet measured against a real skewed table.
4. **`SAMPLE_HASH_ABSOLUTE_CAP = 10_000` is a default, not a tuned number** — chosen as a reasonable bound, not derived from a benchmark; revisit once §L's ladder produces real query-cost numbers.
5. **Binary/bytea columns**: not exercised by any of the four checks' logic here, same untested/unknown status the master doc's §F already flags for row_hash.

## T. Fixed: live oracle-vs-hybrid_v1 divergences (S.6/S.10's "no live validation" gap, closed)

**Status:** §S.6 explicitly flagged hybrid_v1 as validated only against a pandas fake-DB fixture, never against real connectors. A live oracle-vs-hybrid_v1 run against the real controlled Postgres and Snowflake instances (see T.7) found six real divergences; all six are addressed below — five with a code fix, one (T.6) with an explicit, deliberate "keep as documented" decision per its own instruction not to silently change the semantic again.

### T.1 CRITICAL — Snowflake quoted-identifier bug in every outer wrapper query

**Root cause:** every base-rule-generated Snowflake `targetquery` emits its `*_normalized` aliases **quoted lowercase** (`AS "id_normalized"`), specifically so Snowflake's default unquoted-identifier uppercase folding doesn't apply to them. `Project/tiered_runner.py`'s Tier-2 narrowed fetch (`_fetch_batch`'s `WHERE {pk_col} IN (...)`) and quality-check aggregate (`_quality_aggregate_sql`'s `COUNT`/`SUM`/`COUNT DISTINCT`/nullish-`CASE`) both wrap that `targetquery` as a subquery and then referenced its projected columns **unquoted** — Snowflake resolves an unquoted `id_normalized` to `ID_NORMALIZED`, which the inner query never produced, either erroring outright (`invalid identifier`) or (worse, on dialects where it doesn't error) silently targeting the wrong column.

**Fix:** new `_quote_ident(dialect, name)` in `tiered_runner.py` — double-quotes the name for every dialect except MSSQL (bracket-quotes there). Applied everywhere an outer query references a column projected by the wrapped `sourcequery`/`targetquery`: `_fetch_batch`'s PK reference, and every column reference inside `_quality_aggregate_sql` (`COUNT`, the nullish `CASE`, `COUNT(DISTINCT ...)`, and the numeric cast passed to `SUM`/`MIN`/`MAX`). Quoting the already-lowercased column name is correct on every dialect here, not just Snowflake: Postgres/Redshift/Athena/Trino also fold *unquoted* aliases to lowercase, so quoting the lowercase name reproduces the same projected identifier there too — this is not a Snowflake-only patch. `_probe_columns` and `_limit_query`/`_fetch_sample` need no change — both use `SELECT *`, never naming a specific column.

**Live confirmation (T.7):** the fix was exercised end-to-end against real Postgres + Snowflake on `semistructured_demo` (quoted-lowercase target aliases on every column) — Tier-2 fetch, schema probe, and the quality-check aggregate all executed against Snowflake without an `invalid identifier` error.

**Tests:** `test_quote_ident_snowflake_style_lowercase_double_quotes`, `test_fetch_batch_quotes_pk_reference_for_snowflake`, `test_quality_aggregate_sql_quotes_every_column_reference` (`Project/test_tiered_runner.py`).

### T.2 HIGH — PostgreSQL Tier-1 hashing depended on pgcrypto, which the controlled databases don't have

**Root cause:** `src/generated_queries/sql_query_generator.py`'s `_hash_expression` used `encode(digest(..., 'sha256'), 'hex')` for a `postgresql` source — `digest()` is a `pgcrypto` extension function, not a PostgreSQL core builtin. Confirmed live: `SELECT encode(digest('x','sha256'),'hex')` against the controlled `migration_demo` Postgres instance raises `UndefinedFunction: function digest(unknown, unknown) does not exist`.

**Fix, not a fallback:** switched the `postgresql` branch to `MD5(...)`, a PostgreSQL **core builtin** — no extension, no prerequisite check needed (MD5 always works). Confirmed live: `SELECT MD5('x')` succeeds against the same instance. Redshift is unaffected — it already had its own native `SHA2(...)` branch, never depended on `digest()`.

**Preserving Tier-1 semantics (the part that actually mattered here):** Tier 1 classifies a row by comparing the raw hash *string* computed on the source side against the raw hash string computed on the target (Snowflake) side directly — both sides must use the same algorithm over the same pipe-joined input, or identical row content never produces equal hashes. Switching only the Postgres source side to MD5 while leaving the paired Snowflake target side on `SHA2(..., 256)` would have silently broken every Postgres-sourced table's Tier 1 classification (every row looking like a hash mismatch, all falling through to Tier 2). `_hash_expression` gained an `algorithm` parameter (`"sha256"` default, `"md5"` opt-in); `_row_hash_queries` now derives the paired algorithm from `plan.source_db_type` — `"md5"` only when the source is `postgresql`, `"sha256"` (unchanged) for every other source dialect — so the two sides of any one table's Tier-1 comparison always agree on algorithm. Snowflake's `MD5()` builtin (lowercase 32-hex output, same as PostgreSQL's) makes this pairing exact.

**Live confirmation (T.7):** `semistructured_demo` (Postgres source) classified all 12 rows as `HASH_MATCH` using the paired MD5 formula on both sides — cross-engine MD5 comparability confirmed live, not just unit-tested.

**Tests:** `src/generated_queries/test_sql_query_generator.py` (new file) — `test_postgresql_hash_expression_uses_md5_not_pgcrypto_digest`, `test_redshift_hash_expression_unchanged_sha256`, `test_snowflake_hash_expression_algorithm_switch`, `test_row_hash_queries_algorithm_selection_matches_source_dialect`.

### T.3 HIGH — `sample_hash` hashed the raw fetched sample, not the canonicalized one

**Root cause:** `Project/main.py` calls `canonicalize_frames()` **before** `run_quality_checks()` (line ~310 vs ~380) — the oracle's `sample_hash` always hashes canonicalized cells (JSON key order normalized, HStore formatting normalized, numeric-string precision normalized). `tiered_runner.py`'s `_sample_hash_failures` fetched its own bounded sample and hashed it directly, skipping canonicalization entirely — a JSON column with only key-order differences (semantically identical) would hash unequal under hybrid_v1 on every single run, a false failure the oracle would never raise.

**Fix:** `_sample_hash_failures` now calls `canonicalize_frames(src_sample, tgt_sample)` immediately after fetching, before `_hash_dataframe` — same fetch-cap-then-hash shape as before, oracle-equivalent semantics now.

**Tests:** `test_sample_hash_failures_canonicalizes_before_hashing` (JSON key-reorder fixture, asserts no false failure).

### T.4 HIGH — aggregate SUM/MIN/MAX ignored `canonicalize_frames`' numeric-string rounding

**Root cause:** `canonicalize_frames`' `_normalize_numeric_str` rounds any plain-decimal-string column (`'400000.000000'` shape) to 2dp **before** the oracle's `pd.to_numeric(...).sum()` ever runs (same call-order fact as T.3) — e.g. a raw `'0.1234'` becomes `'0.12'` before summing. hybrid_v1's SQL `SUM`/`MIN`/`MAX` aggregated the raw, unrounded numeric cast — reproducing the exact live-observed divergence (oracle `SUM = 1.23`, hybrid SQL `SUM = 1.2334`).

**Fix:** new `_numeric_string_round_columns(sample_df, columns)` — same shape-detection as `_canonicalization_risk_columns` but using only the `looks_numeric_string` signal, computed as the union across both sides' samples (same symmetric-union philosophy `canonicalize_frames` itself documents). `_dialect_numeric_cast` gained a `round2` parameter that wraps the cast in `ROUND(..., 2)`; `_quality_aggregate_sql` applies it to every column in `round2_columns`. Rounding a column that's *not* actually a decimal-string (e.g. a plain integer) to 2dp is a no-op, so applying `ROUND` to every row of a flagged column is exactly equivalent to the oracle's per-cell selective rounding — no partial-column special-casing needed.

**Tests:** `test_numeric_string_round_columns_flags_decimal_strings_only`, `test_quality_aggregate_sql_rounds_numeric_string_columns`, `test_dialect_numeric_cast_round2_wraps_round`.

### T.5 MEDIUM — `distinct_count` canonicalization-risk exclusion: investigated, kept (not weakened)

Per instruction, investigated whether JSON/HStore/numeric-string columns could reach exact or safely-bounded `distinct_count` parity instead of the blanket exclusion (§S.3):

- **Numeric-string columns**: parity *would* be achievable by reusing T.4's `ROUND(..., 2)` transform inside `COUNT(DISTINCT ...)` — but only for a column whose *every* value matches the plain-decimal shape. A column mixing decimal-string and non-matching values would need a per-dialect regex-gated `CASE` (`~` on Postgres/Redshift/Athena, no native regex on MSSQL at all) to reproduce `canonicalize_value`'s "round if it matches, pass through unchanged otherwise" behavior exactly — asymmetric per-dialect complexity for a MEDIUM check, and MSSQL genuinely cannot do it without an approximation that risks a different kind of silent wrongness.
- **JSON/HStore columns**: `canonicalize_value`'s key-reordering has no generic cross-dialect SQL equivalent (same conclusion the master doc's §F already reached, reconfirmed here).

**Decision:** exclusion preserved for both categories (`_canonicalization_risk_columns` unchanged) — per this task's own instruction, "if exact parity is not possible without reproducing Python canonicalization, preserve the safe exclusion and document it" is the correct outcome here, not a fallback. No code change; this section is the documentation.

### T.6 `null_rate`'s hybrid-vs-oracle placeholder contract — kept as-is, documented, not re-litigated

Per explicit instruction not to silently change this semantic again: hybrid_v1's `null_rate` contract (§S.2) stays `SQL NULL OR CAST(col) = '<<NULL>>' ⇒ null-equivalent`. §S.2 already documented the consequence in the abstract; this section records it as **live, retained evidence**, not a hypothetical:

Once a column is COALESCE'd to the `'<<NULL>>'` sentinel (the default `base_rules.py` path), the oracle's `run_quality_checks` never sees a real null there — `pd.Series.isna()` doesn't match a non-null string — so the oracle's `null_rate` on that column is measuring something closer to "0%, always" for that column class, while hybrid_v1's SQL-side check still treats the sentinel as null-equivalent. Retained as a passing regression, not a bug: `test_null_rate_hybrid_placeholder_contract_intentionally_diverges_from_oracle` (`Project/test_tiered_runner.py`) constructs a COALESCE'd fixture, runs both the real oracle (`quality_checks.run_quality_checks`) and hybrid's `_null_rate_failures` against it, and asserts they disagree in exactly this direction — if a future change makes them agree "by accident," this test will fail and force an explicit decision instead of a silent drift.

### T.7 Live connector validation performed this pass

Executed against the real controlled instances (`.env`'s `local` environment: `SRC_1` PostgreSQL `migration_demo` ↔ Snowflake `MIGRATION_DEMO.PUBLIC`), not simulated:

- Confirmed `digest()` genuinely absent (`UndefinedFunction`) and `MD5()` genuinely present on the controlled Postgres instance (T.2).
- Ran `tiered_runner.run_table_hybrid` end-to-end against `semistructured_demo` (12 rows, JSON/JSONB/HStore/array columns) with a hand-built MD5-paired `row_hash_validation` block: Tier 1 classified all 12 keys `HASH_MATCH` via cross-engine MD5 comparison; the post-fix quality-check aggregate (`COUNT`/`SUM`/nullish-`CASE`/`COUNT DISTINCT` against Snowflake's quoted-lowercase `*_normalized` columns) executed without an `invalid identifier` error; `distinct_count` correctly excluded all four semi-structured columns; `sample_hash` ran through `canonicalize_frames` and still surfaced a **genuine** content divergence (this demo table intentionally encodes edge cases like `DEMO-10-hstore-null-vs-empty`) — a real finding, not a validator bug, confirming `sample_hash` still catches real drift after the T.3 fix.
- **Athena and Redshift**: `SRC_4_HOST` is unset in `.env` and no Athena credentials/result-location are configured — neither was reachable this pass, consistent with §L/§S.6's existing "no live 5-connector run" gap for those two specifically. Not fabricated; still open.

### T.8 Tests executed and results

`python -m pytest Project/ -q`: **90 passed**, 0 failed (81 pre-existing/S.8-era + 9 new in `Project/test_tiered_runner.py`: T.1's 3, T.4's 3, T.3's 1, T.6's 1, plus the pre-existing suite unchanged). `python src/generated_queries/test_sql_query_generator.py` (new file, T.2): 4 passed. `python -m py_compile` clean on every touched file: `Project/main.py`, `Project/tiered_runner.py`, `Project/test_tiered_runner.py`, `Project/utils/quality_checks.py`, `Project/utils/semantic_normalize.py`, `src/generated_queries/sql_query_generator.py`, `src/generated_queries/test_sql_query_generator.py`.

### T.9 Remaining known divergences (after this pass)

| Connector | Table | Check | Oracle | Hybrid | SQL/path | Cause | Intentional? |
|---|---|---|---|---|---|---|---|
| postgresql→snowflake | any COALESCE'd table | `null_rate` | ~0% for sentinel-only columns | counts sentinel as null | `_quality_aggregate_sql`'s nullish `CASE` | Different definition of "null" post-COALESCE (T.6) | **Yes** — explicit contract, not a bug |
| postgresql/mssql/athena/redshift→snowflake | any table with JSON/HStore/numeric-string columns | `distinct_count` | full canonical `nunique` | column excluded from the check entirely | `_canonicalization_risk_columns` | No safe cross-dialect SQL equivalent to `canonicalize_value` (T.5) | **Yes** — documented exclusion, not a silent wrong answer |
| any | any | `sample_hash` | ordered-independent by construction (single process) | two unordered per-engine queries may not return "the same" logical rows first | `_sample_hash_failures` / oracle's own `run_quality_checks` | Inherited from the oracle (§R.2.4.5), not introduced by hybrid_v1 | Not fixed here (out of scope — oracle has the same gap) |
| athena, redshift | — | all | — | — | — | Not reachable this pass (`SRC_4_HOST` unset, no Athena credentials) | Open, not fabricated |
