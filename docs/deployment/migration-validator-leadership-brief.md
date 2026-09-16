# Migration Validator: Client Approval Proposal

## Executive Summary

Migration Validator is an end-to-end data quality engineering solution for database migration. It validates PostgreSQL, MSSQL, Athena, and Redshift sources against Snowflake targets, then provides row-level evidence for migration sign-off.

We request approval to run the validator in the client cloud environment and to use a client-managed Claude Code API key or subscription for limited AI-assisted activities.

## What the Project Does

- Discovers source and target schemas and detects schema drift.
- Generates single-table or batch validation configurations.
- Applies exclusions for system, technical, and user-selected columns.
- Uses a governed Rule Book for data types, normalization, transformations, and reusable learned rules.
- Supports filters, batches, primary-key comparisons, and multi-table joins.
- Generates dialect-specific SQL for PostgreSQL, MSSQL, Athena, Redshift, and Snowflake.
- Runs row-count and data validation, including missing, extra, and changed records.
- Produces CSV results, pass/fail status, mismatch percentages, history, and trend views.
- Supports review queues, approval records, notifications, and Jira evidence.

## How Validation Works

1. Connect to approved source and target systems.
2. Extract schemas and apply exclusions and normalization rules.
3. Match columns through exact, normalized, fuzzy, and AI-assisted methods.
4. Build one approved validation plan for SQL and YAML generation.
5. Validate equivalent filters and join conditions on both source and target sides.
6. Run read-only count and data queries.
7. Join results by primary key and compare canonical values.
8. Record row-level failures with source and target details.
9. Route uncertain mappings and rules to human review.
10. Publish auditable results for remediation and migration approval.

## AI Usage and Claude Code Efficiency

AI is used only where it adds value: unresolved column mapping, transformation recommendations, natural-language rule interpretation, and dialect-specific SQL preparation. Exact matches, rule application, joins, query execution, row comparison, thresholds, pass/fail status, and audit recording are deterministic.

Claude Code access can be used efficiently by sending focused schema metadata and validation requirements, reusing approved Rule Book patterns, selecting the required model, and applying usage quotas. AI does not approve mappings or decide migration success. Every uncertain recommendation remains subject to human review.

## Data Protection and Security

- Validator database credentials are read-only and least-privilege.
- The tool never updates, deletes, or moves business data.
- AI receives schema and validation context required for its task, not full source or target result sets.
- API keys, passwords, and connection strings stay in managed secrets and never enter logs or audit records.
- Authentication, role-based permissions, resource allowlists, and service-account controls restrict access.
- AI cannot self-approve mappings, rules, plans, or executions.
- Approved changes use version control and optimistic concurrency checks.
- Audit records are append-only and capture approvals, rule changes, plan approvals, and executions without sensitive payloads.
- Network, model, quota, retention, and billing controls remain under client governance.

## Expected Benefits

Migration Validator replaces repeated manual SQL and spreadsheet comparison with repeatable, scalable checks. It reduces validation effort, finds defects earlier, preserves DQE ownership, supports faster remediation, and creates consistent evidence across migration runs and environments.

## Client Approval Requested
After approval, we will configure connectivity, run a controlled pilot, review results with the client DQE team, and expand usage based on agreed acceptance criteria.
