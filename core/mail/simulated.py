"""Sandbox transport: the email is only written to simulated_emails. Nothing leaves the server."""

from __future__ import annotations

from core import db
from core.mail.base import SIMULATED, OutboundEmail, SendResult


class SimulatedTransport:
    name = "simulated"

    def send(self, email: OutboundEmail, db_path: str | None = None) -> SendResult:
        log_id = db.insert_email(
            email.thread_id, email.inbox_type, email.recipient, email.subject, email.body, db_path,
            author=email.author, attachments=[(a.filename, a.content_type, a.data) for a in email.attachments],
        )
        return SendResult(log_id=log_id, status=SIMULATED)
