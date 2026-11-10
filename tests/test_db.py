from datetime import datetime, timezone

import db


def _age_days(iso: str) -> int:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).days


def test_schema_creates_all_tables(db_path):
    with db.get_conn(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(db.APP_TABLES) <= tables


def test_seed_happy_path_order(db_path):
    order = db.find_order("ORD-9988", "AB-1234", db_path)
    assert order["customer_email"] == "john@acmecorp.com"
    assert order["status"] == "DELIVERED"
    assert order["return_label"] is None
    assert _age_days(order["delivered_at"]) == 10


def test_seed_expired_order(db_path):
    rows = [o for o in db.list_orders(db_path) if o["order_id"] == "ORD-1111"]
    assert len(rows) == 1
    assert _age_days(rows[0]["delivered_at"]) == 45


def test_init_db_is_idempotent(db_path):
    db.init_db(db_path)
    db.init_db(db_path)
    assert len(db.list_orders(db_path)) == len(db.seed_orders())


def test_reset_db_restores_seed_and_clears_state(db_path):
    db.set_return_label("ORD-9988", "AB-1234", "UPS-RMA-ORD-9988", db_path)
    db.save_thread("t1", "john@acmecorp.com", {"<ORDER_TOKEN_1>": "ORD-9988"}, "DONE", db_path)
    db.mark_message_processed("m1", "t1", db_path)
    db.insert_email("t1", "CUSTOMER_INBOX", "john@acmecorp.com", "s", "b", db_path)

    db.reset_db(db_path)

    order = db.find_order("ORD-9988", "AB-1234", db_path)
    assert order["status"] == "DELIVERED" and order["return_label"] is None
    assert db.load_thread("t1", db_path) is None
    assert not db.is_message_processed("m1", db_path)
    assert db.list_emails(db_path) == []


def test_message_idempotency(db_path):
    assert db.mark_message_processed("m1", "t1", db_path) is True
    assert db.mark_message_processed("m1", "t1", db_path) is False
    assert db.is_message_processed("m1", db_path)


def test_thread_vault_roundtrip(db_path):
    db.save_thread("t1", "john@acmecorp.com", {"<PART_TOKEN_1>": "AB-1234"}, "AWAITING_CUSTOMER_INFO", db_path)
    db.save_thread("t1", "john@acmecorp.com", {"<PART_TOKEN_1>": "AB-1234", "<ORDER_TOKEN_1>": "ORD-9988"}, "PROCESSING", db_path)
    t = db.load_thread("t1", db_path)
    assert t["vault"] == {"<PART_TOKEN_1>": "AB-1234", "<ORDER_TOKEN_1>": "ORD-9988"}
    assert t["thread_status"] == "PROCESSING"


def test_three_way_order_match(db_path):
    assert db.find_order_for_customer("ORD-9988", "AB-1234", "JOHN@acmecorp.com", db_path)
    assert db.find_order_for_customer("ORD-9988", "AB-1234", "hacker@gmail.com", db_path) is None


def test_orders_have_named_account_managers(db_path):
    owners = {o["customer_email"]: o["sop_owner_email"] for o in db.list_orders(db_path)}
    assert owners == db.ACCOUNT_MANAGERS
    assert all(p["email"].endswith("@axmattsupply.com") for p in db.list_staff(db_path))


def test_migrate_upgrades_legacy_db_in_place(db_path):
    old = "acmesupply.com"
    with db.get_conn(db_path) as conn:
        conn.execute(f"UPDATE orders SET sop_owner_email = 'sop-team@{old}'")
        conn.execute(f"UPDATE staff SET email = replace(email, 'axmattsupply.com', '{old}')")
        conn.execute(f"UPDATE calendar_events SET owner_email = replace(owner_email, 'axmattsupply.com', '{old}')")
    db.save_thread("t1", "john@acmecorp.com", {"<LINK_TOKEN_1>": f"https://meet.{old}/TS-1"}, "DONE", db_path)
    db.insert_email("t1", "SOP_EMPLOYEE_INBOX", f"sarah.kim@{old}", "s", f"join https://meet.{old}/TS-1", db_path)

    db.init_db(db_path)  # what the server does on startup
    db.init_db(db_path)  # idempotent

    assert {o["customer_email"]: o["sop_owner_email"] for o in db.list_orders(db_path)} == db.ACCOUNT_MANAGERS
    assert len(db.list_staff(db_path)) == len(db.seed_staff())
    assert all(p["email"].endswith("@axmattsupply.com") for p in db.list_staff(db_path))
    assert all(old not in e["owner_email"] for e in db.list_calendar_events(db_path))
    assert db.load_thread("t1", db_path)["vault"]["<LINK_TOKEN_1>"] == "https://meet.axmattsupply.com/TS-1"
    mail = db.list_emails(db_path)[0]
    assert mail["recipient_email"] == "sarah.kim@axmattsupply.com" and old not in mail["body"]
    assert db.busy_intervals(db.AM_JOHN, db_path)  # calendars still line up with the renamed staff
