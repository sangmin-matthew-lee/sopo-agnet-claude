"""SQLite persistence layer for the SOP Return Orchestrator (sop_demo.db)."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

DB_PATH = os.environ.get("SOP_DB_PATH", "sop_demo.db")

SOP_OWNER_EMAIL = "sop-team@acmesupply.com"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT,
    part_number TEXT,
    customer_email TEXT NOT NULL,
    sop_owner_email TEXT NOT NULL,
    purchased_qty INTEGER NOT NULL,
    status TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    return_label TEXT,
    PRIMARY KEY (order_id, part_number)
);
CREATE TABLE IF NOT EXISTS thread_vaults (
    thread_id TEXT PRIMARY KEY,
    customer_email TEXT NOT NULL,
    vault_json TEXT NOT NULL,
    thread_status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS simulated_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    inbox_type TEXT NOT NULL,
    recipient_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

APP_TABLES = ("orders", "thread_vaults", "processed_messages", "simulated_emails")
# Tables created by openai-agents SQLiteSession in the same DB file.
AGENT_SESSION_TABLES = ("agent_messages", "agent_sessions")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def seed_orders() -> list[tuple]:
    """Seed rows: (order_id, part_number, customer_email, sop_owner, qty, status, delivered_at, label)."""
    return [
        ("ORD-9988", "AB-1234", "john@acmecorp.com", SOP_OWNER_EMAIL, 2, "DELIVERED", _days_ago(10), None),
        ("ORD-1111", "CD-5678", "john@acmecorp.com", SOP_OWNER_EMAIL, 1, "DELIVERED", _days_ago(45), None),
        ("ORD-7777", "EF-9012", "mary@betaindustries.com", SOP_OWNER_EMAIL, 5, "SHIPPED", _days_ago(2), None),
    ]


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | None = None, seed: bool = True) -> None:
    """Create tables if missing; seed orders only when the table is empty."""
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        if seed and conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", seed_orders())


def reset_db(db_path: str | None = None) -> None:
    """Drop all app + agent-session tables, then recreate and reseed."""
    with get_conn(db_path) as conn:
        for table in APP_TABLES + AGENT_SESSION_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    init_db(db_path)


# ---------------------------------------------------------------- idempotency

def is_message_processed(message_id: str, db_path: str | None = None) -> bool:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None


def mark_message_processed(message_id: str, thread_id: str, db_path: str | None = None) -> bool:
    """Atomically claim a message_id. Returns False if it was already claimed."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_messages VALUES (?,?,?)",
            (message_id, thread_id, now_iso()),
        )
        return cur.rowcount == 1


# --------------------------------------------------------------- thread vault

def load_thread(thread_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM thread_vaults WHERE thread_id = ?", (thread_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["vault"] = json.loads(data.pop("vault_json"))
    return data


def save_thread(
    thread_id: str,
    customer_email: str,
    vault: dict[str, str],
    thread_status: str,
    db_path: str | None = None,
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO thread_vaults VALUES (?,?,?,?,?)
               ON CONFLICT(thread_id) DO UPDATE SET
                 vault_json = excluded.vault_json,
                 thread_status = excluded.thread_status,
                 updated_at = excluded.updated_at""",
            (thread_id, customer_email, json.dumps(vault), thread_status, now_iso()),
        )


def set_thread_status(thread_id: str, thread_status: str, db_path: str | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE thread_vaults SET thread_status = ?, updated_at = ? WHERE thread_id = ?",
            (thread_status, now_iso(), thread_id),
        )


# -------------------------------------------------------------------- orders

def find_order(order_id: str, part_number: str, db_path: str | None = None) -> dict[str, Any] | None:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ? AND part_number = ?", (order_id, part_number)
        ).fetchone()
    return dict(row) if row else None


def find_order_for_customer(
    order_id: str, part_number: str, customer_email: str, db_path: str | None = None
) -> dict[str, Any] | None:
    """3-way match: order_id + part_number + customer_email (case-insensitive email)."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM orders
               WHERE order_id = ? AND part_number = ? AND lower(customer_email) = lower(?)""",
            (order_id, part_number, customer_email),
        ).fetchone()
    return dict(row) if row else None


def set_return_label(order_id: str, part_number: str, label: str, db_path: str | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE orders SET status = 'RETURN_INITIATED', return_label = ?
               WHERE order_id = ? AND part_number = ?""",
            (label, order_id, part_number),
        )


def list_orders(db_path: str | None = None) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM orders ORDER BY order_id")]


# ---------------------------------------------------------- simulated emails

def insert_email(
    thread_id: str,
    inbox_type: str,
    recipient_email: str,
    subject: str,
    body: str,
    db_path: str | None = None,
) -> int:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO simulated_emails
               (thread_id, inbox_type, recipient_email, subject, body, created_at)
               VALUES (?,?,?,?,?,?)""",
            (thread_id, inbox_type, recipient_email, subject, body, now_iso()),
        )
        return int(cur.lastrowid)


def list_emails(db_path: str | None = None) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM simulated_emails ORDER BY id DESC")]


def list_threads(db_path: str | None = None) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM thread_vaults ORDER BY updated_at DESC")]
    for r in rows:
        r["vault"] = json.loads(r.pop("vault_json"))
    return rows
