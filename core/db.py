"""SQLite persistence layer for the SOP Return Orchestrator (sop_demo.db)."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

DB_PATH = os.environ.get("SOP_DB_PATH", "sop_demo.db")

COMPANY_DOMAIN = "axmattsupply.com"
LEGACY_DOMAIN = (
    "acmesupply.com"  # the demo company's old name; migrated away in _migrate()
)

SOP_OWNER_EMAIL = (
    "sop-team@axmattsupply.com"  # shared SOP queue: escalations & security alerts
)

# Named account managers (the per-account `orders.sop_owner_email`).
AM_JOHN = "sarah.kim@axmattsupply.com"
AM_MARY = "david.park@axmattsupply.com"
# Field/application engineers.
ENG_ALEX = "alex.chen@axmattsupply.com"
ENG_PRIYA = "priya.patel@axmattsupply.com"
ENG_MARCO = "marco.rossi@axmattsupply.com"

# Client -> their account manager (stored per order in `orders.sop_owner_email`).
ACCOUNT_MANAGERS = {"john@acmecorp.com": AM_JOHN, "mary@betaindustries.com": AM_MARY}

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
CREATE TABLE IF NOT EXISTS staff (
    email TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engineer_parts (
    engineer_email TEXT NOT NULL,
    part_number TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (engineer_email, part_number)
);
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_email TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    thread_id TEXT
);
CREATE TABLE IF NOT EXISTS tech_schedules (
    thread_id TEXT PRIMARY KEY,
    customer_email TEXT NOT NULL,
    part_number TEXT,
    am_email TEXT,
    engineer_email TEXT,
    rounds INTEGER NOT NULL DEFAULT 0,
    offered_slots_json TEXT NOT NULL DEFAULT '[]',
    meeting_start TEXT,
    meeting_link TEXT,
    issue_text TEXT,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
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

# Live (real email) mode. None of these are touched by reset_db(); see reset_live().
LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS testers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,   -- sha256 of the dashboard token; the token itself is never stored
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_messages (   -- RFC Message-IDs seen or sent, for In-Reply-To threading
    rfc_message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    direction TEXT NOT NULL,           -- IN | OUT
    subject TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_events (  -- webhook dedup keys: svix:<id>, email:<id>, msgid:<id>
    event_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_log (     -- every received email and what we did with it
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT,
    sender TEXT,
    recipient TEXT,
    subject TEXT,
    decision TEXT NOT NULL,            -- PIPELINE | INTERNAL | IGNORED
    reason TEXT,
    thread_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS internal_inbox (  -- what actually arrived at am@ / eng@
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,                -- AM | ENGINEER
    thread_id TEXT,                    -- from the X-SOPO-Thread header
    sender TEXT,
    recipient TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_usage (     -- local count of Resend quota use (fallback)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,           -- sent | received
    thread_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thread_traces (   -- X-Ray of live threads (the sandbox streams it instead)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    message_id TEXT,
    trace_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Columns added after the first release: (table, column, declaration). Added in place by _migrate().
_ADDED_COLUMNS = (
    ("thread_vaults", "source", "TEXT NOT NULL DEFAULT 'simulated'"),  # simulated | live
    ("orders", "tester_id", "INTEGER"),  # NULL = seed/sandbox order
    ("simulated_emails", "source", "TEXT NOT NULL DEFAULT 'simulated'"),
    ("simulated_emails", "status", "TEXT NOT NULL DEFAULT 'SIMULATED'"),
    ("simulated_emails", "delivered_to", "TEXT"),  # real address used by Resend (am@ / eng@ / tester)
    ("simulated_emails", "provider_id", "TEXT"),
    ("simulated_emails", "rfc_message_id", "TEXT"),
)

SIMULATED_SOURCE, LIVE_SOURCE = "simulated", "live"

APP_TABLES = (
    "orders",
    "thread_vaults",
    "processed_messages",
    "simulated_emails",
    "staff",
    "engineer_parts",
    "calendar_events",
    "tech_schedules",
    "testers",
    "mail_messages",
    "inbound_events",
    "inbound_log",
    "internal_inbox",
    "email_usage",
    "thread_traces",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )


# Seed row shapes (match the INSERT column order).
OrderRow = tuple[str, str, str, str, int, str, str, str | None]  # order_id, part, customer, am, qty, status, delivered_at, label
StaffRow = tuple[str, str, str]  # email, name, role
EngineerPartRow = tuple[str, str, int]  # engineer_email, part_number, priority
BusyBlock = tuple[str, int, int, str]  # when, start_hour, end_hour, title
CalendarRow = tuple[str, str, str, str, str, str | None]  # owner, start_utc, end_utc, title, kind, thread_id


def seed_orders() -> list[OrderRow]:
    """Seed rows: (order_id, part_number, customer_email, sop_owner, qty, status, delivered_at, label)."""
    return [
        (
            "ORD-9988",
            "AB-1234",
            "john@acmecorp.com",
            ACCOUNT_MANAGERS["john@acmecorp.com"],
            2,
            "DELIVERED",
            _days_ago(10),
            None,
        ),
        (
            "ORD-1111",
            "CD-5678",
            "john@acmecorp.com",
            AM_JOHN,
            1,
            "DELIVERED",
            _days_ago(45),
            None,
        ),
        (
            "ORD-7777",
            "EF-9012",
            "mary@betaindustries.com",
            AM_MARY,
            5,
            "SHIPPED",
            _days_ago(2),
            None,
        ),
    ]


def seed_staff() -> list[StaffRow]:
    return [
        (AM_JOHN, "Sarah Kim", "ACCOUNT_MANAGER"),
        (AM_MARY, "David Park", "ACCOUNT_MANAGER"),
        (ENG_ALEX, "Alex Chen", "ENGINEER"),
        (ENG_PRIYA, "Priya Patel", "ENGINEER"),
        (ENG_MARCO, "Marco Rossi", "ENGINEER"),
    ]


def seed_engineer_parts() -> list[EngineerPartRow]:
    """(engineer_email, part_number, priority) — lower priority number is asked first."""
    return [
        (ENG_ALEX, "AB-1234", 1),
        (ENG_PRIYA, "AB-1234", 2),
        (ENG_PRIYA, "CD-5678", 1),
        (ENG_MARCO, "EF-9012", 1),
    ]


# Recurring busy blocks per business-day index d (0 = next business day), local hours [start, end).
_BUSY_PATTERN: dict[str, list[BusyBlock]] = {
    # Alex: mornings booked on even days, afternoons booked on odd days.
    ENG_ALEX: [
        ("even", 9, 12, "Customer site visit"),
        ("odd", 13, 17, "Lab validation"),
    ],
    # Priya: fully booked on odd days, one stand-up block otherwise.
    ENG_PRIYA: [("odd", 9, 17, "On-site install"), ("even", 9, 10, "Team stand-up")],
    ENG_MARCO: [("all", 10, 12, "Design review")],
    # Sarah: lunch + pipeline review every day, one early call on day 1.
    AM_JOHN: [("all", 12, 14, "Pipeline review"), ("day1", 9, 10, "QBR prep")],
    AM_MARY: [("all", 15, 17, "Account planning")],
}


def seed_calendar(now: datetime | None = None) -> list[CalendarRow]:
    """(owner, start_utc, end_utc, title, kind, thread_id) over the scheduling horizon."""
    from core import scheduling as sch

    rows: list[CalendarRow] = []
    for d, day in enumerate(sch.business_days(now or sch.now_local())):
        for owner, blocks in _BUSY_PATTERN.items():
            for when, h1, h2, title in blocks:
                hit = (
                    when == "all"
                    or (when == "even" and d % 2 == 0)
                    or (when == "odd" and d % 2 == 1)
                    or when == f"day{d}"
                )
                if hit:
                    rows.append(
                        (
                            owner,
                            sch.to_utc_iso(sch.at(day, h1)),
                            sch.to_utc_iso(sch.at(day, h2)),
                            title,
                            "BUSY",
                            None,
                        )
                    )
    return rows


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
        conn.executescript(SCHEMA + LIVE_SCHEMA)
        _add_columns(conn)
        if seed and conn.execute("SELECT COUNT(*) FROM orders WHERE tester_id IS NULL").fetchone()[0] == 0:
            _seed_orders(conn)
        if seed and conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 0:
            conn.executemany("INSERT INTO staff VALUES (?,?,?)", seed_staff())
            conn.executemany(
                "INSERT INTO engineer_parts VALUES (?,?,?)", seed_engineer_parts()
            )
            _seed_calendar(conn)
        _migrate(conn)


_ORDER_COLS = "order_id, part_number, customer_email, sop_owner_email, purchased_qty, status, delivered_at, return_label"


def _seed_orders(conn: sqlite3.Connection) -> None:
    conn.executemany(f"INSERT INTO orders ({_ORDER_COLS}) VALUES (?,?,?,?,?,?,?,?)", seed_orders())


def _seed_calendar(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO calendar_events (owner_email, start_at, end_at, title, kind, thread_id) "
        "VALUES (?,?,?,?,?,?)",
        seed_calendar(),
    )


def _add_columns(conn: sqlite3.Connection) -> None:
    for table, col, decl in _ADDED_COLUMNS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# Columns that may hold the demo company's old domain (emails or meeting links).
_DOMAIN_COLUMNS = {
    "orders": ("sop_owner_email",),
    "staff": ("email",),
    "engineer_parts": ("engineer_email",),
    "calendar_events": ("owner_email",),
    "tech_schedules": ("am_email", "engineer_email", "meeting_link"),
    "thread_vaults": ("vault_json",),
    "simulated_emails": ("recipient_email", "subject", "body"),
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade a DB seeded by an older version in place, keeping threads and emails. Idempotent.

    1. Older seeds put every order on the shared SOP queue; give each client its account manager.
    2. The demo company was renamed (acmesupply.com -> axmattsupply.com).
    """
    shared = (SOP_OWNER_EMAIL, SOP_OWNER_EMAIL.replace(COMPANY_DOMAIN, LEGACY_DOMAIN))
    for customer, am in ACCOUNT_MANAGERS.items():
        conn.execute(
            "UPDATE orders SET sop_owner_email = ? WHERE lower(customer_email) = ? AND sop_owner_email IN (?, ?)",
            (am, customer, *shared),
        )
    for table, cols in _DOMAIN_COLUMNS.items():
        for col in cols:
            conn.execute(
                f"UPDATE {table} SET {col} = replace({col}, ?, ?) WHERE {col} LIKE ?",
                (LEGACY_DOMAIN, COMPANY_DOMAIN, f"%{LEGACY_DOMAIN}%"),
            )


