# JIRA Integration - Quick Reference Card

## 🚀 5-Minute Setup

```bash
# 1. Get JIRA API token
https://id.atlassian.com/manage-profile/security/api-tokens

# 2. Add to .env
JIRA_URL=https://your-company.atlassian.net
JIRA_EMAIL=your.email@company.com
JIRA_API_TOKEN=your_token_here
JIRA_PROJECT_KEY=MIG

# 3. Test
python verify_jira_config.py --create-test-ticket
```

---

## 🎯 Common Commands

### Via Gemini Chat

```
"Create a JIRA ticket for the orders table validation failure"
"What's the status of MIG-123?"
"Reject the email mapping - wrong target column"
```

### Via CLI

```bash
# Test configuration
python verify_jira_config.py

# Run integration tests
python test_jira_integration.py --full
```

---

## 📋 Automatic Ticket Creation

| Trigger | When | Example |
|---------|------|---------|
| **Rejected Mapping** | You reject a column mapping | `MIG-101: Rejected Mapping: orders.email` |
| **Validation Failure** | Row counts don't match | `MIG-102: Validation Failure: orders (50 rows)` |
| **Critical Error** | Database connection fails | `MIG-103: Critical Error: Cannot connect` |

---

## 🛠️ Gemini Tools Available

### 1. create_jira_ticket
```python
create_jira_ticket(
    summary="Issue title",
    description="Detailed explanation",
    table="orders",
    labels=["high-priority"],
    priority="High"
)
```

### 2. get_jira_ticket_status
```python
get_jira_ticket_status(
    ticket_key="MIG-123"
)
```

### 3. reject_mapping (enhanced)
```python
reject_mapping(
    record_id="orders.email",
    actor="user@company.com",
    reason="Wrong target column",
    create_jira_ticket=True  # Auto-creates JIRA ticket
)
```

---

## 🔍 JIRA Query Examples

### All Migration Tickets
```jql
project = MIG 
  AND labels = "migration-validator"
ORDER BY priority DESC, created DESC
```

### Open Issues Only
```jql
project = MIG 
  AND labels = "migration-validator" 
  AND status != Done
ORDER BY created DESC
```

### High Priority Issues
```jql
project = MIG 
  AND labels = "migration-validator" 
  AND labels = "high-priority"
ORDER BY created DESC
```

### Rejected Mappings
```jql
project = MIG 
  AND labels = "rejected-mapping"
ORDER BY created DESC
```

### Validation Failures
```jql
project = MIG 
  AND labels = "validation-failure"
ORDER BY created DESC
```

---

## 🚫 Troubleshooting

| Error | Fix |
|-------|-----|
| "JIRA isn't configured" | Add JIRA_* variables to .env |
| "Jira returned 401" | Invalid token - generate new one |
| "Jira returned 404" | Wrong project key - check JIRA URL |
| "Could not reach Jira" | Check JIRA_URL (no trailing /) |

---

## 📊 Ticket Content

### Rejected Mapping Ticket
```
Summary: Rejected Mapping: orders.customer_email

📋 Mapping Details
❌ Rejection Reason
👤 Rejected By
📅 Rejected At
🔍 Next Steps
```

### Validation Failure Ticket
```
Summary: Validation Failure: orders (50 rows missing)

📊 Validation Results
🔍 Investigation Queries
🎯 Possible Causes
```

---

## 🎯 Workflow

```
1. Run validation via Gemini
   ↓
2. Issue detected
   ↓
3. JIRA ticket auto-created
   ↓
4. Team reviews in JIRA
   ↓
5. Engineer fixes issue
   ↓
6. Re-run validation
   ↓
7. Validation passes
   ↓
8. Close JIRA ticket
```

---

## 🔐 Security

✅ Tokens in .env (git-ignored)  
✅ HTTPS only  
✅ Never logged  
✅ Project-scoped  

---

## 📚 Documentation

- **Quick Start**: [jira-user-guide.md](jira-user-guide.md)
- **Technical**: [jira-integration.md](jira-integration.md)
- **Full Summary**: [JIRA_IMPLEMENTATION_SUMMARY.md](JIRA_IMPLEMENTATION_SUMMARY.md)
- **Config**: [environment.md](environment.md)

---

## 💡 Best Practices

1. ✅ Use descriptive project keys (MIG, MIGRATE, DW)
2. ✅ Create JIRA dashboard for migration tickets
3. ✅ Link tickets in commit messages
4. ✅ Set up team notifications
5. ✅ Review tickets weekly
6. ✅ Close tickets when validation passes

---

## 📞 Quick Help

```bash
# Configuration help
python verify_jira_config.py

# Test everything
python test_jira_integration.py --full

# Check what's configured
env | grep JIRA
```

---

## ✨ Features

| Feature | Status |
|---------|--------|
| Auto-ticket on rejection | ✅ |
| Manual ticket creation | ✅ |
| Ticket status checking | ✅ |
| Rich ticket content | ✅ |
| Audit trail | ✅ |
| Graceful degradation | ✅ |

---

**Need Help?** Check the docs or run:
```bash
python verify_jira_config.py
```

---

**Ready to start?** Run:
```bash
python verify_jira_config.py --create-test-ticket
```

🎉 Your first migration validation ticket awaits!
