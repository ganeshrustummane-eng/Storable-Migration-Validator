"""
A2A (Agent2Agent) protocol adapter.

Gemini Enterprise's agent.json ("AgentCard") registration expects the agent's
`url` to serve the A2A protocol directly (JSON-RPC 2.0 POST, method
"message/send") — confirmed by a live 404 from Gemini Enterprise when it
called this connector's base URL as an A2A endpoint. This connector's native
interface is REST/OpenAPI (see api.py); this module is a thin translation
layer only. It forwards the incoming A2A message text into the existing
GeminiAgent conversation loop (the same one /chat uses) and wraps the reply
back into an A2A-shaped JSON-RPC response — no tool-calling or validation
logic is duplicated here.

Written from general A2A protocol knowledge (JSON-RPC 2.0 envelope, Message/
Part objects with a `kind` discriminator) without a live reference to test
against, since capabilities.streaming=false in our AgentCard means Gemini
Enterprise should only need the synchronous "message/send" method. The part
discriminator is read defensively (`kind` or the older `type`) in case
Gemini Enterprise is on a different protocol draft than expected.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List

_AGENT_CARD_PATH = Path(__file__).resolve().parents[2] / "docs" / "hackathon" / "agent.json"

_FALLBACK_AGENT_CARD: Dict[str, Any] = {
    "name": "Migration Validator Connector",
    "version": "1.0.0",
    "description": (
        "Enterprise migration validation connector for Migration Validator "
        "(AI Data QA Tool). Federates PostgreSQL/MSSQL/Athena source systems "
        "with a Snowflake target."
    ),
    "protocolVersion": "1.0",
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["application/json"],
    "defaultOutputModes": ["application/json"],
    "skills": [],
}


def load_agent_card() -> Dict[str, Any]:
    """Serve the same AgentCard submitted during Gemini Enterprise registration."""
    try:
        return json.loads(_AGENT_CARD_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _FALLBACK_AGENT_CARD


def _extract_text(message: Dict[str, Any]) -> str:
    """Pull user text out of an A2A Message's `parts` list."""
    parts = message.get("parts") or []
    texts: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        discriminator = part.get("kind") or part.get("type")
        if discriminator in (None, "text") and "text" in part:
            texts.append(str(part["text"]))
    return "\n".join(texts).strip()


def _jsonrpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _jsonrpc_result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle_a2a_request(
    body: Dict[str, Any],
    run_chat: Callable[[str], Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Handle one A2A JSON-RPC request.

    `run_chat` is a callback `(message_text) -> {"text": str, ...}` supplied
    by the caller — wired to the same GeminiAgent instance /chat uses, so no
    tool-calling logic is duplicated here.
    """
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        request_id = body.get("id") if isinstance(body, dict) else None
        return _jsonrpc_error(request_id, -32600, "Invalid Request: expected a JSON-RPC 2.0 envelope")

    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method not in ("message/send", "tasks/send"):
        return _jsonrpc_error(request_id, -32601, f"Method not found: {method!r}")

    message = params.get("message") or {}
    user_text = _extract_text(message)
    if not user_text:
        return _jsonrpc_error(request_id, -32602, "Invalid params: no text content in message.parts")

    try:
        result = run_chat(user_text)
    except Exception as exc:  # noqa: BLE001 — must not 500 an A2A caller; report as JSON-RPC error
        return _jsonrpc_error(request_id, -32603, f"Internal error: {exc}")

    reply_text = result.get("text") or result.get("reply") or ""

    return _jsonrpc_result(request_id, {
        "kind": "message",
        "role": "agent",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": reply_text}],
    })
