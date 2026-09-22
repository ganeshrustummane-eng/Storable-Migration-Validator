# Gemini Integration

## Overview

Migration Validator integrates with Google Gemini through a **FastAPI connector** that exposes 24 structured tools via Gemini's function-calling API. The `GeminiAgent` class manages the full conversation loop, calling tools iteratively until the user's request is fully resolved.

---

## Integration Architecture

```
Gemini Client (google.com / Vertex AI)
         │
         │  1. User sends natural language message
         │  2. Gemini identifies tool to call
         │  3. Gemini sends function call JSON to connector
         │
         ▼
POST /tools/{tool_name}
{ "arguments": { ... } }
         │
         │  4. Connector authenticates request
         │  5. Connector checks authorization
         │  6. Connector dispatches to tool function
         │  7. Tool calls Migration Validator engine
         │
         ▼
Migration Validator Engine
(ValidationPipeline, PlanStore, ApprovalStore...)
         │
         │  8. Tool returns structured result
         │
         ▼
POST /tools/{tool_name} → Response JSON
         │
         │  9. Gemini formats response for user
         │  10. Gemini may call another tool (multi-round)
         │
         ▼
User receives natural language explanation
```

---

## GeminiAgent Class

**Location:** `src/gemini_connector/gemini_agent.py`

**Status:** Implemented (prototype)

### Initialization

```python
agent = GeminiAgent(
    model="gemini-2.5-flash",      # from GEMINI_MODEL env var
    api_key="...",               # from GOOGLE_API_KEY or GEMINI_API_KEY
    max_tool_rounds=10           # prevents infinite loops
)
```

### Conversation Loop

```python
result = agent.chat(
    user_message="Validate the customer migration",
    actor="jane.doe@company.com"
)
# Returns: {text: str, tool_calls: list, rounds: int}
```

The loop:
1. Sends conversation history + tools to Gemini
2. Gemini returns either text OR a tool call
3. If tool call: connector dispatches it, appends result to history
4. Loop repeats until Gemini returns text (max 10 rounds)

### Offline Fallback

When no Gemini API key is configured, `chat_offline()` parses keyword intent and returns pre-formatted responses from local data:

| Keywords | Response |
|----------|----------|
| `summary`, `status`, `attention` | Migration summary from PlanStore |
| `pending`, `review`, `approve` | Pending approvals from ApprovalStore |
| `metrics`, `roi`, `business` | Business metrics from MetricsTracker |
| `coverage`, `threshold` | Coverage report from plan data |
| `connection`, `source` | Discovered connections |

**Status:** Implemented (offline mode is a demo fallback — full Gemini requires `GOOGLE_API_KEY`)

---

## Tool Declarations

All 24 tools are registered in `TOOL_DECLARATIONS` — a list of Gemini-compatible function schemas.

Each declaration follows the Gemini function calling schema:

```json
{
  "name": "get_migration_summary",
  "description": "Returns an aggregated summary of migration status across all tables in a layer.",
  "parameters": {
    "type": "object",
    "properties": {
      "layer": {
        "type": "string",
        "description": "Medallion layer: bronze, silver, gold, or reporting",
        "enum": ["bronze", "silver", "gold", "reporting"]
      }
    },
    "required": ["layer"]
  }
}
```

### Tool Registration Endpoint

```
GET /tools
→ Returns: List of all 24 TOOL_DECLARATIONS in Gemini function-calling format
```

Gemini discovers tools on first connection and caches them for the session.

---

## System Prompt

The `SYSTEM_PROMPT` in `gemini_agent.py` instructs Gemini on:

- **Role:** Migration validation specialist using structured tools
- **Confidence policy:** Explain auto-accepted vs. queued-for-review mappings
- **Approval protocol:** Always require a human actor before write operations
- **Coverage guidance:** Use `get_coverage` with explicit threshold parameter
- **Workflow sequence:** discover → plan → review → approve → execute

---

## Multi-Round Tool Calling Example

