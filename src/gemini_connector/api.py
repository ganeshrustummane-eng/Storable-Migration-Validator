"""
Migration Intelligence Connector — Enterprise REST API
======================================================
FastAPI server exposing governed migration-validation tools to Gemini Enterprise.

Security architecture:
  1. Authentication (auth.py)  — JWT or static bearer token
  2. Authorization (authz.py)  — RBAC + fine-grained permissions
  3. Version check             — Optimistic concurrency for write operations
  4. Audit                     — Every write is recorded (audit.py)
  5. No credential exposure    — DB passwords / API keys never in responses

Endpoint groups:
  GET  /health                     — service health
  GET  /tools                      — tool declarations for Gemini registration
  POST /tools/{tool_name}          — dispatch any tool
  POST /chat                       — Gemini agent chat
  GET  /pending                    — pending human reviews
  GET  /summary/{layer}            — portfolio summary
  GET  /table/{source_table}       — plan + mappings + failures
  GET  /coverage                   — federated coverage query
  GET  /metrics                    — business metrics
  GET  /audit                      — audit log
  GET  /versions                   — entity version snapshot
  POST /approve/mapping/{id}       — approve a column mapping (idempotent)
  POST /approve/mappings/batch     — approve multiple mappings in one call
  POST /reject/mapping/{id}        — reject a column mapping
  POST /modify/mapping/{id}        — modify then approve a mapping
  POST /approve/rule/{rule_id}     — activate a learned rule (RULE_ADMIN)
  POST /approve/plan/{table}       — approve a validation plan
  POST /execute/validation/{table} — trigger a validation run (VALIDATION_OPERATOR)
  GET  /me                         — return caller's resolved identity and permissions

Run:
    uvicorn src.gemini_connector.api:app --reload --port 8001
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_SRC = Path(__file__).resolve().parents[1]
_ROOT = _SRC.parent
for _p in (str(_SRC), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

try:
    from fastapi import FastAPI, HTTPException, Header, Body, Request
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
except ImportError:
    raise ImportError(
        "FastAPI is required. Install: pip install fastapi uvicorn"
    )

from gemini_connector.tools import dispatch_tool, TOOL_FUNCTIONS
from gemini_connector.gemini_agent import TOOL_DECLARATIONS as _DECLS
from gemini_connector.a2a import load_agent_card, handle_a2a_request
from gemini_connector.audit import audit_logger, AuditRecord
from gemini_connector.approval_store import approval_store
from gemini_connector.version_store import version_store, VersionConflictError
from gemini_connector.auth import verify_bearer, AuthenticationError, AuthResult
from gemini_connector.authz import (
    require_permission, effective_permissions,
    Permission, AuthorizationError,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Migration Intelligence Connector",
    description=(
        "Gemini Enterprise connector for Migration Validator. "
        "Secured with JWT/OAuth authentication and fine-grained RBAC authorization."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
)

_WRITE_TOOLS = {
    "approve_mapping", "batch_approve_mappings", "reject_mapping", "modify_mapping",
    "approve_rule", "approve_plan", "execute_validation",
}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _auth(authorization: Optional[str]) -> AuthResult:
    """Validate bearer token → AuthResult, translate errors to HTTP 401."""
    try:
        return verify_bearer(authorization)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": exc.code, "message": exc.message},
        )


def _check(auth: AuthResult, perm: Permission, **resource_kwargs) -> None:
    """Check permission + resource access, translate errors to HTTP 403."""
    try:
        require_permission(auth, perm, **resource_kwargs)
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": exc.code, "message": exc.message},
        )


def _request_id(request: Request) -> str:
    return request.headers.get("X-Request-Id", str(uuid.uuid4())[:8])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ToolRequest(BaseModel):
    arguments: Dict[str, Any] = {}

class ChatRequest(BaseModel):
    message: str
    actor:   str = ""
    reset:   bool = False

class ApproveRequest(BaseModel):
    reason:           str = ""
    expected_version: int = 0     # 0 = "don't care / first write"

class ModifyRequest(BaseModel):
    new_target_column:       Optional[str] = None
    new_transformation_rule: Optional[str] = None
    reason:                  str = ""
    expected_version:        int = 0

class ExecuteRequest(BaseModel):
    reason:           str = ""
    expected_version: int = 0

class BatchApproveRequest(BaseModel):
    record_ids: List[str]
    reason:     str = ""


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status":          "ok",
        "service":         "Migration Intelligence Connector",
        "version":         "2.0.0",
        "tools_available": len(TOOL_FUNCTIONS),
        "auth_mode":       os.getenv("AUTH_MODE", "static"),
    }


# ---------------------------------------------------------------------------
# Identity introspection
# ---------------------------------------------------------------------------

@app.get("/me")
def me(authorization: Optional[str] = Header(default=None)):
    """Return the caller's resolved identity and effective permissions."""
    auth = _auth(authorization)
    return {
        "user_id":     auth.user_id,
        "actor":       auth.actor,
        "roles":       auth.roles,
        "permissions": effective_permissions(auth),
    }