# Tables created by openai-agents SQLiteSession in the same DB file (keyed by session_id = thread_id).
AGENT_SESSION_TABLES = ("agent_messages", "agent_sessions")

_LIVE_THREADS = "(SELECT thread_id FROM thread_vaults WHERE source = 'live')"


def _delete_agent_sessions(conn: sqlite3.Connection, where: str) -> None:
    for table in AGENT_SESSION_TABLES:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            conn.execute(f"DELETE FROM {table} WHERE {where}")


def reset_db(db_path: str | None = None) -> None:
    """Sandbox reset: wipe simulated threads and restore the seed. Live (real email) data is kept."""
    init_db(db_path, seed=False)  # make sure every table/column exists (old DBs)
    with get_conn(db_path) as conn:
        live = _LIVE_THREADS
        _delete_agent_sessions(conn, f"session_id NOT IN {live}")
        conn.execute(f"DELETE FROM processed_messages WHERE thread_id NOT IN {live}")
        conn.execute(f"DELETE FROM tech_schedules WHERE thread_id NOT IN {live}")
        conn.execute(f"DELETE FROM calendar_events WHERE kind = 'BUSY' OR thread_id IS NULL OR thread_id NOT IN {live}")
        conn.execute(f"DELETE FROM thread_traces WHERE thread_id NOT IN {live}")
        conn.execute("DELETE FROM simulated_emails WHERE source != 'live'")
        conn.execute("DELETE FROM thread_vaults WHERE source != 'live'")
        conn.execute("DELETE FROM orders WHERE tester_id IS NULL")
        _seed_orders(conn)
        conn.executemany("INSERT OR IGNORE INTO staff VALUES (?,?,?)", seed_staff())
        conn.execute("DELETE FROM engineer_parts WHERE part_number IN (SELECT part_number FROM orders WHERE tester_id IS NULL)")
        conn.executemany("INSERT OR IGNORE INTO engineer_parts VALUES (?,?,?)", seed_engineer_parts())
        _seed_calendar(conn)


