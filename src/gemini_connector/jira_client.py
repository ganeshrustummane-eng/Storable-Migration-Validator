"""
Minimal Jira Cloud REST client — raise a ticket directly from Review &
Approve when a mapping/rule is rejected or otherwise needs follow-up.
Optional: if JIRA_* env vars aren't set, callers get a clear
JiraNotConfiguredError instead of a confusing network failure.
"""
import os

import requests

JIRA_URL = os.getenv("JIRA_URL", "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "")
JIRA_ISSUE_TYPE = os.getenv("JIRA_ISSUE_TYPE", "Task")


class JiraNotConfiguredError(Exception):
    pass


class JiraError(Exception):
    pass


def is_configured() -> bool:
    return bool(JIRA_URL and JIRA_EMAIL and JIRA_API_TOKEN and JIRA_PROJECT_KEY)


def _get(path: str, params: dict = None) -> dict:
    if not is_configured():
        raise JiraNotConfiguredError("Jira isn't configured.")
    try:
        resp = requests.get(
            f"{JIRA_URL}{path}", params=params,
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise JiraError(f"Could not reach Jira: {exc}") from exc
    if not resp.ok:
        raise JiraError(f"Jira returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def get_my_tickets(jql_extra: str = "") -> list:
    """Open tickets assigned to the current user in the configured project.

    Returns list of {key, summary, status, priority, url}.
    """
    jql = f"project = {JIRA_PROJECT_KEY} AND assignee = currentUser() AND statusCategory != Done"
    if jql_extra:
        jql += f" AND {jql_extra}"
    jql += " ORDER BY updated DESC"
    data = _get("/rest/api/3/search/jql", {"jql": jql, "maxResults": 50,
                                           "fields": "summary,status,priority"})
    return [
        {
            "key": issue["key"],
            "summary": issue["fields"]["summary"],
            "status": issue["fields"]["status"]["name"],
            "priority": (issue["fields"].get("priority") or {}).get("name", ""),
            "url": f"{JIRA_URL}/browse/{issue['key']}",
        }
        for issue in data.get("issues", [])
    ]


def get_ticket(key: str) -> dict:
    """Full detail for one ticket: key, summary, status, priority, assignee, created, updated, description."""
    data = _get(f"/rest/api/3/issue/{key}", {"fields": "summary,status,priority,description,assignee,created,updated"})
    fields = data["fields"]
    desc_nodes = (fields.get("description") or {}).get("content", [])
    desc = " ".join(
        node.get("text", "")
        for block in desc_nodes
        for node in block.get("content", [])
        if node.get("type") == "text"
    )
    assignee = fields.get("assignee") or {}
    return {
        "key": data["key"],
        "summary": fields["summary"],
        "status": fields["status"]["name"],
        "priority": (fields.get("priority") or {}).get("name", ""),
        "assignee": assignee.get("emailAddress", "Unassigned"),
        "created": fields.get("created", ""),
        "updated": fields.get("updated", ""),
        "description": desc,
        "url": f"{JIRA_URL}/browse/{data['key']}",
    }


def get_transitions(key: str) -> dict:
    """Return {name: id} for all available transitions on a ticket."""
    data = _get(f"/rest/api/3/issue/{key}/transitions")
    return {t["name"]: t["id"] for t in data.get("transitions", [])}


def transition_ticket(key: str, status_name: str) -> None:
    """Move ticket to the named status (e.g. 'In Progress', 'Done').

    Raises JiraError if the transition isn't available on that ticket.
    """
    transitions = get_transitions(key)
    # Case-insensitive match
    tid = next(
        (v for k, v in transitions.items() if k.lower() == status_name.lower()),
        None,
    )
    if tid is None:
        available = ", ".join(transitions.keys())
        raise JiraError(f"Transition '{status_name}' not available. Options: {available}")
    try:
        resp = requests.post(
            f"{JIRA_URL}/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": tid}},
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise JiraError(f"Could not reach Jira: {exc}") from exc
    if resp.status_code not in (200, 204):
        raise JiraError(f"Transition failed {resp.status_code}: {resp.text[:300]}")


def add_comment(key: str, text: str) -> None:
    """Post a plain-text comment on a Jira ticket."""
    if not is_configured():
        raise JiraNotConfiguredError("Jira isn't configured.")
    payload = {
        "body": {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                          "content": [{"type": "text", "text": text}]}],
        }
    }
    try:
        resp = requests.post(
            f"{JIRA_URL}/rest/api/3/issue/{key}/comment",
            json=payload,
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise JiraError(f"Could not reach Jira: {exc}") from exc
    if resp.status_code not in (200, 201):
        raise JiraError(f"Comment failed {resp.status_code}: {resp.text[:300]}")


def create_ticket(summary: str, description: str, labels: list = None, priority: str = "") -> dict:
    """Creates a Jira issue via the Cloud REST API v3. Returns {"key": "...", "url": "..."}.

    Raises JiraNotConfiguredError if JIRA_* env vars are missing, or
    JiraError on any API failure (bad auth, invalid project key, etc.).
    """
    if not is_configured():
        raise JiraNotConfiguredError(
            "Jira isn't configured — set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, "
            "JIRA_PROJECT_KEY in .env to enable ticket creation."
        )

    fields: dict = {
        "project": {"key": JIRA_PROJECT_KEY},
        "summary": summary,
        "description": {
            "type": "doc", "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [{"type": "text", "text": description}],
            }],
        },
        "issuetype": {"name": JIRA_ISSUE_TYPE},
    }
    if labels:
        fields["labels"] = labels
    if priority:
        fields["priority"] = {"name": priority}

    payload = {"fields": fields}

    try:
        resp = requests.post(
            f"{JIRA_URL}/rest/api/3/issue",
            json=payload,
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise JiraError(f"Could not reach Jira at {JIRA_URL}: {exc}") from exc

    if resp.status_code not in (200, 201):
        raise JiraError(f"Jira returned {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    key = data.get("key", "")
    return {"key": key, "url": f"{JIRA_URL}/browse/{key}" if key else ""}