# ---------------------------------------------------------------------------
# Tools catalogue (Gemini Enterprise registration)
# ---------------------------------------------------------------------------

@app.get("/tools")
def list_tools():
    return {"tools": _DECLS, "count": len(_DECLS)}


@app.post("/tools/{tool_name}")
def call_tool(
    tool_name: str,
    request: ToolRequest,
    req: Request,
    authorization: Optional[str] = Header(default=None),
):
    """
    Dispatch a tool by name.

    Read tools are open (auth still encouraged for audit).
    Write tools require Authorization + appropriate permission.
    """
    req_id = _request_id(req)

    if tool_name in _WRITE_TOOLS:
        auth = _auth(authorization)
        _perm = {
            "approve_mapping":        Permission.MAPPING_APPROVE,
            "batch_approve_mappings": Permission.MAPPING_APPROVE,
            "reject_mapping":         Permission.MAPPING_REJECT,
            "modify_mapping":         Permission.MAPPING_MODIFY,
            "approve_rule":      Permission.RULE_ACTIVATE,   # highest gate
            "approve_plan":      Permission.PLAN_APPROVE,
            "execute_validation": Permission.VALIDATION_EXECUTE,
        }[tool_name]
        _check(auth, _perm)
        if "actor" not in request.arguments or not request.arguments["actor"]:
            request.arguments["actor"] = auth.actor

    result = dispatch_tool(tool_name, request.arguments)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message", "Tool error"))
    return result


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

_agent_cache: Dict[str, Any] = {}


@app.post("/chat")
def chat(request: ChatRequest, req: Request):
    """
    Conversational Gemini agent.
    Read-only operations are open; write actions still require the actor to
    have appropriate permissions when the tool routes through /tools.
    """
    from gemini_connector.gemini_agent import GeminiAgent, is_gemini_configured

    session_key = request.actor or "default"
    if session_key not in _agent_cache or request.reset:
        _agent_cache[session_key] = GeminiAgent()

    agent: GeminiAgent = _agent_cache[session_key]

    result = (
        agent.chat(request.message, actor=request.actor)
        if is_gemini_configured()
        else agent.chat_offline(request.message)
    )
    return {
        "reply":      result["text"],
        "tool_calls": result.get("tool_calls", []),
        "rounds":     result.get("rounds", 0),
        "offline":    result.get("offline", False),
    }


# ---------------------------------------------------------------------------
# A2A (Agent2Agent) protocol bridge — for Gemini Enterprise's agent.json
# registration, which calls this connector's base URL as an A2A JSON-RPC
# endpoint rather than via /tools + OpenAPI discovery. See a2a.py for why
# this exists and what it does/doesn't guarantee.
# ---------------------------------------------------------------------------

@app.get("/.well-known/agent.json", include_in_schema=False)
def a2a_agent_card():
    return load_agent_card()