def reset_live(db_path: str | None = None) -> None:
    """Admin only: delete every tester and everything that came through real email.

    email_usage is kept on purpose: deleting our rows doesn't give the Resend quota back.
    """
    init_db(db_path, seed=False)
    with get_conn(db_path) as conn:
        live = _LIVE_THREADS
        _delete_agent_sessions(conn, f"session_id IN {live}")
        for table in ("processed_messages", "tech_schedules", "calendar_events", "thread_traces"):
            conn.execute(f"DELETE FROM {table} WHERE thread_id IN {live}")
        conn.execute("DELETE FROM simulated_emails WHERE source = 'live'")
        conn.execute("DELETE FROM thread_vaults WHERE source = 'live'")
        conn.execute(
            "DELETE FROM engineer_parts WHERE part_number IN (SELECT part_number FROM orders WHERE tester_id IS NOT NULL)"
        )
        conn.execute("DELETE FROM orders WHERE tester_id IS NOT NULL")
        for table in ("testers", "mail_messages", "inbound_events", "inbound_log", "internal_inbox"):
            conn.execute(f"DELETE FROM {table}")


# ---------------------------------------------------------------- idempotency


def mark_message_processed(
    message_id: str, thread_id: str, db_path: str | None = None
) -> bool:
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
        row = conn.execute(
            "SELECT * FROM thread_vaults WHERE thread_id = ?", (thread_id,)
        ).fetchone()
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
    source: str = SIMULATED_SOURCE,
) -> None:
    """Upsert the thread. `source` is set when the thread is created and never changes."""
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO thread_vaults
                 (thread_id, customer_email, vault_json, thread_status, updated_at, source)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(thread_id) DO UPDATE SET
                 vault_json = excluded.vault_json,
                 thread_status = excluded.thread_status,
                 updated_at = excluded.updated_at""",
            (thread_id, customer_email, json.dumps(vault), thread_status, now_iso(), source),
        )


def thread_source(thread_id: str, db_path: str | None = None) -> str:
    """'live' for threads that came in through real email, else 'simulated' (also for unknown threads)."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT source FROM thread_vaults WHERE thread_id = ?", (thread_id,)).fetchone()
    return row["source"] if row else SIMULATED_SOURCE


