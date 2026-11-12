"""Sandbox data and live (real email) data share one DB but never reset or show together."""

import sqlite3

from core import db


def _add_live_data(path):
    db.save_thread("live-1", "tester@gmail.com", {"<ORDER_TOKEN_1>": "ORD-55555"}, "AWAITING_CUSTOMER_INFO", path, source="live")
    db.insert_email("live-1", "CUSTOMER_INBOX", "tester@gmail.com", "Re: return", "b", path, source="live", status="SENT")
    db.mark_message_processed("<m1@gmail.com>", "live-1", path)
    with db.get_conn(path) as conn:
        conn.execute("INSERT INTO testers (email, token_hash, created_at) VALUES ('tester@gmail.com', 'h', 'now')")
        conn.execute(
            "INSERT INTO orders (order_id, part_number, customer_email, sop_owner_email, purchased_qty, status, "
            "delivered_at, return_label, tester_id) VALUES ('ORD-55555','KX-4821','tester@gmail.com',?,2,'DELIVERED','2026-01-01',NULL,1)",
            (db.AM_JOHN,),
        )
        conn.execute("INSERT INTO engineer_parts VALUES (?, 'KX-4821', 1)", (db.ENG_ALEX,))
        conn.execute("INSERT INTO internal_inbox (role, thread_id, subject, body, received_at) VALUES ('AM','live-1','s','b','now')")
        conn.execute("INSERT INTO email_usage (direction, thread_id, created_at) VALUES ('sent','live-1','now')")
        conn.execute(
            "INSERT INTO calendar_events (owner_email, start_at, end_at, title, kind, thread_id) "
            "VALUES (?, '2030-01-01T10:00:00+00:00', '2030-01-01T10:30:00+00:00', 'call', 'MEETING', 'live-1')",
            (db.ENG_ALEX,),
        )


def _count(path, sql):
    with db.get_conn(path) as conn:
        return conn.execute(sql).fetchone()[0]


def test_reset_db_keeps_live_data(db_path):
    _add_live_data(db_path)
    db.save_thread("sim-1", "john@acmecorp.com", {}, "DONE", db_path)
    db.insert_email("sim-1", "CUSTOMER_INBOX", "john@acmecorp.com", "s", "b", db_path)
    db.set_return_label("ORD-9988", "AB-1234", "UPS-RMA-ORD-9988", db_path)

    db.reset_db(db_path)

    assert db.load_thread("sim-1", db_path) is None
    assert db.find_order("ORD-9988", "AB-1234", db_path)["return_label"] is None  # seed restored
    assert db.load_thread("live-1", db_path)["thread_status"] == "AWAITING_CUSTOMER_INFO"
    assert db.mark_message_processed("<m1@gmail.com>", "live-1", db_path) is False  # still claimed
    assert db.find_order("ORD-55555", "KX-4821", db_path) is not None
    assert db.engineers_for_part("KX-4821", db_path) == [db.ENG_ALEX]
    for table in ("testers", "internal_inbox", "email_usage"):
        assert _count(db_path, f"SELECT COUNT(*) FROM {table}") == 1
    assert _count(db_path, "SELECT COUNT(*) FROM simulated_emails WHERE source='live'") == 1
    assert _count(db_path, "SELECT COUNT(*) FROM calendar_events WHERE thread_id='live-1'") == 1
    assert len(db.list_orders(db_path)) == len(db.seed_orders())  # no duplicate seed rows


def test_reset_live_keeps_sandbox_and_usage(db_path):
    _add_live_data(db_path)
    db.save_thread("sim-1", "john@acmecorp.com", {}, "DONE", db_path)

    db.reset_live(db_path)

    assert db.load_thread("live-1", db_path) is None
    assert db.load_thread("sim-1", db_path) is not None
    assert db.find_order("ORD-55555", "KX-4821", db_path) is None
    assert db.engineers_for_part("KX-4821", db_path) == []
    assert db.engineers_for_part("AB-1234", db_path)  # seed untouched
    for table in ("testers", "internal_inbox", "mail_messages"):
        assert _count(db_path, f"SELECT COUNT(*) FROM {table}") == 0
    assert _count(db_path, "SELECT COUNT(*) FROM email_usage") == 1  # Resend quota isn't given back


def test_sandbox_lists_hide_live_data_unless_asked(db_path):
    _add_live_data(db_path)
    assert all(o["tester_id"] is None for o in db.list_orders(db_path))
    assert db.list_emails(db_path) == [] and db.list_threads(db_path) == []
    assert all(e["thread_id"] != "live-1" for e in db.list_calendar_events(db_path))
    assert any(o["order_id"] == "ORD-55555" for o in db.list_orders(db_path, include_live=True))
    assert len(db.list_emails(db_path, include_live=True)) == 1
    assert db.list_threads(db_path, include_live=True)[0]["source"] == "live"


def test_thread_source_is_fixed_at_creation(db_path):
    db.save_thread("live-1", "t@gmail.com", {}, "PROCESSING", db_path, source="live")
    db.save_thread("live-1", "t@gmail.com", {}, "DONE", db_path)  # later saves don't pass source
    assert db.thread_source("live-1", db_path) == "live"
    assert db.thread_source("unknown", db_path) == "simulated"


def test_old_db_gets_new_columns_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)  # the previous release's schema only
    conn.execute("INSERT INTO thread_vaults VALUES ('t1','john@acmecorp.com','{}','DONE','now')")
    conn.commit()
    conn.close()

    db.init_db(path)

    assert db.thread_source("t1", path) == "simulated"
    assert db.load_thread("t1", path) is not None
    assert db.list_orders(path)  # seeded with the new tester_id column present