```
User: "Validate the events table and explain any failures."

Round 1:
  Gemini → discover_connections()
  Result: {connections: [{name: "fms", type: "postgresql"}, {name: "dev_bronze", type: "snowflake"}]}

Round 2:
  Gemini → generate_validation_plan(source_table="events", layer="bronze")
  Result: {status: "complete", active_mappings: 38, pending_review: 0}

Round 3:
  Gemini → execute_validation(source_table="events", actor="jane.doe@corp.com")
  Result: {status: "WARNING", coverage: 99.1%, failed_checks: 1}

Round 4:
  Gemini → get_validation_failures(source_table="events")
  Result: {failures: [{column: "email", source_null_count: 12, target_null_count: 0}]}

Gemini (text):
  "Validation complete. 99.1% coverage across 38 columns.
   One issue found: the 'email' column has 12 NULLs in PostgreSQL
   but 0 NULLs in Snowflake. This suggests the Snowflake load
   may be filtering NULL email records. Recommend reviewing the
   Fivetran connector configuration for this table."
```

---

## Connector Endpoints Used by Gemini

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/tools` | GET | Discover all 24 tool schemas |
| `/tools/{tool_name}` | POST | Execute any tool |
| `/chat` | POST | Conversational agent (per-actor session) |
| `/health` | GET | Confirm connector is reachable |

---

## Token Efficiency Design

The connector is designed to minimize Gemini token consumption:

| Strategy | Implementation |
|----------|---------------|
| **Summary-first responses** | `get_migration_summary` returns stats, not raw data |
| **Pagination** | All list tools support `page` / `page_size` (max 200) |
| **ID references** | Plans and mappings referenced by ID, not inlined |
| **Compact schemas** | Nested objects flattened where possible |
| **No raw data dumps** | Connector never returns full dataset contents |
| **Tool-specific responses** | Each tool returns only the fields Gemini needs |

---

## Session Management

The connector maintains a per-actor agent cache:

```python
_agent_cache: Dict[str, GeminiAgent] = {}
```

Each actor (`jane.doe@company.com`) gets an isolated conversation history. The `/chat` endpoint accepts `reset: true` to clear history.

---

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `GOOGLE_API_KEY` | — | Primary Gemini API key |
| `GEMINI_API_KEY` | — | Alias for `GOOGLE_API_KEY` |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model to use |

---

## Python Integration Example

```python
import google.generativeai as genai

genai.configure(api_key="your-api-key")

# Get tool declarations from connector
import requests
tools_response = requests.get("http://localhost:8001/tools").json()

# Build Gemini tool config
tools = [genai.protos.Tool(
    function_declarations=[
        genai.protos.FunctionDeclaration(**t) for t in tools_response
    ]
)]

model = genai.GenerativeModel("gemini-2.5-flash", tools=tools)
chat = model.start_chat()

# Send a message
response = chat.send_message("What tables need review?")

# Handle tool calls
for part in response.parts:
    if fn := part.function_call:
        # Forward to connector
        result = requests.post(
            f"http://localhost:8001/tools/{fn.name}",
            json={"arguments": dict(fn.args)},
            headers={"Authorization": "Bearer your-token"}
        ).json()
        # Send result back to Gemini
        response = chat.send_message(
            genai.protos.Content(
                parts=[genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=fn.name, response=result
                    )
                )]
            )
        )
```

---

## DIAL Proxy Integration (AI Mapping Backend)

The validation pipeline uses EPAM DIAL as its AI backend for column mapping — separate from the Gemini conversational agent.

```
ValidationPipeline
  → ambiguous column: "cust_created_ts" ↔ "CREATED_AT"
  → calls DIAL proxy (ai-proxy.lab.epam.com)
  → DIAL routes to GPT-4o (or Claude/Gemini/Llama based on model selection)
  → returns: {match: "CREATED_AT", confidence: 0.88, transformation: "timestamp"}
  → ValidationPipeline records AI recommendation in CanonicalValidationPlan