def set_thread_status(
    thread_id: str, thread_status: str, db_path: str | None = None
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE thread_vaults SET thread_status = ?, updated_at = ? WHERE thread_id = ?",
            (thread_status, now_iso(), thread_id),
        )


# -------------------------------------------------------------------- orders


def find_order(
    order_id: str, part_number: str, db_path: str | None = None
) -> dict[str, Any] | None:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ? AND part_number = ?",
            (order_id, part_number),
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


def set_return_label(
    order_id: str, part_number: str, label: str, db_path: str | None = None
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE orders SET status = 'RETURN_INITIATED', return_label = ?
               WHERE order_id = ? AND part_number = ?""",
            (label, order_id, part_number),
        )


def list_orders(db_path: str | None = None, include_live: bool = False) -> list[dict[str, Any]]:
    where = "" if include_live else "WHERE tester_id IS NULL"
    with get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM orders {where} ORDER BY order_id")]


# ---------------------------------------------------------- simulated emails


def insert_email(
    thread_id: str,
    inbox_type: str,
    recipient_email: str,
    subject: str,
    body: str,
    db_path: str | None = None,
    *,
    source: str = SIMULATED_SOURCE,
    status: str = "SIMULATED",
    delivered_to: str | None = None,
    provider_id: str | None = None,
    rfc_message_id: str | None = None,
) -> int:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO simulated_emails
               (thread_id, inbox_type, recipient_email, subject, body, created_at,
                source, status, delivered_to, provider_id, rfc_message_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (thread_id, inbox_type, recipient_email, subject, body, now_iso(),
             source, status, delivered_to, provider_id, rfc_message_id),
        )
        assert cur.lastrowid is not None  # always set after an INSERT
        return cur.lastrowid


def list_emails(db_path: str | None = None, include_live: bool = False) -> list[dict[str, Any]]:
    where = "" if include_live else "WHERE source = 'simulated'"
    with get_conn(db_path) as conn:
        return [
            dict(r)
            for r in conn.execute(f"SELECT * FROM simulated_emails {where} ORDER BY id DESC")
        ]


def list_threads(db_path: str | None = None, include_live: bool = False) -> list[dict[str, Any]]:
    where = "" if include_live else "WHERE source = 'simulated'"
    with get_conn(db_path) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM thread_vaults {where} ORDER BY updated_at DESC"
            )
        ]
    for r in rows:
        r["vault"] = json.loads(r.pop("vault_json"))
    return rows


# ------------------------------------------------------- tech support: people


def find_orders_by_part_for_customer(
    part_number: str, customer_email: str, db_path: str | None = None
) -> list[dict[str, Any]]:
    """Ownership check for tech support: orders of this part placed by this sender, newest first."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM orders WHERE part_number = ? AND lower(customer_email) = lower(?)
               ORDER BY delivered_at DESC""",
            (part_number, customer_email),
        ).fetchall()
    return [dict(r) for r in rows]


def customer_has_orders(customer_email: str, db_path: str | None = None) -> bool:
    """True if this sender has ever ordered from us (an existing customer with an account manager)."""
    with get_conn(db_path) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM orders WHERE lower(customer_email) = lower(?) LIMIT 1",
                (customer_email,),
            ).fetchone()
            is not None
        )


def part_exists(part_number: str, db_path: str | None = None) -> bool:
    with get_conn(db_path) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM orders WHERE part_number = ?", (part_number,)
            ).fetchone()
            is not None
        )


def engineers_for_part(part_number: str, db_path: str | None = None) -> list[str]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT engineer_email FROM engineer_parts WHERE part_number = ? ORDER BY priority, engineer_email",
            (part_number,),
        ).fetchall()
    return [r[0] for r in rows]


def staff_name(email: str, db_path: str | None = None) -> str:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM staff WHERE email = ?", (email,)
        ).fetchone()
    return row[0] if row else email


def list_staff(db_path: str | None = None) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM staff ORDER BY role DESC, name")
        ]


# ----------------------------------------------------- tech support: calendars


def busy_intervals(
    email: str, db_path: str | None = None, exclude_thread: str | None = None
) -> list[tuple[datetime, datetime]]:
    """Busy time for `email`. `exclude_thread` ignores that thread's own MEETING (used when moving it)."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT start_at, end_at FROM calendar_events WHERE lower(owner_email) = lower(?) "
            "AND NOT (kind = 'MEETING' AND thread_id IS ? AND ? IS NOT NULL)",
            (email, exclude_thread, exclude_thread),
        ).fetchall()
    return [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in rows]


