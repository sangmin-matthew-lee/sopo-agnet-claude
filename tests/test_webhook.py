"""POST /webhook/resend: signature, dedup, routing, allow list, loop prevention. Resend is mocked."""

import json
import time

import pytest
from fastapi.testclient import TestClient

import main
import pipeline
from core import db
from core.jev_router import parse_response
from core.mail import threads, usage
from live import inbound, signature

SECRET = "whsec_plJ3nmyCDGBKInavdOK15jsl"
TESTER = "tester@gmail.com"
SUPPORT, AM, ENG = "support@test.example.com", "am@test.example.com", "eng@test.example.com"


@pytest.fixture
def client(resend, monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "letmein")
    main.rate_limiter.reset()
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def calls(monkeypatch):
    """Record pipeline calls instead of running the pipeline."""
    seen = []

    async def fake(sender, thread_id, message_id, body, emit=None, source="simulated"):
        seen.append(dict(sender=sender, thread_id=thread_id, message_id=message_id, body=body, source=source))
        return {"status": "OK", "trace": []}

    monkeypatch.setattr(pipeline, "process_email", fake)
    return seen


def _tester(email=TESTER):
    with db.get_conn() as conn:
        conn.execute("INSERT INTO testers (email, token_hash, created_at) VALUES (?, ?, 'now')", (email, f"h-{email}"))


