"""Thin Resend REST client (https://resend.com/docs/api-reference). The API key is only ever put in a header.

Tests replace `TRANSPORT` with an httpx.MockTransport, so no test ever reaches the network.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.mail.config import LiveConfig

BASE_URL = "https://api.resend.com"
TRANSPORT: httpx.BaseTransport | None = None  # tests: httpx.MockTransport(handler)


class ResendError(Exception):
    """Resend refused the request or was unreachable. Carries the status code, never the key."""

    def __init__(self, status: int | None, detail: str = "") -> None:
        self.status = status
        super().__init__(f"Resend API error (status={status}) {detail[:200]}".strip())


def _client() -> httpx.Client:
    key = LiveConfig.from_env().api_key
    if not key:
        raise ResendError(None, "RESEND_API_KEY is not set")
    return httpx.Client(
        base_url=BASE_URL,
        timeout=10.0,
        transport=TRANSPORT,
        headers={"Authorization": f"Bearer {key}", "User-Agent": "sopo-agent"},
    )


def _request(method: str, path: str, **kw: Any) -> httpx.Response:
    try:
        with _client() as client:
            resp = client.request(method, path, **kw)
    except httpx.HTTPError as exc:
        raise ResendError(None, type(exc).__name__) from None
    if resp.status_code >= 400:
        raise ResendError(resp.status_code, resp.text)
    return resp


def send_email(payload: dict[str, Any], idempotency_key: str) -> tuple[str, dict[str, int]]:
    """POST /emails. Returns (resend email id, quota usage from the response headers, if present)."""
    resp = _request("POST", "/emails", json=payload, headers={"Idempotency-Key": idempotency_key[:256]})
    quota: dict[str, int] = {}
    for header, key in (("x-resend-daily-quota", "daily_used"), ("x-resend-monthly-quota", "monthly_used")):
        value = resp.headers.get(header)
        if value and value.strip().isdigit():
            quota[key] = int(value)
    return str(resp.json().get("id", "")), quota


def get_received_email(email_id: str) -> dict[str, Any]:
    """GET /emails/receiving/{id}: text, html, headers, message_id, from, to, authentication…"""
    return _request("GET", f"/emails/receiving/{email_id}").json()


def get_usage() -> dict[str, Any]:
    """GET /usage: {"emails": {"daily": {used, limit, ...}, "monthly": {...}}}."""
    return _request("GET", "/usage").json()