def add_calendar_event(
    owner_email: str,
    start_utc: str,
    end_utc: str,
    title: str,
    kind: str,
    thread_id: str | None = None,
    db_path: str | None = None,
) -> int:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO calendar_events (owner_email, start_at, end_at, title, kind, thread_id) "
            "VALUES (?,?,?,?,?,?)",
            (owner_email, start_utc, end_utc, title, kind, thread_id),
        )
        assert cur.lastrowid is not None  # always set after an INSERT
        return cur.lastrowid


def delete_thread_meetings(thread_id: str, db_path: str | None = None) -> int:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM calendar_events WHERE kind = 'MEETING' AND thread_id = ?",
            (thread_id,),
        )
        return cur.rowcount


def list_calendar_events(db_path: str | None = None, include_live: bool = False) -> list[dict[str, Any]]:
    where = "" if include_live else f"WHERE thread_id IS NULL OR thread_id NOT IN {_LIVE_THREADS}"
    with get_conn(db_path) as conn:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM calendar_events {where} ORDER BY start_at, owner_email"
            )
        ]


# --------------------------------------------------- tech support: thread state


def get_tech_schedule(
    thread_id: str, db_path: str | None = None
) -> dict[str, Any] | None:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tech_schedules WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["offered_slots"] = json.loads(data.pop("offered_slots_json"))
    return data


