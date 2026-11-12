"""What happens to one email Resend received for us (runs after the webhook has already answered 200)."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from email.utils import getaddresses, parseaddr
from typing import Any

import pipeline
from core import db
from core.mail import resend_api, threads, usage
from core.mail.config import LiveConfig
from core.mail.resend import THREAD_HEADER, is_tester

log = logging.getLogger("sopo.live")

PIPELINE, INTERNAL, IGNORED = "PIPELINE", "INTERNAL", "IGNORED"
_DAEMON_RE = re.compile(r"^(mailer-daemon|postmaster)$", re.IGNORECASE)
MAX_BODY = 10_000  # same cap as /api/simulate-email


def claim(event_key: str, db_path: str | None = None) -> bool:
    """Atomically mark a webhook event / email / Message-ID as seen. False if it was already seen."""
    with db.get_conn(db_path) as conn:
        cur = conn.execute("INSERT OR IGNORE INTO inbound_events VALUES (?,?)", (event_key, db.now_iso()))
        return cur.rowcount == 1


def _log(email_id: str, sender: str, recipient: str, subject: str, decision: str, reason: str,
         thread_id: str | None = None) -> None:
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO inbound_log (email_id, sender, recipient, subject, decision, reason, thread_id, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (email_id, sender, recipient, subject, decision, reason, thread_id, db.now_iso()),
        )
    log.info("inbound %s: %s (%s)", email_id, decision, reason)


def _headers(full: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (full.get("headers") or {}).items():
        out[str(k).lower()] = " ".join(map(str, v)) if isinstance(v, list) else str(v)
    return out


def _addresses(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [addr.lower() for _, addr in getaddresses([str(v) for v in values or []]) if addr]


def loop_reason(sender: str, headers: dict[str, str], cfg: LiveConfig) -> str | None:
    """Why this email must not be answered (auto-replies, bounces, our own mail), or None."""
    auto = headers.get("auto-submitted", "no").strip().lower()
    if auto != "no":
        return f"Auto-Submitted: {auto}"
    local, _, domain = sender.partition("@")
    if _DAEMON_RE.match(local):
        return f"{local} (bounce / delivery report)"
    if cfg.test_domain and (domain == cfg.test_domain or domain.endswith("." + cfg.test_domain)):
        return "sent from our own domain"
    return None


def auth_passed(full: dict[str, Any]) -> bool:
    """DKIM or DMARC pass, as checked by Resend's receiving server. A bare From: is trivially forged."""
    auth = full.get("authentication") or {}
    return auth.get("dkim") == "pass" or auth.get("dmarc") == "pass"


async def handle(email_id: str, usage_id: int) -> None:
    """Fetch the email, then: support@ -> pipeline, am@/eng@ -> internal_inbox, anything else -> log only."""
    cfg = LiveConfig.from_env()
    try:
        full = await asyncio.to_thread(resend_api.get_received_email, email_id)
    except resend_api.ResendError as exc:
        _log(email_id, "", "", "", IGNORED, f"could not fetch the email from Resend: {exc}")
        return

    headers = _headers(full)
    message_id = threads.normalize(full.get("message_id") or headers.get("message-id"))
    sender = (parseaddr(str(full.get("from") or ""))[1] or "").lower()
    subject = str(full.get("subject") or "")
    recipients = _addresses(full.get("to")) + _addresses(full.get("cc")) + _addresses(full.get("received_for"))
    if message_id and not claim(f"msgid:{message_id}"):
        _log(email_id, sender, ",".join(recipients), subject, IGNORED, "duplicate Message-ID")
        return
    body = str(full.get("text") or full.get("html") or "")[:MAX_BODY]

    if cfg.support_address in recipients:
        await _customer_email(cfg, email_id, usage_id, full, headers, message_id, sender, subject, body)
    elif cfg.am_address in recipients or cfg.eng_address in recipients:
        roles = [r for r, a in (("AM", cfg.am_address), ("ENGINEER", cfg.eng_address)) if a in recipients]
        _internal_email(cfg, email_id, usage_id, roles, headers, sender, subject, body)
    else:
        _log(email_id, sender, ",".join(recipients), subject, IGNORED, "not addressed to a known inbox")


async def _customer_email(cfg: LiveConfig, email_id: str, usage_id: int, full: dict[str, Any],
                          headers: dict[str, str], message_id: str | None, sender: str,
                          subject: str, body: str) -> None:
    to = cfg.support_address
    reason = loop_reason(sender, headers, cfg)
    if reason is None and not is_tester(sender):
        reason = "sender is not a registered tester"
    if reason is None and cfg.require_sender_auth and not auth_passed(full):
        reason = "sender authentication failed (no DKIM/DMARC pass)"
    if reason:
        _log(email_id, sender, to, subject, IGNORED, reason)
        return

    refs = threads.parse_ids(headers.get("in-reply-to")) + threads.parse_ids(headers.get("references"))
    thread_id = threads.find_thread(refs, owner=sender) or f"live-{uuid.uuid4().hex[:12]}"
    message_id = message_id or f"<resend-{email_id}@{cfg.test_domain or 'localhost'}>"
    threads.remember(message_id, thread_id, threads.IN, subject)
    usage.attach(usage_id, thread_id)
    _log(email_id, sender, to, subject, PIPELINE, "new thread" if not refs else "reply", thread_id)

    try:
        await pipeline.process_email(sender, thread_id, message_id, body or "(empty email)", source=db.LIVE_SOURCE)
    except Exception as exc:  # fail closed: a person takes over
        log.exception("live pipeline error on %s", thread_id)
        if db.load_thread(thread_id) is None:
            db.save_thread(thread_id, sender, {}, "PROCESSING", source=db.LIVE_SOURCE)
        reasons = [f"live processing error: {type(exc).__name__}"]
        trace = pipeline.Trace()
        pipeline._escalate(thread_id, pipeline.Sender.of(sender, body), body, reasons, trace)
        db.set_thread_status(thread_id, pipeline.ESCALATED_TO_HUMAN)
        db.save_trace(thread_id, message_id, trace.steps)


def _internal_email(cfg: LiveConfig, email_id: str, usage_id: int, roles: list[str],
                    headers: dict[str, str], sender: str, subject: str, body: str) -> None:
    # Only our own agent's mail is tied to a tester's thread; a forged X-SOPO-Thread from anyone else is not.
    thread_id = headers.get(THREAD_HEADER.lower()) if sender == cfg.agent_address else None
    if thread_id and db.thread_source(thread_id) != db.LIVE_SOURCE:
        thread_id = None
    with db.get_conn() as conn:
        for role in roles:
            conn.execute(
                """INSERT INTO internal_inbox (role, thread_id, sender, recipient, subject, body, received_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (role, thread_id, sender, cfg.am_address if role == "AM" else cfg.eng_address,
                 subject, body, db.now_iso()),
            )
    if thread_id:
        usage.attach(usage_id, thread_id)
    _log(email_id, sender, ",".join(roles), subject, INTERNAL,
         "agent copy" if thread_id else "no valid X-SOPO-Thread", thread_id)