@app.post("/", include_in_schema=False)
async def a2a_entrypoint(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

    # Session key per A2A contextId, not a single shared constant — a hardcoded key here
    # meant every Gemini Enterprise conversation (from any user, any thread) accumulated
    # into the SAME GeminiAgent history forever, including stale tool-call errors from
    # long-past turns bleeding into unrelated new questions.
    context_id = None
    if isinstance(body, dict):
        context_id = ((body.get("params") or {}).get("message") or {}).get("contextId")
    session_key = f"a2a:{context_id}" if context_id else f"a2a:oneoff:{uuid.uuid4()}"

    def _run_chat(text: str) -> Dict[str, Any]:
        from gemini_connector.gemini_agent import GeminiAgent, is_gemini_configured

        if session_key not in _agent_cache:
            _agent_cache[session_key] = GeminiAgent()
        agent: GeminiAgent = _agent_cache[session_key]
        return agent.chat(text, actor="") if is_gemini_configured() else agent.chat_offline(text)

    return handle_a2a_request(body, _run_chat)


# ---------------------------------------------------------------------------
# Read endpoints (auth encouraged, not enforced — adjust per policy)
# ---------------------------------------------------------------------------

@app.get("/pending")
def get_pending(table: Optional[str] = None):
    return dispatch_tool("get_pending_reviews", {"table": table} if table else {})


@app.get("/summary/{layer}")
def get_summary(layer: str = "bronze"):
    return dispatch_tool("get_migration_summary", {"layer": layer})


@app.get("/coverage")
def get_coverage_endpoint(layer: str = "bronze", threshold: float = 95.0,
                          page: int = 1, page_size: int = 50):
    return dispatch_tool("get_coverage", {
        "layer": layer, "threshold": threshold,
        "page": page, "page_size": page_size,
    })


@app.get("/table/{source_table}")
def get_table_info(source_table: str, layer: str = "bronze"):
    return {
        "plan":            dispatch_tool("get_table_mapping",      {"source_table": source_table, "layer": layer}),
        "column_mappings": dispatch_tool("get_column_mappings",    {"source_table": source_table, "layer": layer}),
        "failures":        dispatch_tool("get_validation_failures", {"source_table": source_table, "layer": layer}),
    }


@app.get("/metrics")
def get_metrics():
    return dispatch_tool("get_business_metrics", {})


@app.get("/audit")
def get_audit(n: int = 50):
    records = audit_logger.recent(n)
    return {"count": len(records), "records": [r.to_dict() for r in records]}


@app.get("/versions")
def get_versions():
    """Return current version for every tracked entity (debugging / UI)."""
    return {"versions": version_store.all_versions()}


# ---------------------------------------------------------------------------
# Write endpoints — full auth / authz / version / audit chain
# ---------------------------------------------------------------------------

@app.post("/approve/mapping/{record_id}")
def approve_mapping_endpoint(
    record_id: str,
    body: ApproveRequest,
    req: Request,
    authorization: Optional[str] = Header(default=None),
):
    auth   = _auth(authorization)
    req_id = _request_id(req)
    _check(auth, Permission.MAPPING_APPROVE)

    # Optimistic concurrency
    entity_key = f"mapping/{record_id}"
    try:
        new_ver = version_store.check_and_bump(entity_key, body.expected_version)
    except VersionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": exc.code, "message": exc.message},
        )

    result = dispatch_tool("approve_mapping", {
        "record_id": record_id,
        "actor":     auth.actor,
        "reason":    body.reason,
    })
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))

    audit_logger.log(AuditRecord(
        action       = "APPROVE_MAPPING",
        entity_type  = "mapping",
        entity_id    = record_id,
        actor        = auth.actor,
        user_id      = auth.user_id,
        request_id   = req_id,
        previous     = {"status": "pending"},
        new_state    = {"status": "approved"},
        reason       = body.reason,
        plan_version = new_ver,
    ))
    return {**result, "plan_version": new_ver}


@app.post("/approve/mappings/batch")
def batch_approve_mappings_endpoint(
    body: BatchApproveRequest,
    req: Request,
    authorization: Optional[str] = Header(default=None),
):
    """Approve multiple pending mappings in a single round-trip."""
    auth   = _auth(authorization)
    req_id = _request_id(req)
    _check(auth, Permission.MAPPING_APPROVE)

    result = dispatch_tool("batch_approve_mappings", {
        "record_ids": body.record_ids,
        "actor":      auth.actor,
        "reason":     body.reason,
    })
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))

    audit_logger.log(AuditRecord(
        action      = "BATCH_APPROVE_MAPPINGS",
        entity_type = "mapping",
        entity_id   = f"batch:{len(body.record_ids)}_records",
        actor       = auth.actor,
        user_id     = auth.user_id,
        request_id  = req_id,
        new_state   = {"approved": result.get("approved"), "skipped": result.get("skipped")},
        reason      = body.reason,
    ))
    return result


@app.post("/reject/mapping/{record_id}")
def reject_mapping_endpoint(
    record_id: str,
    body: ApproveRequest,
    req: Request,
    authorization: Optional[str] = Header(default=None),
):
    auth   = _auth(authorization)
    req_id = _request_id(req)
    _check(auth, Permission.MAPPING_REJECT)

    if not body.reason:
        raise HTTPException(status_code=400, detail="A reason is required to reject a mapping.")

    entity_key = f"mapping/{record_id}"
    try:
        new_ver = version_store.check_and_bump(entity_key, body.expected_version)
    except VersionConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message})

    result = dispatch_tool("reject_mapping", {
        "record_id": record_id, "actor": auth.actor, "reason": body.reason,
    })
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))

    audit_logger.log(AuditRecord(
        action       = "REJECT_MAPPING",
        entity_type  = "mapping",
        entity_id    = record_id,
        actor        = auth.actor,
        user_id      = auth.user_id,
        request_id   = req_id,
        previous     = {"status": "pending"},
        new_state    = {"status": "rejected"},
        reason       = body.reason,
        plan_version = new_ver,
    ))
    return {**result, "plan_version": new_ver}


