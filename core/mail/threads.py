"""RFC 5322 threading for live threads: Message-IDs we received or sent, mapped to our thread_id."""

from __future__ import annotations

import re

from core import db

IN, OUT = "IN", "OUT"
_MSGID_RE = re.compile(r"<[^<>\s]+>")


def parse_ids(header_value: str | None) -> list[str]:
    """'<a@x> <b@y>' -> ['<a@x>', '<b@y>'] (In-Reply-To / References)."""
    return _MSGID_RE.findall(header_value or "")


def normalize(message_id: str | None) -> str | None:
    if not message_id:
        return None
    message_id = message_id.strip()
    return message_id if message_id.startswith("<") else f"<{message_id}>"


def remember(message_id: str, thread_id: str, direction: str, subject: str | None, db_path: str | None = None,
             *, sender: str | None = None, body: str | None = None, mailbox: str | None = None,
             sender_name: str | None = None) -> None:
    """Record a Message-ID of this thread. Incoming customer mail also keeps its sender, cleaned text and
    the mailbox it was sent to."""
    with db.get_conn(db_path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO mail_messages
               (rfc_message_id, thread_id, direction, subject, created_at, sender, body, mailbox, sender_name)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (message_id, thread_id, direction, subject, db.now_iso(), sender, body, mailbox, sender_name),
        )


SUPPORT, AM, ENG = "support", "am", "eng"


def mailbox(thread_id: str, db_path: str | None = None) -> str:
    """The address the customer first wrote to (support | am | eng); replies come from the same one."""
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT mailbox FROM mail_messages WHERE thread_id = ? AND direction = 'IN' AND mailbox IS NOT NULL "
            "ORDER BY rowid LIMIT 1",
            (thread_id,),
        ).fetchone()
    return row["mailbox"] if row else SUPPORT


def customer_name(thread_id: str, db_path: str | None = None) -> str | None:
    """The From display name of the customer's latest email on this thread (live mail only)."""
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT sender_name FROM mail_messages WHERE thread_id = ? AND direction = 'IN' "
            "AND sender_name IS NOT NULL AND sender_name != '' ORDER BY rowid DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
    return row["sender_name"] if row else None


def find_thread(ids: list[str], db_path: str | None = None, *, owner: str | None = None) -> str | None:
    """The live thread any of these Message-IDs belongs to (the most recent reference wins).

    With `owner`, only that sender's threads match: someone else's reply starts a new thread instead of
    reading (or writing into) a thread that isn't theirs."""
    with db.get_conn(db_path) as conn:
        for mid in reversed(ids):
            row = conn.execute(
                """SELECT m.thread_id FROM mail_messages m JOIN thread_vaults t ON t.thread_id = m.thread_id
                   WHERE m.rfc_message_id = ? AND t.source = 'live'
                     AND (? IS NULL OR lower(t.customer_email) = lower(?))""",
                (mid, owner, owner),
            ).fetchone()
            if row:
                return row["thread_id"]
    return None


def chain(thread_id: str, db_path: str | None = None) -> list[dict[str, str]]:
    with db.get_conn(db_path) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT rfc_message_id, direction, subject FROM mail_messages WHERE thread_id = ? ORDER BY rowid",
                (thread_id,),
            )
        ]


def reply_headers(thread_id: str, db_path: str | None = None) -> tuple[dict[str, str], str | None]:
    """(In-Reply-To/References headers, the customer's original subject) for a reply on this thread."""
    msgs = chain(thread_id, db_path)
    incoming = [m for m in msgs if m["direction"] == IN]
    headers: dict[str, str] = {}
    if incoming:
        headers["In-Reply-To"] = incoming[-1]["rfc_message_id"]
        headers["References"] = " ".join(m["rfc_message_id"] for m in msgs[-20:])
    return headers, (incoming[0]["subject"] if incoming else None)


def re_subject(original: str | None, fallback: str) -> str:
    subject = (original or "").strip() or fallback
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


MAX_TRANSCRIPT_MESSAGES = 10
MAX_TRANSCRIPT_CHARS = 2000


def transcript(thread_id: str, db_path: str | None = None) -> str:
    """The conversation with the customer so far, newest first, quoted like a forwarded email.

    Customer emails come from mail_messages (IN); replies are the live emails actually sent to the customer
    (by the agent or by staff from the dashboard)."""
    with db.get_conn(db_path) as conn:
        incoming = conn.execute(
            """SELECT created_at, 0 AS seq, sender AS sender, '' AS recipient, subject, body FROM mail_messages
               WHERE thread_id = ? AND direction = 'IN' AND body IS NOT NULL""",
            (thread_id,),
        ).fetchall()
        replies = conn.execute(
            """SELECT created_at, id AS seq, author AS sender, delivered_to AS recipient, subject, body
               FROM simulated_emails
               WHERE thread_id = ? AND source = 'live' AND inbox_type = 'CUSTOMER_INBOX' AND status = 'SENT'""",
            (thread_id,),
        ).fetchall()
    msgs = sorted((dict(r) for r in [*incoming, *replies]), key=lambda m: (m["created_at"], m["seq"]))
    msgs = msgs[-MAX_TRANSCRIPT_MESSAGES:][::-1]
    if not msgs:
        return ""
    parts = []
    for m in msgs:
        sender = "SOPO AI agent" if m["sender"] == "agent" else m["sender"]
        body = m["body"] if len(m["body"]) <= MAX_TRANSCRIPT_CHARS else m["body"][:MAX_TRANSCRIPT_CHARS] + " […]"
        to = f"\nTo: {m['recipient']}" if m["recipient"] else ""
        parts.append(f"From: {sender}{to}\nDate: {m['created_at']}\nSubject: {m['subject']}\n\n{body.strip()}")
    return "---------- Conversation so far (newest first) ----------\n\n" + "\n\n- - -\n\n".join(parts)
