"""Closed-beta testers: sign-up with an invite code, then a private dashboard at /t/<token>.

Every /t/<token>/* query is scoped to that tester (their orders, and only threads they own). The token is
random (256 bits) and only its sha256 is stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core import db, mail
from core.mail import usage
from core.mail.config import LiveConfig
from core.mail.resend import CUSTOMER_INBOX
from core.masker import ORDER_RE, PART_RE
from core.ratelimit import client_ip, register_limiter, tester_limiter

router = APIRouter()
TESTER_PAGE = Path(__file__).parent.parent / "static" / "tester.html"
MAX_ORDERS_PER_TESTER = 10


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def tester_for(token: str) -> dict[str, Any] | None:
    if not token or len(token) > 100:
        return None
    with db.get_conn() as conn:
        row = conn.execute("SELECT id, email, created_at FROM testers WHERE token_hash = ?", (_hash(token),)).fetchone()
    return dict(row) if row else None


def current_tester(token: str, request: Request) -> dict[str, Any]:
    if not tester_limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many requests. Please slow down.")
    tester = tester_for(token)
    if tester is None:
        raise HTTPException(404, "Not found.")
    return tester


# --------------------------------------------------------------------- sign-up


class Registration(BaseModel):
    invite_code: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/tester/register")
async def register(reg: Registration, request: Request) -> dict[str, Any]:
    cfg = LiveConfig.from_env()
    if not cfg.invite_code:
        raise HTTPException(503, "Tester sign-up is closed (TESTER_INVITE_CODE is not set).")
    if not register_limiter.allow(client_ip(request)):  # counts wrong codes too
        raise HTTPException(429, "Too many attempts. Please wait a minute.")
    if not hmac.compare_digest(reg.invite_code.encode(), cfg.invite_code.encode()):
        raise HTTPException(401, "Invalid invite code.")
    email = reg.email.strip().lower()
    if cfg.test_domain and email.endswith("@" + cfg.test_domain):
        raise HTTPException(400, "Please use your personal email address.")

    token = secrets.token_urlsafe(32)
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM testers WHERE email = ?", (email,)).fetchone():
            # No re-issue: that would let anyone with the invite code take over another tester's dashboard.
            raise HTTPException(409, "This email is already registered. Ask the admin for your dashboard link.")
        if conn.execute("SELECT COUNT(*) FROM testers").fetchone()[0] >= cfg.max_testers:
            raise HTTPException(409, "The beta is full.")
        conn.execute("INSERT INTO testers (email, token_hash, created_at) VALUES (?,?,?)",
                     (email, _hash(token), db.now_iso()))
    base = cfg.public_base_url or str(request.base_url).rstrip("/")
    return {"email": email, "dashboard_url": f"{base}/t/{token}", "support_address": cfg.support_address}


# ---------------------------------------------------------------------- orders


class NewOrder(BaseModel):
    delivered: Literal["10d", "45d"] = "10d"  # 10 days ago: returnable; 45 days ago: past the 30-day window


def _new_ids(conn: Any) -> tuple[str, str]:
    """A fresh order id and part number the masker recognizes (ORD-12345, KX-4821), unused so far."""
    rng = random.SystemRandom()
    for _ in range(100):
        order_id = f"ORD-{rng.randint(10000, 99999)}"
        prefix = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2))
        part = f"{prefix}-{rng.randint(1000, 9999)}"
        if not (PART_RE.fullmatch(part) and ORDER_RE.fullmatch(order_id)):
            continue  # e.g. "IS-1234": the masker would read "is" as a word, not a part
        taken = conn.execute(
            "SELECT 1 FROM orders WHERE order_id = ? OR part_number = ? "
            "UNION SELECT 1 FROM engineer_parts WHERE part_number = ?",
            (order_id, part, part),
        ).fetchone()
        if not taken:
            return order_id, part
    raise HTTPException(500, "Could not allocate an order id.")


@router.post("/t/{token}/orders")
async def create_order(order: NewOrder, tester: dict[str, Any] = Depends(current_tester)) -> dict[str, Any]:
    days = 10 if order.delivered == "10d" else 45
    delivered_at = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM orders WHERE tester_id = ?", (tester["id"],)).fetchone()[0] \
                >= MAX_ORDERS_PER_TESTER:
            raise HTTPException(409, f"You already have {MAX_ORDERS_PER_TESTER} test orders.")
        order_id, part = _new_ids(conn)
        conn.execute(
            "INSERT INTO orders (order_id, part_number, customer_email, sop_owner_email, purchased_qty, status, "
            "delivered_at, return_label, tester_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (order_id, part, tester["email"], db.AM_JOHN, 2, "DELIVERED", delivered_at, None, tester["id"]),
        )
        # Tech support: engineers who know this part (same priority pattern as the seed's AB-1234).
        conn.executemany("INSERT OR IGNORE INTO engineer_parts VALUES (?,?,?)",
                         [(db.ENG_ALEX, part, 1), (db.ENG_PRIYA, part, 2)])
    return {"order_id": order_id, "part_number": part, "delivered_at": delivered_at,
            "returnable": days <= 30, "purchased_qty": 2}


# --------------------------------------------------- staff replies and hand-back

ROLE_TITLES = {"AM": "Account Manager", "ENGINEER": "Engineer"}


class StaffReply(BaseModel):
    role: Literal["AM", "ENGINEER"]
    body: str = Field(min_length=1, max_length=5000)


def _own_thread(tester: dict[str, Any], thread_id: str) -> dict[str, Any]:
    thread = db.load_thread(thread_id)
    if (thread is None or thread["source"] != db.LIVE_SOURCE
            or thread["customer_email"].lower() != tester["email"].lower()):
        raise HTTPException(404, "Not found.")
    return thread


@router.post("/t/{token}/threads/{thread_id}/reply")
async def staff_reply(thread_id: str, reply: StaffReply,
                      tester: dict[str, Any] = Depends(current_tester)) -> dict[str, Any]:
    """The AM or engineer answers the customer in the customer's own thread; the agent then stays quiet."""
    thread = _own_thread(tester, thread_id)
    name = db.staff_name(db.thread_staff(thread_id)[reply.role])
    title = ROLE_TITLES[reply.role]
    body = f"{reply.body.strip()}\n\n{name}\n{title}, AxMatt Supply"
    try:
        await asyncio.to_thread(mail.send, thread_id, CUSTOMER_INBOX, thread["customer_email"],
                                "Reply from AxMatt Supply", body, author=f"{name} ({title})")
    except mail.MailNotSent as exc:
        raise HTTPException(503, f"Not sent ({exc.result.status}). Check the email quota.") from None
    db.pause_thread(thread_id, reply.role)
    return {"thread_status": db.HUMAN_HANDLING, "author": f"{name} ({title})"}


