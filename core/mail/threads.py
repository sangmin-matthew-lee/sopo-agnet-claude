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


def remember(message_id: str, thread_id: str, direction: str, subject: str | None, db_path: str | None = None) -> None:
    with db.get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO mail_messages VALUES (?,?,?,?,?)",
            (message_id, thread_id, direction, subject, db.now_iso()),
        )


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