def post_event(client, email_id, svix_id=None, secret=SECRET, ts=None, event_type="email.received"):
    body = json.dumps({"type": event_type, "created_at": "2026-11-11T00:00:00Z",
                       "data": {"email_id": email_id, "to": [SUPPORT]}}).encode()
    svix_id = svix_id or f"msg_{email_id}"
    ts = str(ts or int(time.time()))
    sig = signature.sign(secret, svix_id, ts, body)
    return client.post("/webhook/resend", content=body, headers={
        "svix-id": svix_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}", "content-type": "application/json"})


def received(resend, email_id, sender=TESTER, to=(SUPPORT,), text="Hi, I want to return something.",
             message_id=None, headers=None, auth="pass", subject="Return request"):
    resend.received[email_id] = {
        "object": "email", "id": email_id, "from": f"Test Person <{sender}>", "to": list(to),
        "subject": subject, "text": text, "html": None, "headers": headers or {},
        "message_id": message_id or f"<{email_id}@mail.gmail.com>",
        "authentication": {"spf": auth, "dkim": auth, "dmarc": auth},
    }


def _log(decision=None):
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM inbound_log ORDER BY id")]
    return [r for r in rows if decision is None or r["decision"] == decision]


def _received_count():
    with db.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM email_usage WHERE direction='received'").fetchone()[0]


# ------------------------------------------------------------------ signature


def test_svix_reference_vector():
    body = b'{"event_type":"ping","data":{"success":true}}'
    assert signature.verify(SECRET, "msg_loFOjxBNrRLzqYUf", "1731705121",
                            "v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0=", body, now=1731705121)
    # several signatures (key rotation): any match is enough
    assert signature.verify(SECRET, "msg_loFOjxBNrRLzqYUf", "1731705121",
                            "v1,bm9wZQ== v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0=", body, now=1731705121)
    assert not signature.verify(SECRET, "msg_loFOjxBNrRLzqYUf", "1731705121",
                                "v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0=", body + b" ", now=1731705121)


def test_valid_signature_accepted(client, resend, calls):
    _tester()
    received(resend, "e1")
    assert post_event(client, "e1").status_code == 200
    assert len(calls) == 1


def test_bad_signature_rejected(client, resend, calls):
    received(resend, "e1")
    r = post_event(client, "e1", secret="whsec_" + "QUJDREVGR0hJSktMTU5PUA==")
    assert r.status_code == 401 and calls == [] and resend.requests == []


def test_old_timestamp_rejected(client, resend, calls):
    r = post_event(client, "e1", ts=int(time.time()) - 3600)
    assert r.status_code == 401


def test_missing_signature_headers_rejected(client):
    assert client.post("/webhook/resend", json={"type": "email.received"}).status_code == 401


def test_webhook_off_in_simulated_mode(client, monkeypatch):
    monkeypatch.setenv("MAIL_MODE", "simulated")
    assert post_event(client, "e1").status_code == 503


def test_no_passcode_needed(client, resend, calls):
    _tester()
    received(resend, "e1")
    assert post_event(client, "e1").status_code == 200  # no X-Demo-Passcode header sent


def test_other_event_types_are_acknowledged_and_ignored(client, resend, calls):
    assert post_event(client, "e1", event_type="email.delivered").json()["ignored"] == "email.delivered"
    assert calls == [] and resend.requests == []


# ---------------------------------------------------------------------- dedup


def test_same_event_twice_runs_once(client, resend, calls):
    _tester()
    received(resend, "e1")
    post_event(client, "e1", svix_id="msg_A")
    assert post_event(client, "e1", svix_id="msg_A").json()["duplicate"] is True  # Svix retry
    assert post_event(client, "e1", svix_id="msg_B").json()["duplicate"] is True  # same email, new event
    assert len(calls) == 1 and _received_count() == 1


def test_same_message_id_under_new_email_id_runs_once(client, resend, calls):
    _tester()
    received(resend, "e1", message_id="<same@gmail.com>")
    received(resend, "e2", message_id="<same@gmail.com>")
    post_event(client, "e1")
    post_event(client, "e2")
    assert len(calls) == 1
    assert _log("IGNORED")[0]["reason"] == "duplicate Message-ID"


# ----------------------------------------------------- allow list / loop guard


def test_unregistered_sender_ignored_but_counted(client, resend, calls):
    received(resend, "e1", sender="stranger@gmail.com")
    post_event(client, "e1")
    assert calls == [] and _received_count() == 1
    assert _log("IGNORED")[0]["reason"] == "sender is not a registered tester"
    assert resend.sent() == []  # no reply to strangers


@pytest.mark.parametrize("sender,headers,reason", [
    (TESTER, {"Auto-Submitted": "auto-replied"}, "Auto-Submitted: auto-replied"),
    (TESTER, {"auto-submitted": "auto-generated"}, "Auto-Submitted: auto-generated"),
    ("MAILER-DAEMON@gmail.com", {}, "bounce"),
    ("postmaster@gmail.com", {}, "bounce"),
    ("sopo-agent@test.example.com", {}, "own domain"),
])
def test_loop_mail_ignored(client, resend, calls, sender, headers, reason):
    _tester(sender.lower())  # even if somehow registered
    received(resend, "e1", sender=sender, headers=headers)
    post_event(client, "e1")
    assert calls == [] and _received_count() == 1
    assert reason in _log("IGNORED")[0]["reason"]


def test_auto_submitted_no_is_a_person(client, resend, calls):
    _tester()
    received(resend, "e1", headers={"Auto-Submitted": "no"})
    post_event(client, "e1")
    assert len(calls) == 1


def test_forged_sender_without_dkim_ignored(client, resend, calls):
    _tester()
    received(resend, "e1", auth="fail")
    post_event(client, "e1")
    assert calls == [] and "authentication" in _log("IGNORED")[0]["reason"]


def test_sender_auth_check_can_be_turned_off(client, resend, calls, monkeypatch):
    monkeypatch.setenv("REQUIRE_SENDER_AUTH", "false")
    _tester()
    received(resend, "e1", auth="fail")
    post_event(client, "e1")
    assert len(calls) == 1


# ------------------------------------------------------------------- threading


def test_new_email_starts_a_live_thread(client, resend, calls):
    _tester()
    received(resend, "e1", text="Return ord/55555 please\n\nOn Mon, Bob wrote:\n> old")
    post_event(client, "e1")
    [c] = calls
    assert c["thread_id"].startswith("live-") and c["source"] == "live"
    assert c["sender"] == TESTER and c["message_id"] == "<e1@mail.gmail.com>"
    assert threads.find_thread(["<e1@mail.gmail.com>"]) is None  # thread row is created by the pipeline


def test_in_reply_to_joins_the_existing_thread(client, resend, calls):
    _tester()
    db.save_thread("live-abc", TESTER, {}, "AWAITING_CUSTOMER_INFO", source="live")
    threads.remember("<sopo-1@test.example.com>", "live-abc", threads.OUT, "Re: Return request")
    received(resend, "e2", headers={"In-Reply-To": "<sopo-1@test.example.com>",
                                    "References": "<e1@mail.gmail.com> <sopo-1@test.example.com>"})
    post_event(client, "e2")
    assert calls[0]["thread_id"] == "live-abc"
    with db.get_conn() as conn:
        usage_row = conn.execute("SELECT thread_id FROM email_usage WHERE direction='received'").fetchone()
    assert usage_row["thread_id"] == "live-abc"


def test_reply_to_a_sandbox_thread_id_never_joins_it(client, resend, calls):
    _tester()
    db.save_thread("th-1", TESTER, {}, "AWAITING_CUSTOMER_INFO")  # simulated
    threads.remember("<x@y>", "th-1", threads.OUT, "s")
    received(resend, "e1", headers={"In-Reply-To": "<x@y>"})
    post_event(client, "e1")
    assert calls[0]["thread_id"] != "th-1"


# --------------------------------------------------------------- internal inbox


def _internal_rows():
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM internal_inbox ORDER BY id")]


def test_agent_copy_to_am_is_stored_with_its_thread(client, resend, calls):
    db.save_thread("live-abc", TESTER, {}, "MEETING_CONFIRMED", source="live")
    received(resend, "e1", sender="sopo-agent@test.example.com", to=(AM,),
             headers={"X-SOPO-Thread": "live-abc", "Auto-Submitted": "auto-generated"}, subject="[Invite] call")
    post_event(client, "e1")
    [row] = _internal_rows()
    assert row["role"] == "AM" and row["thread_id"] == "live-abc" and row["subject"] == "[Invite] call"
    assert calls == []  # never goes into the pipeline


def test_forged_thread_header_is_not_trusted(client, resend, calls):
    db.save_thread("live-abc", TESTER, {}, "MEETING_CONFIRMED", source="live")
    received(resend, "e1", sender="someone@gmail.com", to=(ENG,), headers={"X-SOPO-Thread": "live-abc"})
    post_event(client, "e1")
    [row] = _internal_rows()
    assert row["role"] == "ENGINEER" and row["thread_id"] is None


def test_unknown_recipient_only_logged(client, resend, calls):
    _tester()
    received(resend, "e1", to=("sales@test.example.com",))
    post_event(client, "e1")
    assert calls == [] and _internal_rows() == []
    assert _log("IGNORED")[0]["reason"] == "not addressed to a known inbox"


# ------------------------------------------------------ end to end (real pipeline)


def _jev(spam=0.02, intent="non_sop", conf=0.95, score=0.1):
    return parse_response({"answers": {
        "is_spam_or_injection": {"type": "noul", "noul": spam},
        "intent": {"type": "choice", "choice": intent, "confidence": conf, "probabilities": {intent: conf}},
        "frustration_level": {"type": "score", "score": score, "probabilities": {}},
    }})


def test_live_email_gets_a_real_threaded_reply(client, resend, monkeypatch):
    _tester()
    jev_calls = []

    async def fake_classify(masked, **kw):
        jev_calls.append(masked)
        return _jev()

    monkeypatch.setattr(pipeline, "classify", fake_classify)
    received(resend, "e1", text="Can I get a quote for 50 controllers?", subject="Quote")
    post_event(client, "e1")

    thread = db.list_threads(include_live=True)[0]
    assert thread["source"] == "live" and thread["thread_status"] == "ESCALATED_TO_HUMAN"
    to_customer = [m for m in resend.sent() if m["to"] == [TESTER]]
    to_am = [m for m in resend.sent() if AM in m["to"][0]]
    assert len(to_customer) == 1 and len(to_am) == 1
    assert to_customer[0]["subject"] == "Re: Quote"
    assert to_customer[0]["headers"]["In-Reply-To"] == "<e1@mail.gmail.com>"
    assert to_am[0]["headers"]["X-SOPO-Thread"] == thread["thread_id"]
    # the AM sees the customer's email and the holding reply the customer got
    assert "Can I get a quote for 50 controllers?" in to_am[0]["text"]
    assert to_customer[0]["text"].strip() in to_am[0]["text"]
    assert db.list_emails() == []  # nothing in the sandbox view
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM thread_traces").fetchone()[0] == 1
    assert usage.snapshot()["daily_used"] == 3  # 1 received + 2 sent


def test_live_email_with_no_quota_left_escalates_without_sending(client, resend, monkeypatch):
    monkeypatch.setenv("RESEND_DAILY_LIMIT", "1")  # the incoming email itself uses the last one
    _tester()

    async def boom(*a, **kw):
        raise AssertionError("Jev must not be called")

    monkeypatch.setattr(pipeline, "classify", boom)
    received(resend, "e1")
    post_event(client, "e1")

    thread = db.list_threads(include_live=True)[0]
    assert thread["thread_status"] == "ESCALATED_TO_HUMAN"
    assert resend.sent() == []
    assert {e["status"] for e in db.list_emails(include_live=True)} == {"BLOCKED_QUOTA"}


def test_pipeline_crash_fails_closed(client, resend, monkeypatch):
    _tester()

    async def crash(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "process_email", crash)
    received(resend, "e1")
    post_event(client, "e1")
    thread = db.list_threads(include_live=True)[0]
    assert thread["thread_status"] == "ESCALATED_TO_HUMAN" and thread["source"] == "live"
    assert any("live processing error" in m["text"] for m in resend.sent())


def test_inbound_claim_is_atomic(db_path):
    assert inbound.claim("svix:x") is True
    assert inbound.claim("svix:x") is False


# ----------------------------------------------------- customer writes to am@ / eng@


def test_tester_mail_to_am_goes_to_the_agent_and_the_am_tab(client, resend, calls):
    _tester()
    received(resend, "e1", to=(AM,), text="Hi Sarah, please return ORD-12345.")
    post_event(client, "e1")
    [c] = calls
    assert c["thread_id"].startswith("live-") and c["source"] == "live"
    assert threads.mailbox(c["thread_id"]) == threads.AM
    [row] = _internal_rows()
    assert row["role"] == "AM" and row["thread_id"] == c["thread_id"] and "please return" in row["body"]
    assert _log("PIPELINE")[0]["recipient"] == AM


def test_non_tester_mail_to_am_is_internal_only(client, resend, calls):
    received(resend, "e1", sender="someone@gmail.com", to=(AM,))
    post_event(client, "e1")
    assert calls == [] and _internal_rows()[0]["thread_id"] is None


def test_support_in_cc_wins(client, resend, calls):
    _tester()
    received(resend, "e1", to=(AM, SUPPORT))
    post_event(client, "e1")
    assert threads.mailbox(calls[0]["thread_id"]) == threads.SUPPORT and _internal_rows() == []


def test_agent_answers_mail_to_am_from_am_signed_as_ai(client, resend, monkeypatch):
    _tester()

    async def fake_classify(masked, **kw):
        return _jev()

    monkeypatch.setattr(pipeline, "classify", fake_classify)
    received(resend, "e1", to=(AM,), text="Can I get a quote for 50 controllers?", subject="Quote")
    post_event(client, "e1")

    [reply] = [m for m in resend.sent() if m["to"] == [TESTER]]
    assert reply["from"] == f"SOPO AI Assistant <{AM}>" and reply["reply_to"] == AM
    assert reply["subject"] == "Re: Quote" and reply["headers"]["In-Reply-To"] == "<e1@mail.gmail.com>"
    assert "— SOPO AI Assistant, AxMatt Supply" in reply["text"]
    assert "This reply was written by an AI agent." in reply["text"]


def test_customer_reply_to_am_stays_in_the_thread(client, resend, calls):
    _tester()
    db.save_thread("live-abc", TESTER, {}, "AWAITING_CUSTOMER_INFO", source="live")
    threads.remember("<e1@mail.gmail.com>", "live-abc", threads.IN, "Return", mailbox=threads.AM)
    threads.remember("<sopo-1@test.example.com>", "live-abc", threads.OUT, "Re: Return")
    received(resend, "e2", to=(AM,), headers={"In-Reply-To": "<sopo-1@test.example.com>"})
    post_event(client, "e2")
    assert calls[0]["thread_id"] == "live-abc"


async def test_replies_on_one_thread_are_processed_one_at_a_time(client, resend, monkeypatch):
    """Two replies sent seconds apart must not run the pipeline for the same thread concurrently."""
    import asyncio

    _tester()
    running, overlap, threads_seen = 0, [], []

    async def slow(sender, thread_id, message_id, body, emit=None, source="simulated"):
        nonlocal running
        running += 1
        overlap.append(running)
        threads_seen.append(thread_id)
        if db.load_thread(thread_id) is None:
            db.save_thread(thread_id, sender, {}, "PROCESSING", source=db.LIVE_SOURCE)
        await asyncio.sleep(0.05)
        running -= 1
        return {"status": "OK", "trace": []}

    monkeypatch.setattr(pipeline, "process_email", slow)
    received(resend, "e1", message_id="<first@mail.gmail.com>")
    await inbound.handle("e1", 0)
    for eid in ("e2", "e3"):
        received(resend, eid, text="Tuesday works.", headers={"In-Reply-To": "<first@mail.gmail.com>"})
    await asyncio.gather(inbound.handle("e2", 0), inbound.handle("e3", 0))
    assert len(set(threads_seen)) == 1 and len(threads_seen) == 3
    assert max(overlap) == 1