def save_tech_schedule(
    thread_id: str, customer_email: str, db_path: str | None = None, **fields: Any
) -> dict[str, Any]:
    """Upsert: merges `fields` into the existing row (or a fresh one) and returns the result."""
    cur = get_tech_schedule(thread_id, db_path) or {
        "thread_id": thread_id,
        "customer_email": customer_email,
        "part_number": None,
        "am_email": None,
        "engineer_email": None,
        "rounds": 0,
        "offered_slots": [],
        "meeting_start": None,
        "meeting_link": None,
        "issue_text": None,
        "status": "NEW",
    }
    cur.update(fields)
    cur["updated_at"] = now_iso()
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO tech_schedules
               (thread_id, customer_email, part_number, am_email, engineer_email, rounds,
                offered_slots_json, meeting_start, meeting_link, issue_text, status, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cur["thread_id"],
                cur["customer_email"],
                cur["part_number"],
                cur["am_email"],
                cur["engineer_email"],
                cur["rounds"],
                json.dumps(cur["offered_slots"]),
                cur["meeting_start"],
                cur["meeting_link"],
                cur["issue_text"],
                cur["status"],
                cur["updated_at"],
            ),
        )
    return cur


def list_tech_schedules(db_path: str | None = None, include_live: bool = False) -> list[dict[str, Any]]:
    where = "" if include_live else f"WHERE thread_id NOT IN {_LIVE_THREADS}"
    with get_conn(db_path) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM tech_schedules {where} ORDER BY updated_at DESC"
            )
        ]
    for r in rows:
        r["offered_slots"] = json.loads(r.pop("offered_slots_json"))
    return rows


def find_upcoming_meeting(
    customer_email: str,
    part_number: str | None,
    now_utc: str,
    exclude_thread: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any] | None:
    """The customer's next booked meeting on ANOTHER thread (for this part, or their only one if part is None)."""
    with get_conn(db_path) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT thread_id, part_number FROM tech_schedules WHERE lower(customer_email) = lower(?) "
                "AND meeting_start IS NOT NULL AND meeting_start > ? AND thread_id IS NOT ? "
                "ORDER BY meeting_start",
                (customer_email, now_utc, exclude_thread),
            )
        ]
    if part_number is not None:
        rows = [r for r in rows if r["part_number"] == part_number]
    elif len(rows) != 1:
        return None  # ambiguous without a part number
    return get_tech_schedule(rows[0]["thread_id"], db_path) if rows else None


def has_upcoming_meeting(
    customer_email: str, now_utc: str, db_path: str | None = None
) -> bool:
    with get_conn(db_path) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM tech_schedules WHERE lower(customer_email) = lower(?) AND meeting_start > ? "
                "LIMIT 1",
                (customer_email, now_utc),
            ).fetchone()
            is not None
        )


def move_tech_schedule(
    from_thread: str, to_thread: str, db_path: str | None = None
) -> dict[str, Any]:
    """A new email thread adopts a booked meeting: the schedule row and its MEETING events follow it."""
    old = get_tech_schedule(from_thread, db_path)
    if old is None:
        raise KeyError(from_thread)
    new = get_tech_schedule(to_thread, db_path) or {}
    fields = {
        k: old[k]
        for k in (
            "part_number",
            "am_email",
            "engineer_email",
            "meeting_start",
            "meeting_link",
            "status",
        )
    }
    if (
        fields["status"] == "ESCALATED"
    ):  # the meeting is still booked; the new thread starts fresh
        fields["status"] = "CONFIRMED"
    fields.update(
        rounds=0,
        offered_slots=[],
        issue_text=old["issue_text"] or new.get("issue_text"),
    )
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM tech_schedules WHERE thread_id = ?", (from_thread,))
        conn.execute(
            "UPDATE calendar_events SET thread_id = ? WHERE kind = 'MEETING' AND thread_id = ?",
            (to_thread, from_thread),
        )
    return save_tech_schedule(to_thread, old["customer_email"], db_path, **fields)
