"""
Minimal Coalesce.io REST client — read-only metadata access for Silver-layer
validation (see docs/decisions/0013 and 0014). Fetches one node's metadata
so `src/silver/coalesce_plan_builder.py` can turn it into a
CanonicalValidationPlan. No write operations, no other Coalesce endpoints —
don't grow this into a general Coalesce SDK.

Optional: if COALESCE_* env vars aren't set, callers get a clear
CoalesceNotConfiguredError instead of a confusing network failure.
"""
import os

import requests

# ponytail: base URL is the standard public Coalesce API host, not confirmed
# against this org's actual tenant — verify before first live use.
COALESCE_API_BASE = os.getenv("COALESCE_API_BASE", "https://app.coalescesoftware.io/api/v1").rstrip("/")
COALESCE_API_TOKEN = os.getenv("COALESCE_API_TOKEN", "")
COALESCE_WORKSPACE_ID = os.getenv("COALESCE_WORKSPACE_ID", "")


class CoalesceNotConfiguredError(Exception):
    pass


class CoalesceError(Exception):
    pass


def is_configured() -> bool:
    return bool(COALESCE_API_TOKEN and COALESCE_WORKSPACE_ID)


def _get(path: str, params: dict = None) -> dict:
    if not is_configured():
        raise CoalesceNotConfiguredError(
            "Coalesce isn't configured — set COALESCE_API_TOKEN and "
            "COALESCE_WORKSPACE_ID in .env to enable Silver-layer metadata extraction."
        )
    try:
        resp = requests.get(
            f"{COALESCE_API_BASE}{path}", params=params,
            headers={"Authorization": f"Bearer {COALESCE_API_TOKEN}", "Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise CoalesceError(f"Could not reach Coalesce: {exc}") from exc
    if not resp.ok:
        raise CoalesceError(f"Coalesce returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def get_node(workspace_id: str, node_id: str) -> dict:
    """Full metadata for one Coalesce node (`GET /workspaces/{ws}/nodes/{node_id}`).

    Pass `workspace_id=""` (falsy) to fall back to COALESCE_WORKSPACE_ID.
    Raises CoalesceNotConfiguredError if env vars/workspace_id are missing, or
    CoalesceError on any API failure.
    """
    ws = workspace_id or COALESCE_WORKSPACE_ID
    if not ws:
        raise CoalesceNotConfiguredError(
            "No workspace_id given and COALESCE_WORKSPACE_ID isn't set."
        )
    return _get(f"/workspaces/{ws}/nodes/{node_id}")
