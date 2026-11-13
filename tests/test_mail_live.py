"""ResendTransport, threading headers and quota tracking. Resend is mocked (tests/conftest.py)."""

from datetime import datetime, timedelta, timezone

import pytest

from core import db, mail
from core.mail import threads, usage
from core.mail.base import BLOCKED_QUOTA, FAILED, SENT

TESTER = "tester@gmail.com"


def _tester(path, email=TESTER):
    with db.get_conn(path) as conn:
        conn.execute("INSERT INTO testers (email, token_hash, created_at) VALUES (?, ?, 'now')", (email, f"h-{email}"))


def _live_thread(path, thread="live-1", email=TESTER, incoming="<c1@gmail.com>", subject="Return request"):
    db.save_thread(thread, email, {}, "PROCESSING", path, source="live")
    threads.remember(incoming, thread, threads.IN, subject, path)


def _log(path):
    return db.list_emails(path, include_live=True)


def test_simulated_thread_never_calls_resend(resend, db_path):
    db.save_thread("sim-1", "john@acmecorp.com", {}, "PROCESSING", db_path)
    mail.send("sim-1", "CUSTOMER_INBOX", "john@acmecorp.com", "s", "b", db_path)
    mail.send("never-saved", "SOP_EMPLOYEE_INBOX", db.AM_JOHN, "s", "b", db_path)
    assert resend.requests == []
    assert {e["status"] for e in _log(db_path)} == {"SIMULATED"}
    assert usage.snapshot(db_path)["daily_used"] == 0


def test_customer_reply_is_threaded(resend, db_path):
    _tester(db_path)
    _live_thread(db_path)
    mail.send("live-1", "CUSTOMER_INBOX", TESTER, "Your return label", "Here it is", db_path)

    [sent] = resend.sent()
    h = sent["headers"]
    assert sent["to"] == [TESTER] and sent["subject"] == "Re: Return request"
    assert h["In-Reply-To"] == "<c1@gmail.com>" and h["References"].startswith("<c1@gmail.com>")
    assert h["Auto-Submitted"] == "auto-replied" and sent["reply_to"] == "support@test.example.com"
    assert h["Message-ID"].endswith("@test.example.com>")
    assert "X-SOPO-Thread" not in h
    row = _log(db_path)[0]
    assert row["status"] == SENT and row["source"] == "live" and row["rfc_message_id"] == h["Message-ID"]
    assert threads.find_thread([h["Message-ID"]], db_path) == "live-1"  # a reply to our mail threads back
    assert usage.snapshot(db_path)["daily_used"] == 1

    mail.send("live-1", "CUSTOMER_INBOX", TESTER, "Follow up", "x", db_path)
    refs = resend.sent()[1]["headers"]["References"].split()
    assert refs == ["<c1@gmail.com>", h["Message-ID"]]


def test_staff_mail_goes_to_shared_test_inboxes_with_thread_header(resend, db_path):
    _live_thread(db_path)
    mail.send("live-1", "ENGINEER_INBOX", db.ENG_ALEX, "[Invite] call", "b", db_path)
    mail.send("live-1", "SOP_EMPLOYEE_INBOX", db.AM_JOHN, "[Invite] call", "b", db_path)
    eng, am = resend.sent()
    assert eng["to"] == ["Alex Chen (Engineer) <eng@test.example.com>"]
    assert am["to"] == ["Sarah Kim (Account Manager) <am@test.example.com>"]
    assert eng["headers"]["X-SOPO-Thread"] == "live-1" == am["headers"]["X-SOPO-Thread"]
    assert eng["from"] == "SOPO Agent <sopo-agent@test.example.com>"
    # the log keeps the real staff identity; delivered_to shows where it actually went
    rows = {r["inbox_type"]: r for r in _log(db_path)}
    assert rows["ENGINEER_INBOX"]["recipient_email"] == db.ENG_ALEX
    assert "eng@test.example.com" in rows["ENGINEER_INBOX"]["delivered_to"]


def test_staff_mail_quotes_the_whole_thread(resend, db_path):
    _tester(db_path)
    db.save_thread("live-1", TESTER, {}, "PROCESSING", db_path, source="live")
    threads.remember("<c1@gmail.com>", "live-1", threads.IN, "Return request", db_path,
                     sender=TESTER, body="Please return ORD-12345 part KX-4821")
    mail.send("live-1", "CUSTOMER_INBOX", TESTER, "Your label", "Label UPS-RMA-ORD-12345 is ready.", db_path)
    mail.send("live-1", "SOP_EMPLOYEE_INBOX", db.AM_JOHN, "[SOP] Your label", "Return approved.", db_path)

    am = resend.sent()[-1]
    text = am["text"]
    assert text.startswith("Return approved.")
    # newest first: the agent's reply, then the customer's original email
    assert text.index("Label UPS-RMA-ORD-12345 is ready.") < text.index("Please return ORD-12345 part KX-4821")
    assert f"From: {TESTER}" in text and "From: SOPO AI agent" in text
    assert am["headers"]["In-Reply-To"] == "<c1@gmail.com>"
    assert am["headers"]["References"].split()[0] == "<c1@gmail.com>"
    assert _log(db_path)[0]["body"] == text  # the sent log shows what was really sent


