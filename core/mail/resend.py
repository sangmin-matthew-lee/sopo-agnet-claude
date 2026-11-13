"""Live transport: real delivery through Resend, for threads that came in by real email.

- Customer replies go only to a registered tester, threaded (In-Reply-To / References / "Re: <subject>"),
  from the address the customer wrote to. Agent replies to mail sent to am@ / eng@ are signed
  "SOPO AI Assistant" so the customer knows an AI answered.
- Mail for staff is delivered to the shared AM / engineer test inboxes (DB identities are unchanged, so
  engineer priority and calendars work as before) and carries X-SOPO-Thread so the copy that comes back
  in can be shown to the right tester. It quotes the conversation so far, so staff see which customer
  email the agent answered.
- Every attempt is written to the send log (simulated_emails, source='live'), sent or not.
"""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

from core import db
from core.mail import resend_api, threads, usage
from core.mail.base import AGENT, BLOCKED_QUOTA, FAILED, SENT, OutboundEmail, SendResult
from core.mail.config import LiveConfig

log = logging.getLogger("sopo.mail")

CUSTOMER_INBOX, SOP_EMPLOYEE_INBOX, ENGINEER_INBOX = "CUSTOMER_INBOX", "SOP_EMPLOYEE_INBOX", "ENGINEER_INBOX"
THREAD_HEADER = "X-SOPO-Thread"


def is_tester(email: str, db_path: str | None = None) -> bool:
    with db.get_conn(db_path) as conn:
        return conn.execute("SELECT 1 FROM testers WHERE email = ?", (email.strip().lower(),)).fetchone() is not None


AI_ASSISTANT = "SOPO AI Assistant"


def ai_signature(thread_id: str, mailbox: str, db_path: str | None = None) -> str:
    if mailbox == threads.AM:
        who = f"Your account manager {db.staff_name(db.thread_staff(thread_id, db_path)['AM'], db_path)}"
    else:
        who = "Our engineering team"
    return (f"— {AI_ASSISTANT}, AxMatt Supply\n"
            f"This reply was written by an AI agent. {who} can see this conversation and may reply personally.")


class ResendTransport:
    name = "resend"

    def send(self, email: OutboundEmail, db_path: str | None = None) -> SendResult:
        cfg = LiveConfig.from_env()
        message_id = f"<sopo-{uuid.uuid4().hex}@{cfg.test_domain or 'localhost'}>"
        headers = {"Message-ID": message_id}
        subject, body = email.subject, email.body
        reply_to: str | None = None

        if email.inbox_type == CUSTOMER_INBOX:
            to = email.recipient
            # Replies come from the address the customer wrote to: support@, or am@ / eng@ directly.
            mailbox = threads.mailbox(email.thread_id, db_path)
            address = {threads.AM: cfg.am_address, threads.ENG: cfg.eng_address}.get(mailbox, cfg.support_address)
            if email.author != AGENT:
                name = f"{email.author} via AxMatt Supply"
            elif mailbox == threads.SUPPORT:
                name = "AxMatt Supply Support"
            else:  # the customer wrote to a person: say plainly that an AI answered
                name = AI_ASSISTANT
                body = f"{body.rstrip()}\n\n{ai_signature(email.thread_id, mailbox, db_path)}"
            sender, reply_to = f"{name} <{address}>", address
            reply, original = threads.reply_headers(email.thread_id, db_path)
            headers.update(reply)
            headers["Auto-Submitted"] = "auto-replied"  # RFC 3834: other auto-responders must not answer
            subject = threads.re_subject(original, email.subject)
            allowed = is_tester(to, db_path)
        else:
            role = "Engineer" if email.inbox_type == ENGINEER_INBOX else "Account Manager"
            address = cfg.eng_address if email.inbox_type == ENGINEER_INBOX else cfg.am_address
            to = f"{db.staff_name(email.recipient, db_path)} ({role}) <{address}>"
            sender = f"SOPO Agent <{cfg.agent_address}>"
            headers[THREAD_HEADER] = email.thread_id
            headers["Auto-Submitted"] = "auto-generated"
            # Staff see exactly which customer email this is about: the whole thread, quoted like a forward.
            headers.update(threads.reply_headers(email.thread_id, db_path)[0])
            conversation = threads.transcript(email.thread_id, db_path)
            body = f"{body.rstrip()}\n\n{conversation}" if conversation else body
            allowed = bool(cfg.test_domain)

        def logged(status: str, provider_id: str | None = None) -> SendResult:
            log_id = db.insert_email(
                email.thread_id, email.inbox_type, email.recipient, subject, body, db_path,
                source=db.LIVE_SOURCE, status=status, delivered_to=to,
                provider_id=provider_id, rfc_message_id=message_id, author=email.author,
                attachments=[(a.filename, a.content_type, a.data) for a in email.attachments],
            )
            return SendResult(log_id, status)

        if not allowed:
            log.warning("Live send refused: %s is not a registered tester / test inbox", email.inbox_type)
            return logged(FAILED)
        if not usage.can_send(db_path):
            log.warning("Live send blocked: Resend email quota used up")
            return logged(BLOCKED_QUOTA)

        payload: dict[str, Any] = {"from": sender, "to": [to], "subject": subject, "text": body, "headers": headers}
        if reply_to:
            payload["reply_to"] = reply_to
        if email.attachments:
            payload["attachments"] = [
                # the content type is derived from the filename (.pdf)
                {"filename": a.filename, "content": base64.b64encode(a.data).decode()} for a in email.attachments
            ]
        try:
            provider_id, quota = resend_api.send_email(payload, idempotency_key=message_id)
        except resend_api.ResendError as exc:
            log.warning("Live send failed: %s", exc)
            return logged(FAILED)

        usage.record(usage.SENT, email.thread_id, db_path)
        usage.note_send_headers(quota)
        threads.remember(message_id, email.thread_id, threads.OUT, subject, db_path)
        return logged(SENT, provider_id)
