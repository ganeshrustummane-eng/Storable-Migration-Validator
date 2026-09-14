"""
Failure notifier — Slack webhook and/or SMTP email.

Reads from env (already loaded by dotenv at app startup):
    SLACK_WEBHOOK_URL          — post to a Slack incoming webhook
    NOTIFY_EMAIL_TO            — comma-separated recipient(s)
    NOTIFY_EMAIL_FROM          — sender address
    SMTP_HOST / SMTP_PORT      — defaults: localhost / 587
    SMTP_USERNAME / SMTP_PASSWORD — leave blank for unauthenticated relay

Call notify_failure() after a run that has failures. Safe to call even when
unconfigured — returns silently without raising.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import requests

_SLACK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO", "")
_EMAIL_FROM = os.getenv("NOTIFY_EMAIL_FROM", "migration-validator@localhost")
_SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
_SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
_SMTP_USER = os.getenv("SMTP_USERNAME", "")
_SMTP_PASS = os.getenv("SMTP_PASSWORD", "")


def notify_failure(subject: str, body: str) -> list[str]:
    """Send failure notification via every configured channel.

    Returns list of error strings (empty = all succeeded / nothing configured).
    """
    errors: list[str] = []
    if _SLACK_URL:
        errors.extend(_slack(subject, body))
    if _EMAIL_TO:
        errors.extend(_email(subject, body))
    return errors


def _slack(subject: str, body: str) -> list[str]:
    try:
        resp = requests.post(
            _SLACK_URL,
            json={"text": f"*{subject}*\n{body}"},
            timeout=10,
        )
        if not resp.ok:
            return [f"Slack: {resp.status_code} {resp.text[:200]}"]
    except Exception as exc:
        return [f"Slack: {exc}"]
    return []


def _email(subject: str, body: str) -> list[str]:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _EMAIL_FROM
    msg["To"] = _EMAIL_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=15) as s:
            if _SMTP_USER:
                s.starttls()
                s.login(_SMTP_USER, _SMTP_PASS)
            s.send_message(msg)
    except Exception as exc:
        return [f"Email: {exc}"]
    return []
