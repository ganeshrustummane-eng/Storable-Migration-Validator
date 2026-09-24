# 0014. Coalesce metadata extraction: column classification, key resolution, API access

**Status:** Proposed
**Date:** 2026-09-24
**Follows on from:** [0013](0013-silver-layer-validation-strategy.md) (why recompute-vs-materialize; what's reused)

## Context

ADR 0013 decided silver validation recomputes each Silver node's declared
transform against Bronze and diffs it against the materialized Silver table.
This ADR is the concrete spec for turning one Coalesce node's metadata
(`GET /api/v1/workspaces/{ws}/nodes/{nodeId}`) into inputs the existing
`CanonicalValidationPlan` → SQL/YAML generators already accept.

Worked from one real sample node (`INT_FACILITIES`, persistentStage,
`SILVER_EDGE.CONFORMED_RRADHAKR`, one upstream dependency
`BRONZE_EDGE.FACILITIES`). Rules below are inferred from that one shape —
flagged everywhere they're an assumption, not a fact, so the next sample
either confirms or corrects them before code is written.

## Decision

### 1. API access

- New env block, same pattern as `SRC_n_*`: `COALESCE_API_TOKEN`,
  `COALESCE_WORKSPACE_ID`. Token is a bearer token, long-lived — treat it
  like `SNOWFLAKE_PASSWORD`, not a bronze/silver connector credential
  (it's not a `SRC_n_TYPE=` block, Coalesce isn't a data source we
  validate *against*, it's metadata *about* the transform).
- One thin client, `GET /workspaces/{workspace_id}/nodes/{node_id}`,
  `Authorization: Bearer {token}`. No write operations, no other Coalesce
  endpoints needed for this — don't build a general Coalesce SDK.
- `--ssl-no-revoke` in the sample curl is a Windows/corporate-proxy curl
  flag, not a real Coalesce API requirement — a Python `requests` call
  doesn't need an equivalent; don't carry it over as a real setting.

### 2. Identity: which table is Bronze, which is Silver

From one node's metadata:

| Plan field | Source in JSON |
|---|---|
| Silver (target) database/schema/table | top-level `database`, `schema`, `name` |
| Bronze (source) database/schema/table | `metadata.sourceMapping[0].dependencies[0].locationName` (database), same entry's `nodeName` (table); schema is **not present in this payload** — assumption: same schema-naming convention as the rest of that Bronze location, needs confirming against a second sample or a Bronze-side node lookup |

`isMultisource: false` and one `sourceMapping[]` entry with one
`dependencies[]` entry is the only shape handled. A node with more than one
dependency (a join across two Bronze tables) or `isMultisource: true` is
**out of scope** — raise/flag for human review rather than guessing at a
multi-table plan. (`RelationshipSpec` already exists in
`CanonicalValidationPlan` for multi-table joins, so this isn't a dead end —
just not designed here.)

### 3. Column classification (the core logic)

Each entry in `metadata.columns[]` becomes one `ColumnMappingEntry`
(`src/core/validation_plan.py:159`), classified into exactly one of these
four buckets — this is the part that didn't exist for bronze and is the
real net-new logic:

| Bucket | Detection | Handling |
|---|---|---|
| **Passthrough** | `sources[0].transform == ""` and exactly one `columnReferences[0]` | Resolve the referenced `columnID` to a real column name (see §4). `match_method = "configured"`. Recomputed SQL = plain `"<ref_table>"."<ref_column>"`. Normal type-based normalization applies, same as bronze. |
| **Recomputable expression** | Non-empty `transform`, contains no `{{ }}` Jinja/macro syntax | Resolve any quoted `"Table"."Column"` references inside the expression string via the same `nodeID`/alias map (§4), substitute the real Bronze-qualified names, use the resulting SQL verbatim as the Bronze-side SELECT expression. Example from the sample: `SYS_VERSION`'s `ROW_NUMBER() OVER (PARTITION BY "FACILITIES"."ID" ORDER BY "FACILITIES"."UPDATED_AT")`. |
| **Macro-computed — skip** | `transform` contains `{{ ... }}` (a Coalesce/Jinja macro call, e.g. `ids_to_surrogate_key(...)`) | `skip_validation = True`, `skip_reason = "macro-expanded transform ({{ macro }}), not recomputable without Coalesce's macro engine"`. **Do not** hand-reimplement the macro's SQL to "match" it — that's exactly the speculative-abstraction trap ADR 0013 rejected. If a specific macro turns out to matter enough to validate, that's a future, narrowly-scoped ADR for *that macro*, not a general interpreter. |
| **Non-deterministic — existence-only** | Expression (raw or resolved) contains a non-deterministic function: `CURRENT_TIMESTAMP`, `CURRENT_DATE`, `RANDOM`, `UUID_STRING()`, etc. | Excluded from value comparison the same way Fivetran metadata columns are excluded today (`CLAUDE.md`'s Fivetran carve-out) — recomputing it now will never equal what Coalesce wrote at ETL run time. Check `NOT NULL` only, not value equality. In the sample: `SYS_CREATE_DATE`, `SYS_UPDATE_DATE` (`CAST(CURRENT_TIMESTAMP() AS TIMESTAMP)`). |

A column can't be both macro-computed *and* non-deterministic-excluded in
this schema (macros here are all deterministic surrogate-key hashes) — order
of the checks doesn't matter in practice, but detect non-determinism first
since it's the cheaper/more certain check.

### 4. Resolving `columnReferences` (cross-node column names)

`sources[].columnReferences[].columnID` is a UUID into an *upstream* node's
own `metadata.columns[]` list — the Silver node's payload alone doesn't
contain the Bronze column's name. Resolving it requires **one additional
API call per distinct upstream `nodeID`** referenced (in the sample, exactly
one: `323522c8-a433-4859-bb89-4ddcb48298f7`, aliased `FACILITIES` in
`sourceMapping[0].aliases`) — fetch that node, build a
`columnID → column name` map from its own `metadata.columns[]`, cache it for
the rest of this table's extraction (many silver columns reference the same
one or two upstream nodes).

The `aliases` map (`{"FACILITIES": "<nodeID>"}`) is what lets the
recomputed SQL use the same table alias the metadata's own `transform`
strings already use (`"FACILITIES"."ID"`) — so substitution is
name-for-name, not a rewrite, minimizing the chance of introducing a bug in
translation.

### 5. Primary/business key resolution — the one genuinely hard part

`isBusinessKey: true` marks a column as part of the Silver table's grain.
In the sample, that's `FACILITY_KEY` (a **macro-computed surrogate key** —
bucket 3, skip_validation) and `EFFECTIVE_FROM` (a **recomputable
expression** — bucket 2, `TO_TIMESTAMP_NTZ("FACILITIES"."_FIVETRAN_START")`).

**Problem:** the recomputed Bronze-side query has no `FACILITY_KEY` column
to join on — it doesn't exist in Bronze, it's a Silver-only computed value.
Using a skip-validation column as the join/PK key doesn't work.

**Rule:** when the business key includes a macro-computed column, fall back
to the plain source column(s) the macro's own argument list references —
parse the macro's argument, e.g. `ids_to_surrogate_key('EDGE',
['"FACILITIES"."ID"'])` → the underlying natural key is `FACILITIES.ID`,
which is already present as its own passthrough column, `FACILITY_ID`.
Use **that** (plus any non-computed `isBusinessKey` column, here
`EFFECTIVE_FROM`) as the actual PK for row matching
(`identity.source_primary_keys` / `target_primary_keys` in the plan).

This is a heuristic, not a certainty — mark any plan built this way
`requires_review: True` with `review_reasons: ["surrogate business key
resolved to natural-key fallback FACILITY_ID — confirm this is the correct
join grain"]`, so a human checks it once before it's trusted, same pattern
`CanonicalValidationPlan` already uses for AI-resolved ambiguity in bronze.

### 6. What this does *not* do (deliberately)

- No general Jinja/macro interpreter (see bucket 3 above).
- No support yet for multi-dependency / multi-source nodes, `overrideSQL`,
  or `customSQL` — all real fields in the schema, none seen in a populated
  sample yet. Detect and hard-stop with a clear "unsupported node shape"
  message rather than silently producing a wrong plan.
- No caching/persistence layer for fetched Coalesce metadata beyond
  the in-memory per-run `columnID` map in §4 — this runs at YAML-generation
  time, same cadence as bronze's mapping pipeline, not a background sync.

## Alternatives considered

- **Hand-maintain the Bronze↔Silver column mapping in a YAML file instead of
  calling the Coalesce API.** Rejected — that's the exact "silently drifts
  from reality" problem bronze's fuzzy/AI matching exists to avoid, except
  worse, because a human has to notice a Coalesce change and update the file
  manually. The API is the whole point of doing this at all.
- **Fetch and expand every referenced node recursively, no matter how deep,
  up front.** Rejected for v1 — the sample only ever needs one hop. Recurse
  only as far as an actual `columnReferences` chain requires; don't
  pre-fetch a whole lineage graph speculatively.

## Consequences

- Gets easier: once this extractor produces a `CanonicalValidationPlan`,
  everything downstream (SQL gen, YAML gen, `Project/main.py --layer_type
  silver`) is already built and already tested against bronze's use of the
  same plan object.
- Gets harder: this is the one piece of real net-new code, and it's parsing
  someone else's tool's JSON — the four-bucket classification in §3 and the
  key-fallback heuristic in §5 are the two places most likely to need a
  second real sample before they're provably right. Don't write the
  production version of §5's macro-argument parsing until at least one more
  sample node with a composite surrogate key confirms the pattern.
- Watch for: Coalesce node/workspace IDs and the bearer token in this ADR's
  source conversation are real, live credentials pasted into chat — rotate
  that token before this ships anywhere shared; it should never end up
  committed to a config file or example.
