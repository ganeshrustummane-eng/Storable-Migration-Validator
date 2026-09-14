# JIRA Integration - Quick User Guide

## What is JIRA Integration?

JIRA integration automatically creates tickets in your JIRA project when:
- ❌ You reject a column mapping
- ⚠️ Validation finds row count mismatches
- 🔴 Critical errors occur during migration
- 🤔 Low-confidence mappings need expert review

This ensures **nothing falls through the cracks** during your migration.

---

## 📋 Quick Setup (5 minutes)

### Step 1: Get Your JIRA API Token

1. Go to: https://id.atlassian.com/manage-profile/security/api-tokens
2. Click **"Create API token"**
3. Name it: `Migration Validator`
4. **Copy the token** (you won't see it again!)

### Step 2: Update Your `.env` File

Open your `.env` file and add:

```bash
JIRA_URL=https://your-company.atlassian.net
JIRA_EMAIL=your.email@company.com
JIRA_API_TOKEN=paste_your_token_here
JIRA_PROJECT_KEY=MIG
```

Replace:
- `your-company` → Your JIRA workspace name
- `your.email@company.com` → Your JIRA account email
- `paste_your_token_here` → The API token from Step 1
- `MIG` → Your migration project key (check JIRA for the correct key)

### Step 3: Test It

```bash
python verify_jira_config.py --create-test-ticket
```

✅ If successful, you'll see a test ticket created in JIRA!

---

## 🎯 How to Use It

### 1. Via Gemini Chat

**Example 1: Reject a mapping and create ticket**
```
You: "Reject the customer_email mapping. It should map to EMAIL_ADDRESS not EMAIL_ADDR."

Gemini: ✅ Mapping rejected. JIRA ticket MIG-123 created automatically.
```

**Example 2: Create a custom ticket**
```
You: "Create a JIRA ticket for the payments table. 
      We need senior architect review for the currency mapping strategy."

Gemini: ✅ JIRA ticket MIG-124 created: 
        "Review Required: payments table currency mapping"
```

**Example 3: Check ticket status**
```
You: "What's the status of MIG-123?"

Gemini: 📋 Ticket MIG-123:
        - Status: In Progress
        - Assigned to: john.doe@company.com
        - Summary: Rejected Mapping: orders.customer_email
```

### 2. Automatic Creation

JIRA tickets are **automatically created** when:

| Scenario | When | Example |
|----------|------|---------|
| **Rejected Mapping** | You reject any column mapping | `MIG-101: Rejected Mapping: orders.email` |
| **Validation Failure** | Row counts don't match | `MIG-102: Validation Failure: orders (50 rows missing)` |
| **Critical Error** | Database connection fails | `MIG-103: Critical Error: Cannot connect to source` |

### 3. Via Web UI

In the **Review & Approve** tab:
1. Select a mapping
2. Click **"Reject"**
3. Enter rejection reason
4. ✅ JIRA ticket auto-created!

---

## 📊 Ticket Contents

### For Rejected Mappings

```
Summary: Rejected Mapping: orders.customer_email

Description:
  📋 Mapping Details:
  - Source: postgres.orders.customer_email
  - Target: snowflake.orders.email_addr
  - Confidence: 0.78

  ❌ Rejection Reason:
  Wrong target column - should map to EMAIL_ADDRESS not EMAIL_ADDR

  👤 Rejected By: jane.doe@company.com
  📅 Rejected At: 2026-09-01 14:30:00

  🔍 Next Steps:
  1. Review source and target column definitions
  2. Confirm correct target column in Snowflake
  3. Update mapping using modify_mapping tool
  4. Re-run validation

Labels: migration-validator, rejected-mapping, orders
```

### For Validation Failures

```
Summary: Validation Failure: orders (50 rows missing)

Description:
  📊 Validation Results:
  - Source Rows: 10,000
  - Target Rows: 9,950
  - Difference: 50 rows (0.5%)
  - Status: ❌ FAILED

  🎯 Possible Causes:
  1. Primary key mapping incorrect
  2. Source data filtered incorrectly
  3. Fivetran exclusion not applied
  4. Type coercion causing NULL values

Labels: migration-validator, validation-failure, orders, high-priority
```

---

## 💡 Best Practices

### 1. Use Descriptive Project Keys
✅ Good: `MIG`, `MIGRATE`, `DW`, `DATA_MIG`  
❌ Bad: `PROJ`, `TEST`, `ABC`

### 2. Create a JIRA Dashboard
Track all migration tickets in one place:
```jql
project = MIG 
  AND labels = "migration-validator" 
  AND status != Done
ORDER BY priority DESC
```

### 3. Set Up Notifications
Configure JIRA to notify:
- 📧 Migration team lead on ticket creation
- 📧 Original reporter on status change
- 📧 Assignee on comments

### 4. Link to PRs
Reference JIRA tickets in commit messages:
```bash
git commit -m "Fix customer_email mapping - MIG-123"
```

---

## 🚫 Disabling JIRA (Optional)

Don't want JIRA tickets? Simply:

**Option 1:** Remove JIRA variables from `.env`
```bash
# Comment out or delete these lines:
# JIRA_URL=...
# JIRA_EMAIL=...
# JIRA_API_TOKEN=...
# JIRA_PROJECT_KEY=...
```

**Option 2:** Reject mapping without ticket
```python
# In code or via API:
reject_mapping(
    record_id="orders.email",
    actor="jane@company.com",
    reason="Wrong mapping",
    create_jira_ticket=False  # ← Disable ticket creation
)
```

---

## 🆘 Common Issues

### ❌ "JIRA isn't configured"

**Problem:** Environment variables not set  
**Fix:** Check your `.env` file has all 4 required variables

### ❌ "Jira returned 401"

**Problem:** Invalid API token  
**Fix:** 
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Create a new token
3. Update `JIRA_API_TOKEN` in `.env`

### ❌ "Jira returned 404"

**Problem:** Wrong project key  
**Fix:**
1. Go to your JIRA dashboard
2. Check the project key (it's in the URL: `/browse/MIG`)
3. Update `JIRA_PROJECT_KEY` in `.env`

### ❌ "Could not reach Jira"

**Problem:** Network or URL issue  
**Fix:**
1. Verify `JIRA_URL` is correct (no trailing `/`)
2. Test: `curl https://your-company.atlassian.net`
3. Check firewall/VPN settings

---

## 📞 Support

Need help?
- 📚 Full docs: [jira-integration.md](jira-integration.md)
- 🔧 Test tool: `python verify_jira_config.py`
- 📋 Environment docs: [environment.md](environment.md)

---

## 🎯 Example Workflow

```
Day 1: Setup JIRA integration
├─ Get API token (2 min)
├─ Update .env file (1 min)
└─ Test connection (1 min)

Day 2-30: Run validation
├─ Gemini: "Validate orders table"
│  └─ ❌ 50 rows missing
│      └─ Auto-creates: MIG-201
│
├─ Engineer investigates MIG-201
│  └─ Finds missing WHERE clause in source query
│      └─ Updates query
│          └─ Re-runs validation
│              └─ ✅ Passes
│                  └─ Comments on MIG-201: "Fixed"
│
└─ Gemini: "Validate customers table"
   └─ Mapping rejected: email → email_addr (wrong)
       └─ Auto-creates: MIG-202
           └─ Engineer fixes mapping
               └─ Validation passes
                   └─ Closes MIG-202

Result: Full audit trail + zero missed issues!
```

---

## ✨ Summary

| Feature | Benefit |
|---------|---------|
| **Auto-ticket creation** | Nothing gets lost or forgotten |
| **Full traceability** | Link every issue to JIRA |
| **Team visibility** | Everyone sees migration blockers |
| **Audit compliance** | Complete history in JIRA + logs |
| **Easy setup** | 5 minutes to configure |

Ready to get started? Run:
```bash
python verify_jira_config.py --create-test-ticket
```

🎉 **Your first migration validation ticket awaits!**