```

Configuration:

```bash
DIAL_API_KEY=your-dial-key
DIAL_API_BASE=https://ai-proxy.lab.epam.com
DIAL_API_VERSION=2025-04-01-preview
DIAL_MODEL=gpt-4o
```

---

## Register as a Gemini Extension

This section documents how the deployed connector (Cloud Run service `migration-connector`)
is registered with **Gemini Enterprise** (Discovery Engine / Agentspace), distinct from the
classic Gemini API integration (`GeminiAgent`) described above.

### Deployed Endpoint

> **Status: deployed and verified** (region `us-central1`). Verified live on 2026-08-29 —
> `/health` returns `{"status":"ok","tools_available":24,"auth_mode":"static"}` (HTTP 200),
> `/tools` returns the full 24-tool list (HTTP 200), and the paired Streamlit review UI
> (`migration-webapp`) responds HTTP 200. All three require a Cloud Run identity token
> (`--allow-unauthenticated` is blocked by this project's org policy) — access is via IAM
> (`roles/run.invoker`), not the public internet.

```
Connector URL   : https://migration-connector-877936790636.us-central1.run.app
OpenAPI spec    : https://migration-connector-877936790636.us-central1.run.app/openapi.json
Tool list       : https://migration-connector-877936790636.us-central1.run.app/tools  (24 tools)
Health check    : https://migration-connector-877936790636.us-central1.run.app/health
Webapp (review) : https://migration-webapp-877936790636.us-central1.run.app
```

FastAPI auto-generates a full OpenAPI 3.1 document at `/openapi.json` — this is the artifact
Gemini Enterprise's custom connector/action registration consumes to discover available
operations, request/response schemas, and parameters. No hand-written spec is required.

### Two-Layer Authentication

Access to this connector is gated at two independent layers:

1. **Cloud Run IAM (transport layer) — required, since this project's org policy blocks
   `--allow-unauthenticated`.** `roles/run.invoker` is already granted on the `migration-connector`
   service (verified via `gcloud run services get-iam-policy migration-connector
   --region=us-central1`) to:

   ```
   serviceAccount:service-71784361107@gcp-sa-discoveryengine.iam.gserviceaccount.com   (Discovery Engine — Gemini Enterprise's caller identity)
   serviceAccount:connector-tester@hl2-gcpp-ccoe-ge-h-migrat-1646.iam.gserviceaccount.com  (used for manual identity-token testing)
   user:ganesh_rustummane@epam.com
   ```

   To replicate this on a fresh service:

   ```bash
   gcloud run services add-iam-policy-binding migration-connector \
     --region=us-central1 \
     --member="serviceAccount:service-71784361107@gcp-sa-discoveryengine.iam.gserviceaccount.com" \
     --role="roles/run.invoker"
   ```

2. **Application-level auth (`AUTH_MODE=static`).** Independent of the Cloud Run IAM gate, the
   connector itself enforces a bearer-token check (`CONNECTOR_API_TOKEN`, stored in Secret
   Manager) plus role-based authorization (`CONNECTOR_ROLES`) on every tool call — see
   [`docs/api/authentication.md`](../api/authentication.md). Gemini Enterprise's registration
   must supply this token as the `Authorization: Bearer <token>` header on every request.

### Registration Steps

1. Confirm the connector is reachable and returns the expected spec (adjust if using
   `AUTH_MODE=static` with a bearer token instead of Cloud Run IAM):

   ```bash
   curl -H "Authorization: Bearer $CONNECTOR_API_TOKEN" \
     <Connector URL>/openapi.json
   ```

2. Submit the connector URL, OpenAPI spec URL, and `CONNECTOR_API_TOKEN` value through the
   hackathon's connector-binding template/support channel, so Gemini Enterprise's admin console
   can bind this endpoint to the shared Gemini Enterprise application instance.

3. Once bound, verify end-to-end by issuing a natural-language request through Gemini Enterprise
   and confirming it resolves to a `/tools/{tool_name}` call against this connector (check
   `GET /audit` on the connector for the resulting audit log entry).
