# JIRA Integration Guide

## Overview

The Migration Validator integrates with JIRA Cloud to automatically create tickets for:
- **Rejected mappings** that need manual investigation
- **Validation failures** (row count mismatches, data quality issues)
- **Critical errors** during migration validation
- **High-priority reviews** requiring senior architect approval

This provides full traceability between migration issues and your project management system.

---

## 🔧 Configuration

### Step 1: Generate JIRA API Token

1. Go to [JIRA Account Settings](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click **Create API token**
3. Give it a name: `Migration Validator`
4. Copy the generated token (you won't see it again!)

### Step 2: Update `.env` File

Add these variables to your `.env` file:

```bash
# ── JIRA Integration (Optional) ──────────────────────────────────────────────
# Leave blank to disable JIRA ticket creation.
# JIRA Cloud instance URL (no trailing slash)
JIRA_URL=https://your-company.atlassian.net

# Your JIRA account email
JIRA_EMAIL=your.email@company.com

# API token from https://id.atlassian.com/manage-profile/security/api-tokens
JIRA_API_TOKEN=your_api_token_here

# Project key where tickets will be created (e.g., MIG, MIGRATE, DW)
JIRA_PROJECT_KEY=MIG

# Default issue type (Task, Bug, Story, etc.)
JIRA_ISSUE_TYPE=Task
```

### Step 3: Verify Configuration

Run the verification script:

```bash
python verify_jira_config.py
```

This will:
- Test JIRA connectivity
- Verify project access
- Create a test ticket (optional)

---

## 🎯 Use Cases

### 1. Rejected Mapping Creates Ticket

**Scenario:** A data engineer rejects a column mapping because it's incorrect.

```
User (via Gemini): "Reject the customer_email mapping. 
                    It should map to EMAIL_ADDRESS not EMAIL_ADDR."
```

**What happens:**
1. Gemini calls `reject_mapping()` tool
2. System automatically creates JIRA ticket:
   - **Summary:** `Rejected Mapping: orders.customer_email`
   - **Description:** Detailed rejection reason + mapping details
   - **Labels:** `migration-validator`, `rejected-mapping`, `orders`
3. Returns JIRA ticket key (e.g., `MIG-123`) to user
4. Logs ticket creation in audit trail

### 2. Validation Failure Creates Ticket

**Scenario:** Row count mismatch detected during validation.

```
User: "Run validation for orders table"
→ Source: 10,000 rows
→ Target: 9,987 rows
→ ❌ Mismatch detected
```

**What happens:**
1. System detects 13 missing rows
2. Automatically creates JIRA ticket:
   - **Summary:** `Validation Failure: orders (13 rows missing)`
   - **Description:** 
     - Source vs Target comparison
     - Potential causes
     - SQL queries to investigate
   - **Labels:** `migration-validator`, `validation-failure`, `high-priority`
3. Links ticket to validation result

### 3. Manual Ticket Creation via Gemini

**Scenario:** User wants to escalate a complex issue.

```
User: "Create a JIRA ticket for the payments table. 
       We need senior architect review for the currency mapping strategy."
```

**What happens:**
1. Gemini calls `create_jira_ticket()` tool
2. Creates ticket with user's description
3. Returns ticket URL for tracking

---

## 🛠️ Gemini Tools

### Available JIRA Tools for Gemini

#### 1. `create_jira_ticket`
```python
create_jira_ticket(
    summary="Brief issue description",
    description="Detailed explanation",
    labels=["migration-validator", "high-priority"],
    table="orders"  # optional
)
```

**Returns:**
```json
{
  "status": "ok",
  "jira_key": "MIG-123",
  "jira_url": "https://your-company.atlassian.net/browse/MIG-123",
  "message": "JIRA ticket created successfully"
}
```

#### 2. `get_jira_ticket_status`
```python
get_jira_ticket_status(ticket_key="MIG-123")
```

**Returns:**
```json
{
  "status": "ok",
  "key": "MIG-123",
  "summary": "Rejected Mapping: orders.customer_email",
  "status": "In Progress",
  "assignee": "john.doe@company.com",
  "url": "https://your-company.atlassian.net/browse/MIG-123"
}
```

---

## 📊 Automatic Ticket Creation Logic

### When Tickets Are Created Automatically

| Trigger | Condition | Priority | Labels |
|---------|-----------|----------|--------|
| **Rejected Mapping** | Human rejects any mapping | Medium | `rejected-mapping`, `{table}` |
| **Validation Failure** | Row count mismatch > 0.1% | High | `validation-failure`, `{table}` |
| **Critical Error** | Database connection failure | Highest | `critical-error` |
| **Review Required** | Confidence < 0.50 | Medium | `needs-review`, `{table}` |

### Ticket Content Template

**For Rejected Mappings:**
```
Summary: Rejected Mapping: {table}.{source_column}
Description:
  A column mapping was rejected during migration validation review.

  📋 Mapping Details:
  - Source: {source_db}.{table}.{source_column}
  - Target: {target_db}.{table}.{target_column}
  - Confidence: {confidence_score}
  - Match Method: {match_method}

  ❌ Rejection Reason:
  {rejection_reason}

  👤 Rejected By: {actor}
  📅 Rejected At: {timestamp}

  🔍 Next Steps:
  1. Review source and target column definitions
  2. Confirm correct target column in Snowflake
  3. Update mapping using modify_mapping tool
  4. Re-run validation

  🔗 Audit Trail: {audit_log_link}

Labels: migration-validator, rejected-mapping, {table}
```

**For Validation Failures:**
```
Summary: Validation Failure: {table} ({row_diff} rows missing)
Description:
  Row count validation failed for {table}.

  📊 Validation Results:
  - Source Rows: {source_count:,}
  - Target Rows: {target_count:,}
  - Difference: {row_diff:,} rows ({pct_diff}%)
  - Status: ❌ FAILED

  🔍 Investigation Queries:
  
  -- Check for NULL keys
  {null_check_query}

  -- Check for duplicates
  {duplicate_check_query}

  -- Sample missing records
  {missing_records_query}

  🎯 Possible Causes:
  1. Primary key mapping incorrect
  2. Source data filtered incorrectly
  3. Fivetran exclusion not applied
  4. Type coercion causing NULL values

  📋 Validation Plan: {validation_plan_link}
  🔗 Full Results: {results_link}

Labels: migration-validator, validation-failure, {table}, high-priority
```

---

## 🔐 Security & Permissions

### Required JIRA Permissions

Your JIRA user needs:
- **Browse Projects** permission
- **Create Issues** permission
- Access to the target project (e.g., `MIG`)

### Credential Storage

- ✅ API tokens stored in `.env` (git-ignored)
- ✅ Never logged or exposed via API
- ✅ Transmitted over HTTPS only
- ❌ Never stored in approval_store or audit_log

---

## 📈 Metrics & Reporting

### JIRA Integration Metrics

Track via `get_business_metrics()` tool:

```json
{
  "jira_integration": {
    "enabled": true,
    "tickets_created": 47,
    "tickets_by_type": {
      "rejected_mapping": 23,
      "validation_failure": 18,
      "critical_error": 2,
      "manual": 4
    },
    "average_resolution_time": "2.3 days"
  }
}
```

### Query JIRA for Migration Issues

```jql
project = MIG 
  AND labels = "migration-validator" 
  AND created >= -30d 
ORDER BY priority DESC, created DESC
```

---

## 🧪 Testing

### Test JIRA Integration

```bash
# Test 1: Verify configuration
python -c "from src.gemini_connector.jira_client import is_configured; print(f'JIRA configured: {is_configured()}')"

# Test 2: Create test ticket
python -c "
from src.gemini_connector.jira_client import create_ticket
result = create_ticket(
    'Test Ticket - Migration Validator',
    'This is a test ticket created by the migration validator.',
    ['test', 'migration-validator']
)
print(f'Created: {result}')
"

# Test 3: End-to-end test
python test_jira_integration.py
```

---

## 🚫 Disabling JIRA Integration

To disable automatic ticket creation:

1. **Remove JIRA variables from `.env`**, or
2. **Comment them out**, or
3. **Set `JIRA_INTEGRATION_ENABLED=false`**

When disabled:
- No tickets are created automatically
- `create_jira_ticket()` tool returns an informational message
- All other functionality works normally

---

## 📚 Related Documentation

- [Review Workflow](../human-in-the-loop/review-workflow.md)
- [Audit Trail](../human-in-the-loop/audit-trail.md)
- [Environment Variables](environment.md)
- [Security Architecture](../architecture/security-architecture.md)

---

## 🆘 Troubleshooting

### "JIRA isn't configured" Error

**Cause:** Missing or incomplete JIRA environment variables.

**Fix:**
```bash
# Check which variables are set
env | grep JIRA

# Ensure all required variables are present:
# JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY
```

### "Jira returned 401" Error

**Cause:** Invalid API token or email.

**Fix:**
1. Verify email matches your JIRA account
2. Generate a new API token
3. Update `.env` with new token

### "Jira returned 404" Error

**Cause:** Invalid project key.

**Fix:**
1. Go to your JIRA instance
2. Navigate to Projects
3. Copy the exact project key (e.g., `MIG`, not `migration`)
4. Update `JIRA_PROJECT_KEY` in `.env`

### "Could not reach Jira" Error

**Cause:** Network connectivity or incorrect URL.

**Fix:**
1. Verify `JIRA_URL` is correct (no trailing slash)
2. Test connectivity: `curl {JIRA_URL}/rest/api/3/myself`
3. Check firewall/proxy settings

---

## 💡 Best Practices

1. **Use descriptive project keys:** `MIG`, `MIGRATE`, or `DW` instead of generic `PROJ`
2. **Configure labels:** Use consistent labels for easy filtering
3. **Set default assignee:** Configure project to auto-assign to migration team
4. **Create dashboard:** Build JIRA dashboard for migration-validator tickets
5. **Link tickets:** Reference JIRA tickets in commit messages and PRs
6. **Archive resolved:** Automatically close tickets when validation passes

---

## Example: Complete Workflow

```bash
# 1. Configure JIRA
cat >> .env << EOF
JIRA_URL=https://mycompany.atlassian.net
JIRA_EMAIL=engineer@company.com
JIRA_API_TOKEN=abc123...
JIRA_PROJECT_KEY=MIG
EOF

# 2. Start connector
python start_connector.py

# 3. In Gemini chat:
User: "Run validation for orders table"
→ Validation runs
→ Detects 50 missing rows
→ Auto-creates MIG-456
→ Returns: "Validation failed. Created JIRA ticket MIG-456 for tracking."

# 4. Team resolves issue
→ Data engineer investigates via JIRA
→ Fixes source query
→ Updates JIRA ticket
→ Re-runs validation
→ Validation passes
→ System comments on JIRA ticket: "✅ Validation now passing"
```
