"""Customer greetings are fixed locally: never a staff name, the customer's first name when known."""

from core import db, mail
from core.mail.base import AGENT
from core.mail.greeting import first_name, fix_greeting


def test_greeting_uses_the_customer_name_or_none():
    assert fix_greeting("Hi Sarah,\n\nYour label is ready.", "Matthew") == "Hi Matthew,\n\nYour label is ready."
    assert fix_greeting("Hi Sarah,\n\nYour label is ready.", None) == "Hi,\n\nYour label is ready."
    assert fix_greeting("Dear Customer,\nThanks.", "Matthew") == "Hi Matthew,\nThanks."
    assert fix_greeting("\nHello John!\nOk", "Matthew") == "\nHi Matthew,\nOk"


def test_non_greeting_first_lines_are_left_alone():
    for body in ("Your return label is ready.", "Hi, I checked your order and it is eligible.",
                 "Hello and thank you for reaching out to us today.", ""):
        assert fix_greeting(body, "Matthew") == body


def test_first_name_only_from_a_real_looking_name():
    assert first_name("Matthew Lee") == "Matthew"
    assert first_name('"matthew lee"') == "Matthew"
    for bad in (None, "", "workformatthew@gmail.com", "123 Corp", "J"):
        assert first_name(bad) is None


def test_sandbox_agent_reply_drops_a_staff_name_but_staff_replies_keep_theirs(db_path):
    db.save_thread("t1", "john@acmecorp.com", {}, "PROCESSING", db_path)
    mail.send("t1", "CUSTOMER_INBOX", "john@acmecorp.com", "Re", "Hi Sarah,\n\nDone.", db_path, author=AGENT)
    mail.send("t1", "CUSTOMER_INBOX", "john@acmecorp.com", "Re", "Hi John,\n\nSarah here.", db_path,
              author="Sarah Kim (Account Manager)")
    bodies = [e["body"] for e in sorted(db.list_emails(db_path), key=lambda e: e["id"])]
    assert bodies == ["Hi,\n\nDone.", "Hi John,\n\nSarah here."]
