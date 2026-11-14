"""Outbound mail. Callers use `send(...)`; the thread's origin picks the transport.

- Threads created by /api/simulate-email* (source=simulated) always use SimulatedTransport:
  nothing leaves the server and no Resend quota is used, whatever MAIL_MODE says.
- Threads created by the Resend webhook (source=live) use ResendTransport.
"""

from __future__ import annotations

from core import db
from core.mail import threads
from core.mail.base import AGENT, SENT, SIMULATED, Attachment, MailTransport, OutboundEmail, SendResult
from core.mail.greeting import first_name, fix_greeting
from core.mail.resend import ResendTransport
from core.mail.simulated import SimulatedTransport

_simulated = SimulatedTransport()
_resend = ResendTransport()


class MailNotSent(Exception):
    """A live email could not be delivered (quota used up, Resend error, recipient not allowed)."""

    def __init__(self, result: SendResult) -> None:
        self.result = result
        super().__init__(f"email not sent: {result.status}")


def transport_for(thread_id: str, db_path: str | None = None) -> MailTransport:
    return _resend if db.thread_source(thread_id, db_path) == db.LIVE_SOURCE else _simulated


def send(
    thread_id: str,
    inbox_type: str,
    recipient: str,
    subject: str,
    body: str,
    db_path: str | None = None,
    *,
    best_effort: bool = False,
    author: str = AGENT,
    attachments: tuple[Attachment, ...] = (),
) -> int:
    """Send one email and log it. Returns the log row id.

    Raises MailNotSent if a live email wasn't delivered, so an agent run stops and the pipeline
    fails closed. Escalations pass best_effort=True: they are logged locally even if not delivered.
    """
    if inbox_type == "CUSTOMER_INBOX" and author == AGENT:  # staff replies keep their own greeting
        body = fix_greeting(body, first_name(threads.customer_name(thread_id, db_path)))
    email = OutboundEmail(thread_id, inbox_type, recipient, subject, body, author, attachments)
    result = transport_for(thread_id, db_path).send(email, db_path)
    if result.status not in (SENT, SIMULATED) and not best_effort:
        raise MailNotSent(result)
    return result.log_id


__all__ = ["Attachment", "MailNotSent", "MailTransport", "OutboundEmail", "SendResult", "send", "transport_for"]