@router.post("/t/{token}/threads/{thread_id}/resume")
async def hand_back(thread_id: str, tester: dict[str, Any] = Depends(current_tester)) -> dict[str, Any]:
    """'Hand back to agent': restore the status the thread had before a person took over."""
    if _own_thread(tester, thread_id)["thread_status"] != db.HUMAN_HANDLING:
        raise HTTPException(409, "This thread is not handled by a person.")
    return {"thread_status": db.resume_thread(thread_id)}


# ----------------------------------------------------------------------- state


def _rows(conn: Any, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args)]


def tester_state(tester: dict[str, Any]) -> dict[str, Any]:
    """Everything this tester may see, and nothing else."""
    cfg = LiveConfig.from_env()
    with db.get_conn() as conn:
        threads = _rows(conn,
                        "SELECT thread_id, thread_status, human_roles, updated_at FROM thread_vaults "
                        "WHERE source = 'live' AND lower(customer_email) = lower(?) ORDER BY updated_at DESC",
                        (tester["email"],))
        ids = tuple(t["thread_id"] for t in threads)
        marks = ",".join("?" * len(ids)) or "NULL"
        orders = _rows(conn, "SELECT order_id, part_number, purchased_qty, status, delivered_at, return_label "
                             "FROM orders WHERE tester_id = ? ORDER BY delivered_at DESC", (tester["id"],))
        sent = _rows(conn, "SELECT id, thread_id, inbox_type, delivered_to, subject, body, status, author, created_at "
                           f"FROM simulated_emails WHERE source = 'live' AND thread_id IN ({marks}) ORDER BY id DESC",
                     ids)
        internal = _rows(conn, "SELECT id, role, thread_id, subject, body, received_at FROM internal_inbox "
                               f"WHERE thread_id IN ({marks}) ORDER BY id DESC", ids)
        traces = _rows(conn, "SELECT thread_id, message_id, trace_json, created_at FROM thread_traces "
                             f"WHERE thread_id IN ({marks}) ORDER BY id DESC LIMIT 30", ids)
        meetings = _rows(conn, "SELECT thread_id, part_number, meeting_start, status FROM tech_schedules "
                               f"WHERE thread_id IN ({marks})", ids)
    for t in traces:
        t["trace"] = json.loads(t.pop("trace_json"))
    for t in threads:  # who would answer from the AM / engineer tab
        t["staff"] = {role: db.staff_name(email) for role, email in db.thread_staff(t["thread_id"]).items()}
    return {
        "tester": {"email": tester["email"], "since": tester["created_at"]},
        "support_address": cfg.support_address,
        "am_address": cfg.am_address,
        "live_enabled": cfg.live_enabled,
        "orders": orders,
        "threads": threads,
        "am_inbox": [m for m in internal if m["role"] == "AM"],
        "engineer_inbox": [m for m in internal if m["role"] == "ENGINEER"],
        "sent_log": sent,
        "traces": traces,
        "meetings": meetings,
        "usage": usage.snapshot(),
    }


@router.get("/t/{token}/state")
async def state(tester: dict[str, Any] = Depends(current_tester)) -> dict[str, Any]:
    return tester_state(tester)


@router.get("/t/{token}", include_in_schema=False)
async def dashboard(tester: dict[str, Any] = Depends(current_tester)) -> FileResponse:
    return FileResponse(TESTER_PAGE)


@router.get("/tester", include_in_schema=False)
async def signup_page() -> FileResponse:
    return FileResponse(TESTER_PAGE)

