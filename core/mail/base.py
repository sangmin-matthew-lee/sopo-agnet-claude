"""Mail transport interface. Every email the agents or the pipeline send goes through one of these."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# Values of simulated_emails.status (the "sent log").
SIMULATED = "SIMULATED"  # written to the sandbox inbox only, never left the server
SENT = "SENT"  # delivered to Resend
BLOCKED_QUOTA = "BLOCKED_QUOTA"  # not sent: today's / this month's email quota is used up
FAILED = "FAILED"  # Resend rejected it or was unreachable


@dataclass(frozen=True)
class OutboundEmail:
    thread_id: str
    inbox_type: str  # CUSTOMER_INBOX | SOP_EMPLOYEE_INBOX | ENGINEER_INBOX
    recipient: str  # the real (DB) address; a transport may deliver it elsewhere
    subject: str
    body: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    log_id: int  # simulated_emails.id
    status: str
    provider_id: str | None = None
    rfc_message_id: str | None = None


class MailTransport(Protocol):
    name: str

    def send(self, email: OutboundEmail, db_path: str | None = None) -> SendResult: ...
