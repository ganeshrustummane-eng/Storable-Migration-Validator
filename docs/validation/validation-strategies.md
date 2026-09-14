# Validation Strategies

## Two Complementary Strategies

Migration Validator implements two complementary validation strategies that can be run independently or together.

---

## Strategy 1: Count Validation

**Purpose:** Verify that the total number of rows migrated from source matches Snowflake.

**When to use:** Fast sanity check. Run first to detect bulk data loss.

**SQL pattern:**

```sql
-- Source (PostgreSQL)
SELECT COUNT(*) AS source_row_count
FROM public.customer;

-- Target (Snowflake)
SELECT COUNT(*) AS target_row_count
FROM DEV_SITELINK_BRONZE.PUBLIC.CUSTOMER
WHERE _FIVETRAN_ACTIVE = TRUE;
```

**Output config:** `config/<layer>/count_validation/<layer>.yaml`

**Interpretation:**
- Equal counts: row count validation passes
- Source > Target: rows dropped during migration (data loss)
- Source < Target: duplicate rows in target or different active/deleted filtering

**Note:** The Fivetran SCD2 pattern means Snowflake tables can have more rows than source (historical versions). The `WHERE _FIVETRAN_ACTIVE = TRUE` filter selects only current rows.

---

## Strategy 2: Data Validation

**Purpose:** Compare actual column values row-by-row between source and target.

**When to use:** Deep validation after count check passes. Detects type coercion errors, NULL/empty mismatches, encoding differences.

**SQL pattern:**

```sql
-- Source (PostgreSQL)
SELECT
  CAST(id AS TEXT) AS id_normalized,
  COALESCE(CAST(email AS TEXT), '<<NULL>>') AS email_normalized,
  COALESCE(
    CASE WHEN active = true THEN '1'
         WHEN active = false THEN '0'
         ELSE NULL END,
  '<<NULL>>') AS active_normalized,
  COALESCE(TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI:SS'), '<<NULL>>') AS created_at_normalized
FROM public.customer
ORDER BY id;

-- Target (Snowflake)
SELECT
  CAST(ID AS STRING) AS id_normalized,
  COALESCE(CAST(EMAIL AS STRING), '<<NULL>>') AS email_normalized,
  COALESCE(
    CASE WHEN ACTIVE = TRUE THEN '1'
         WHEN ACTIVE = FALSE THEN '0'
         ELSE NULL END,
  '<<NULL>>') AS active_normalized,
  COALESCE(TO_VARCHAR(CREATED_AT, 'YYYY-MM-DD HH24:MI:SS'), '<<NULL>>') AS created_at_normalized
FROM DEV_SITELINK_BRONZE.PUBLIC.CUSTOMER
WHERE _FIVETRAN_ACTIVE = TRUE
ORDER BY ID;
```

**Output config:** `config/<layer>/data_validation/<table>.yaml`

**Join strategy:** Primary key join (`pksourcecolumn` / `pktargetcolumn`). Each normalized column is compared. Mismatches are recorded with source and target values.

---

## Medallion Layer Coverage

| Layer | Count Validation | Data Validation | Notes |
|-------|-----------------|-----------------|-------|
| Bronze | ✓ | ✓ | Raw ingest — validate all columns |
| Silver | ✓ | ✓ | Cleansed — validate transformations |
| Gold | ✓ | ✓ | Aggregated — validate business metrics |
| Reporting | Planned | Planned | Derived tables |

---

## Validation Status Values

| Status | Meaning |
|--------|---------|
| `PASS` | All checks within tolerance |
| `WARNING` | Minor differences detected or review is required |
| `FAIL` | Significant mismatches detected |
| `ERROR` | Validation could not complete (connection error, SQL error) |

---

## Coverage Calculation

```
coverage_pct = active_mappings / (active_mappings + unmatched_source_columns) × 100
```

Where:
- `active_mappings` = columns successfully matched (any match method)
- `unmatched_source_columns` = columns in source with no target match
- Excluded/skipped columns are not counted in either numerator or denominator

Coverage below the configured review threshold triggers a warning in the migration summary.
