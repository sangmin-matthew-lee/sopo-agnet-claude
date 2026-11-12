"""Outbound mail. Callers use `send(...)`; the thread's origin picks the transport."""

from __future__ import annotations

from core.mail.base import MailTransport, OutboundEmail, SendResult
from core.mail.simulated import SimulatedTransport

_simulated = SimulatedTransport()


def transport_for(thread_id: str, db_path: str | None = None) -> MailTransport:
    return _simulated


def send(
    thread_id: str,
    inbox_type: str,
    recipient: str,
    subject: str,
    body: str,
    db_path: str | None = None,
) -> int:
    """Send one email and log it in simulated_emails. Returns the log row id."""
    email = OutboundEmail(thread_id, inbox_type, recipient, subject, body)
    return transport_for(thread_id, db_path).send(email, db_path).log_id


__all__ = ["MailTransport", "OutboundEmail", "SendResult", "send", "transport_for"]