def test_staff_mail_without_a_stored_customer_email_is_unchanged(resend, db_path):
    _live_thread(db_path)  # Message-ID only, no body
    mail.send("live-1", "SOP_EMPLOYEE_INBOX", db.AM_JOHN, "s", "Just this.", db_path)
    assert resend.sent()[0]["text"] == "Just this."


def test_never_sends_to_an_address_that_is_not_a_tester(resend, db_path):
    _live_thread(db_path, email="stranger@gmail.com")
    with pytest.raises(mail.MailNotSent):
        mail.send("live-1", "CUSTOMER_INBOX", "stranger@gmail.com", "s", "b", db_path)
    assert resend.requests == [] and _log(db_path)[0]["status"] == FAILED


def test_resend_error_is_logged_and_raised(resend, db_path):
    _tester(db_path)
    _live_thread(db_path)
    resend.send_status = 422
    with pytest.raises(mail.MailNotSent):
        mail.send("live-1", "CUSTOMER_INBOX", TESTER, "s", "b", db_path)
    assert _log(db_path)[0]["status"] == FAILED
    assert usage.snapshot(db_path)["daily_used"] == 0  # nothing was delivered


def test_quota_zero_blocks_sending(resend, db_path, monkeypatch):
    monkeypatch.setenv("RESEND_DAILY_LIMIT", "2")
    _tester(db_path)
    _live_thread(db_path)
    usage.record(usage.RECEIVED, "live-1", db_path)
    usage.record(usage.RECEIVED, None, db_path)  # an ignored email still used quota

    with pytest.raises(mail.MailNotSent):
        mail.send("live-1", "CUSTOMER_INBOX", TESTER, "s", "b", db_path)
    mail.send("live-1", "SOP_EMPLOYEE_INBOX", db.SOP_OWNER_EMAIL, "alert", "b", db_path, best_effort=True)

    assert resend.sent() == []
    assert [r["status"] for r in _log(db_path)] == [BLOCKED_QUOTA, BLOCKED_QUOTA]
    snap = usage.snapshot(db_path)
    assert snap["exhausted"] and snap["remaining"] == 0 and snap["scenarios_left"] == 0


def test_daily_count_resets_at_utc_midnight(db_path):
    midnight = datetime(2026, 11, 11, tzinfo=timezone.utc)
    with db.get_conn(db_path) as conn:
        for t in (midnight - timedelta(seconds=1), midnight, midnight + timedelta(hours=3)):
            conn.execute("INSERT INTO email_usage (direction, created_at) VALUES ('sent', ?)",
                         (t.isoformat(timespec="seconds"),))
    snap = usage.snapshot(db_path, now=midnight + timedelta(hours=5))
    assert snap["daily_used"] == 2 and snap["daily_remaining"] == 98
    assert snap["monthly_used"] == 3 and snap["monthly_remaining"] == 2997
    assert snap["resets_at"] == "2026-11-12T00:00:00+00:00" and snap["source"] == "local"


def test_resend_usage_api_value_wins(resend, db_path):
    usage.record(usage.SENT, None, db_path)  # local count is behind the real one
    resend.usage = {"emails": {"daily": {"used": 90, "limit": 100}, "monthly": {"used": 400, "limit": 3000}}}
    snap = usage.snapshot(db_path)
    assert snap["source"] == "resend_api"
    assert snap["daily_remaining"] == 10 and snap["monthly_remaining"] == 2600 and snap["low"]


def test_send_response_quota_headers_are_used(resend, db_path):
    _tester(db_path)
    _live_thread(db_path)
    resend.quota_headers = {"x-resend-daily-quota": "57", "x-resend-monthly-quota": "800"}
    mail.send("live-1", "CUSTOMER_INBOX", TESTER, "s", "b", db_path)
    snap = usage.snapshot(db_path)
    assert snap["source"] == "resend_headers"
    assert snap["daily_used"] == 57 and snap["monthly_used"] == 800


def test_scenarios_left_uses_recent_thread_average(db_path):
    assert usage.snapshot(db_path)["avg_per_scenario"] == 6.0  # default with no data
    for thread, n in (("a", 4), ("b", 8)):
        for _ in range(n):
            usage.record(usage.SENT, thread, db_path)
    snap = usage.snapshot(db_path)
    assert snap["avg_per_scenario"] == 6.0 and snap["avg_sample_threads"] == 2
    assert snap["scenarios_left"] == (100 - 12) // 6