@app.post("/modify/mapping/{record_id}")
def modify_mapping_endpoint(
    record_id: str,
    body: ModifyRequest,
    req: Request,
    authorization: Optional[str] = Header(default=None),
):
    auth   = _auth(authorization)
    req_id = _request_id(req)
    _check(auth, Permission.MAPPING_MODIFY)

    entity_key = f"mapping/{record_id}"
    try:
        new_ver = version_store.check_and_bump(entity_key, body.expected_version)
    except VersionConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message})

    result = dispatch_tool("modify_mapping", {
        "record_id":               record_id,
        "actor":                   auth.actor,
        "new_target_column":       body.new_target_column,
        "new_transformation_rule": body.new_transformation_rule,
        "reason":                  body.reason,
    })
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))

    audit_logger.log(AuditRecord(
        action       = "MODIFY_MAPPING",
        entity_type  = "mapping",
        entity_id    = record_id,
        actor        = auth.actor,
        user_id      = auth.user_id,
        request_id   = req_id,
        new_state    = {
            "target_column": body.new_target_column,
            "rule":          body.new_transformation_rule,
            "status":        "modified",
        },
        reason       = body.reason,
        plan_version = new_ver,
    ))
    return {**result, "plan_version": new_ver}


@app.post("/approve/rule/{rule_id}")
def approve_rule_endpoint(
    rule_id: str,
    body: ApproveRequest,
    req: Request,
    authorization: Optional[str] = Header(default=None),
):
    auth   = _auth(authorization)
    req_id = _request_id(req)
    # Rule activation requires RULE_ACTIVATE permission (RULE_ADMIN or ADMIN only)
    _check(auth, Permission.RULE_ACTIVATE)

    entity_key = f"rule/{rule_id}"
    try:
        new_ver = version_store.check_and_bump(entity_key, body.expected_version)
    except VersionConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message})

    result = dispatch_tool("approve_rule", {
        "rule_id": rule_id, "actor": auth.actor, "reason": body.reason,
    })
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))

    audit_logger.log(AuditRecord(
        action       = "APPROVE_RULE",
        entity_type  = "rule",
        entity_id    = rule_id,
        actor        = auth.actor,
        user_id      = auth.user_id,
        request_id   = req_id,
        previous     = {"status": "draft"},
        new_state    = {"status": "active"},
        reason       = body.reason,
        rule_id      = rule_id,
        plan_version = new_ver,
    ))
    return {**result, "plan_version": new_ver}


@app.post("/approve/plan/{source_table}")
def approve_plan_endpoint(
    source_table: str,
    body: ApproveRequest,
    req: Request,
    layer: str = "bronze",
    authorization: Optional[str] = Header(default=None),
):
    auth   = _auth(authorization)
    req_id = _request_id(req)
    _check(auth, Permission.PLAN_APPROVE)

    entity_key = f"plan/{layer}/{source_table}"
    try:
        new_ver = version_store.check_and_bump(entity_key, body.expected_version)
    except VersionConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message})

    result = dispatch_tool("approve_plan", {
        "source_table": source_table, "layer": layer,
        "actor": auth.actor, "reason": body.reason,
    })
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))

    audit_logger.log(AuditRecord(
        action       = "APPROVE_PLAN",
        entity_type  = "plan",
        entity_id    = entity_key,
        actor        = auth.actor,
        user_id      = auth.user_id,
        request_id   = req_id,
        new_state    = {"approval_status": "approved"},
        reason       = body.reason,
        table        = source_table,
        plan_version = new_ver,
    ))
    return {**result, "plan_version": new_ver}


@app.post("/execute/validation/{source_table}")
def execute_validation_endpoint(
    source_table: str,
    body: ExecuteRequest,
    req: Request,
    layer: str = "bronze",
    authorization: Optional[str] = Header(default=None),
):
    auth   = _auth(authorization)
    req_id = _request_id(req)
    _check(auth, Permission.VALIDATION_EXECUTE)

    result = dispatch_tool("execute_validation", {
        "source_table": source_table, "layer": layer, "actor": auth.actor,
    })
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))

    audit_logger.log(AuditRecord(
        action      = "EXECUTE_VALIDATION",
        entity_type = "validation_run",
        entity_id   = f"{layer}/{source_table}",
        actor       = auth.actor,
        user_id     = auth.user_id,
        request_id  = req_id,
        reason      = body.reason,
        table       = source_table,
    ))
    return result
