"""Tester sign-up, test orders, per-tester isolation, and sandbox/live coexistence. Resend is mocked."""

import json

import pytest
from fastapi.testclient import TestClient

import main
import pipeline
from core import db
from core.jev_router import parse_response
from core.mail import threads
from core.masker import mask_text
from core.ratelimit import register_limiter, tester_limiter

H = {"X-Demo-Passcode": "letmein"}
INVITE = "invite-123"


@pytest.fixture
def client(resend, monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "letmein")
    for limiter in (main.rate_limiter, register_limiter, tester_limiter):
        limiter.reset()
    with TestClient(main.app) as c:
        yield c


def register(client, email, code=INVITE):
    return client.post("/tester/register", json={"invite_code": code, "email": email})


def token_of(resp):
    return resp.json()["dashboard_url"].rsplit("/t/", 1)[1]


# --------------------------------------------------------------------- sign-up


def test_register_returns_private_dashboard_link(client):
    r = register(client, "Alice@Gmail.com")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "alice@gmail.com" and body["support_address"] == "support@test.example.com"
    assert body["dashboard_url"].startswith("https://sopo.example.com/t/")
    token = token_of(r)
    assert len(token) >= 40
    with db.get_conn() as conn:
        stored = conn.execute("SELECT token_hash FROM testers").fetchone()[0]
    assert stored != token and token not in stored  # only the hash is kept


def test_register_needs_the_invite_code(client):
    assert register(client, "a@gmail.com", code="wrong").status_code == 401
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM testers").fetchone()[0] == 0


def test_register_closed_without_invite_code_env(client, monkeypatch):
    monkeypatch.delenv("TESTER_INVITE_CODE")
    assert register(client, "a@gmail.com").status_code == 503


def test_register_is_rate_limited(client):
    codes = [register(client, f"a{i}@gmail.com", code="guess").status_code for i in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_tester_limit(client, monkeypatch):
    monkeypatch.setenv("MAX_TESTERS", "2")
    assert register(client, "a@gmail.com").status_code == 200
    assert register(client, "b@gmail.com").status_code == 200
    assert register(client, "c@gmail.com").status_code == 409


def test_same_email_cannot_take_over_a_dashboard(client):
    first = token_of(register(client, "a@gmail.com"))
    assert register(client, "a@gmail.com").status_code == 409
    assert client.get(f"/t/{first}/state").status_code == 200  # the original link still works


def test_our_own_addresses_cannot_register(client):
    assert register(client, "am@test.example.com").status_code == 400


# ---------------------------------------------------------------------- orders


def test_create_orders_returnable_and_expired(client):
    token = token_of(register(client, "a@gmail.com"))
    ok = client.post(f"/t/{token}/orders", json={"delivered": "10d"}).json()
    late = client.post(f"/t/{token}/orders", json={"delivered": "45d"}).json()
    assert ok["returnable"] is True and late["returnable"] is False
    for o in (ok, late):
        row = db.find_order_for_customer(o["order_id"], o["part_number"], "a@gmail.com")
        assert row is not None and row["sop_owner_email"] == db.AM_JOHN and row["tester_id"] is not None
        assert db.engineers_for_part(o["part_number"]) == [db.ENG_ALEX, db.ENG_PRIYA]
        masked = mask_text(f"return {o['order_id']} part {o['part_number']}").masked_text
        assert o["order_id"] not in masked and o["part_number"] not in masked  # the masker catches both
    assert db.find_order_for_customer(ok["order_id"], ok["part_number"], "b@gmail.com") is None


def test_order_option_is_validated(client):
    token = token_of(register(client, "a@gmail.com"))
    assert client.post(f"/t/{token}/orders", json={"delivered": "1d"}).status_code == 422


# ------------------------------------------------------------------- isolation


def _seed_tester_data(token, email, tag):
    """A live thread with a sent mail, an AM copy, an engineer copy and a trace, all owned by `email`."""
    thread = f"live-{tag}"
    db.save_thread(thread, email, {}, "MEETING_CONFIRMED", source="live")
    db.insert_email(thread, "CUSTOMER_INBOX", email, f"Re: {tag}", f"secret-{tag}", source="live", status="SENT")
    db.save_trace(thread, f"<{tag}@gmail.com>", [{"phase": "phase1", "title": f"trace-{tag}"}])
    with db.get_conn() as conn:
        for role in ("AM", "ENGINEER"):
            conn.execute("INSERT INTO internal_inbox (role, thread_id, subject, body, received_at) "
                         "VALUES (?,?,?,?, 'now')", (role, thread, f"{role}-{tag}", f"body-{tag}"))
    return thread


def test_testers_never_see_each_other(client):
    ta = token_of(register(client, "a@gmail.com"))
    tb = token_of(register(client, "b@gmail.com"))
    oa = client.post(f"/t/{ta}/orders", json={"delivered": "10d"}).json()
    ob = client.post(f"/t/{tb}/orders", json={"delivered": "10d"}).json()
    _seed_tester_data(ta, "a@gmail.com", "aaa")
    _seed_tester_data(tb, "b@gmail.com", "bbb")
    db.save_thread("sim-1", "a@gmail.com", {}, "DONE")  # a sandbox thread with the same email
    db.insert_email("sim-1", "CUSTOMER_INBOX", "a@gmail.com", "sandbox", "sandbox-only")
    with db.get_conn() as conn:  # an internal mail with no (trusted) thread
        conn.execute("INSERT INTO internal_inbox (role, thread_id, subject, body, received_at) "
                     "VALUES ('AM', NULL, 'orphan', 'orphan', 'now')")

    a = client.get(f"/t/{ta}/state").json()
    b = client.get(f"/t/{tb}/state").json()
    dump_a, dump_b = json.dumps(a), json.dumps(b)

    assert [o["order_id"] for o in a["orders"]] == [oa["order_id"]]
    assert [t["thread_id"] for t in a["threads"]] == ["live-aaa"]
    assert len(a["am_inbox"]) == len(a["engineer_inbox"]) == len(a["sent_log"]) == len(a["traces"]) == 1
    for other in ("bbb", ob["order_id"], ob["part_number"], "b@gmail.com", "orphan", "sandbox-only"):
        assert other not in dump_a
    for other in ("aaa", oa["order_id"], oa["part_number"], "a@gmail.com", "orphan"):
        assert other not in dump_b
    assert a["tester"]["email"] == "a@gmail.com"


@pytest.mark.parametrize("method,path,body", [
    ("get", "/t/{t}/state", None),
    ("post", "/t/{t}/orders", {"delivered": "10d"}),
    ("get", "/t/{t}", None),
])
def test_bad_or_missing_token_is_404(client, method, path, body):
    register(client, "a@gmail.com")
    for bad in ("x", "A" * 43, "a" * 200):
        r = getattr(client, method)(path.format(t=bad), **({"json": body} if body else {}))
        assert r.status_code == 404
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE tester_id IS NOT NULL").fetchone()[0] == 0


def test_state_includes_usage_badge_numbers(client):
    token = token_of(register(client, "a@gmail.com"))
    u = client.get(f"/t/{token}/state").json()["usage"]
    assert u["daily_limit"] == 100 and u["monthly_limit"] == 3000 and u["scenarios_left"] == 16


# ---------------------------------------------------------------- coexistence


def _jev(spam=0.02, intent="non_sop", conf=0.95, score=0.1):
    return parse_response({"answers": {
        "is_spam_or_injection": {"type": "noul", "noul": spam},
        "intent": {"type": "choice", "choice": intent, "confidence": conf, "probabilities": {intent: conf}},
        "frustration_level": {"type": "score", "score": score, "probabilities": {}},
    }})


def test_simulate_path_never_calls_resend_even_in_resend_mode(client, resend, monkeypatch):
    async def fake_classify(masked, **kw):
        return _jev()

    monkeypatch.setattr(pipeline, "classify", fake_classify)
    register(client, "a@gmail.com")  # even for a registered tester's address
    r = client.post("/api/simulate-email", headers=H, json={
        "sender_email": "a@gmail.com", "thread_id": "th-1", "message_id": "m-1", "body": "Quote for 50 units?"})
    assert r.json()["status"] == "ESCALATED_TO_HUMAN"
    assert resend.requests == []
    assert {e["status"] for e in db.list_emails()} == {"SIMULATED"} and len(db.list_emails()) == 2
    assert db.thread_source("th-1") == "simulated"
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_usage").fetchone()[0] == 0


def test_reset_db_endpoint_keeps_live_data(client):
    token = token_of(register(client, "a@gmail.com"))
    order = client.post(f"/t/{token}/orders", json={"delivered": "10d"}).json()
    _seed_tester_data(token, "a@gmail.com", "aaa")
    assert client.post("/api/reset-db", headers=H).status_code == 200
    s = client.get(f"/t/{token}/state").json()
    assert [o["order_id"] for o in s["orders"]] == [order["order_id"]]
    assert len(s["threads"]) == len(s["am_inbox"]) == len(s["sent_log"]) == 1


def test_sandbox_state_hides_live_data_unless_toggled(client):
    token = token_of(register(client, "a@gmail.com"))
    order = client.post(f"/t/{token}/orders", json={"delivered": "10d"}).json()
    _seed_tester_data(token, "a@gmail.com", "aaa")
    plain = client.get("/api/state", headers=H).json()
    assert order["order_id"] not in json.dumps(plain["orders"]) and plain["threads"] == []
    assert "usage" in plain and plain["include_live"] is False
    full = client.get("/api/state?include_live=true", headers=H).json()
    assert order["order_id"] in json.dumps(full["orders"]) and full["threads"][0]["thread_id"] == "live-aaa"


def test_reset_live_needs_passcode_and_confirmation(client):
    token = token_of(register(client, "a@gmail.com"))
    _seed_tester_data(token, "a@gmail.com", "aaa")
    assert client.post("/api/admin/reset-live", json={"confirm": "DELETE ALL LIVE DATA"}).status_code == 401
    assert client.post("/api/admin/reset-live", headers=H, json={}).status_code == 400
    assert client.post("/api/admin/reset-live", headers=H, json={"confirm": "yes"}).status_code == 400
    assert client.get(f"/t/{token}/state").status_code == 200  # still there
    assert client.post("/api/admin/reset-live", headers=H, json={"confirm": "DELETE ALL LIVE DATA"}).status_code == 200
    assert client.get(f"/t/{token}/state").status_code == 404
    assert db.list_threads(include_live=True) == []


def test_pages_are_served(client):
    signup = client.get("/tester")
    assert signup.status_code == 200 and "테스터 등록" in signup.text
    token = token_of(register(client, "a@gmail.com"))
    page = client.get(f"/t/{token}")
    assert page.status_code == 200
    assert '<meta name="referrer" content="no-referrer"' in page.text  # the token never leaks as a Referer


# ------------------------------------------------------- staff reply / hand-back


def _thread_with_order(client, email="a@gmail.com", status="RETURN_LABEL_ISSUED"):
    token = token_of(register(client, email))
    order = client.post(f"/t/{token}/orders", json={"delivered": "10d"}).json()
    db.save_thread("live-1", email, {"<ORDER_TOKEN_1>": order["order_id"]}, status, source="live")
    threads.remember("<c1@gmail.com>", "live-1", threads.IN, "Return request", sender=email, body="Return it please")
    return token, order


def test_staff_reply_lands_in_the_customer_thread_and_pauses_the_agent(client, resend):
    token, _ = _thread_with_order(client)
    r = client.post(f"/t/{token}/threads/live-1/reply", json={"role": "AM", "body": "I'll call you today."})
    assert r.status_code == 200 and r.json()["author"] == "Sarah Kim (Account Manager)"
    [sent] = resend.sent()
    assert sent["to"] == ["a@gmail.com"] and sent["subject"] == "Re: Return request"
    assert sent["headers"]["In-Reply-To"] == "<c1@gmail.com>"
    assert sent["from"] == "Sarah Kim (Account Manager) via AxMatt Supply <support@test.example.com>"
    assert sent["text"].startswith("I'll call you today.") and "Sarah Kim\nAccount Manager" in sent["text"]

    thread = db.load_thread("live-1")
    assert thread["thread_status"] == "HUMAN_HANDLING" and thread["paused_from"] == "RETURN_LABEL_ISSUED"
    state = client.get(f"/t/{token}/state").json()
    assert state["threads"][0]["staff"] == {"AM": "Sarah Kim", "ENGINEER": "Alex Chen"}
    assert state["sent_log"][0]["author"] == "Sarah Kim (Account Manager)"


def test_hand_back_restores_the_previous_status(client, resend):
    token, _ = _thread_with_order(client, status="MEETING_CONFIRMED")
    assert client.post(f"/t/{token}/threads/live-1/resume").status_code == 409  # not paused yet
    client.post(f"/t/{token}/threads/live-1/reply", json={"role": "ENGINEER", "body": "On it."})
    client.post(f"/t/{token}/threads/live-1/reply", json={"role": "AM", "body": "Me too."})
    assert db.load_thread("live-1")["human_roles"] == "AM,ENGINEER"
    assert db.load_thread("live-1")["paused_from"] == "MEETING_CONFIRMED"  # not overwritten by the 2nd reply
    assert client.post(f"/t/{token}/threads/live-1/resume").json() == {"thread_status": "MEETING_CONFIRMED"}
    thread = db.load_thread("live-1")
    assert thread["paused_from"] is None and thread["human_roles"] is None


def test_staff_reply_only_on_your_own_live_thread(client, resend):
    _thread_with_order(client)
    other = token_of(register(client, "b@gmail.com"))
    db.save_thread("sim-1", "b@gmail.com", {}, "DONE")  # a sandbox thread
    for thread in ("live-1", "sim-1", "nope"):
        assert client.post(f"/t/{other}/threads/{thread}/reply", json={"role": "AM", "body": "x"}).status_code == 404
        assert client.post(f"/t/{other}/threads/{thread}/resume").status_code == 404
    assert resend.sent() == [] and db.load_thread("live-1")["thread_status"] == "RETURN_LABEL_ISSUED"


def test_staff_reply_not_sent_keeps_the_agent_on(client, resend, monkeypatch):
    token, _ = _thread_with_order(client)
    resend.send_status = 500
    r = client.post(f"/t/{token}/threads/live-1/reply", json={"role": "AM", "body": "x"})
    assert r.status_code == 503 and db.load_thread("live-1")["thread_status"] == "RETURN_LABEL_ISSUED"


async def test_customer_mail_on_a_paused_thread_goes_to_the_people_not_the_agent(client, resend, monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("Jev must not be called")

    monkeypatch.setattr(pipeline, "classify", boom)
    token, _ = _thread_with_order(client)
    client.post(f"/t/{token}/threads/live-1/reply", json={"role": "ENGINEER", "body": "Send me a photo."})
    before = len(resend.sent())
    result = await pipeline.process_email("a@gmail.com", "live-1", "<c2@gmail.com>", "Here is the photo.",
                                          source="live")
    assert result["status"] == "HUMAN_HANDLING" and db.load_thread("live-1")["thread_status"] == "HUMAN_HANDLING"
    new = resend.sent()[before:]
    assert sorted(m["to"][0].split("<")[1] for m in new) == ["am@test.example.com>", "eng@test.example.com>"]
    assert all(m["subject"].startswith("[HUMAN]") and "Here is the photo." in m["text"] for m in new)


# -------------------------------------------------------------------- attachments


def test_label_download_only_for_the_owner(client, resend):
    from core import mail

    token, _ = _thread_with_order(client)
    pdf = mail.Attachment("SAMPLE-return-label.pdf", "application/pdf", b"%PDF-1.4 x")
    mail.send("live-1", "CUSTOMER_INBOX", "a@gmail.com", "Label", "Here.", attachments=(pdf,))
    [att] = client.get(f"/t/{token}/state").json()["sent_log"][0]["attachments"]

    r = client.get(f"/t/{token}/attachments/{att['id']}")
    assert r.status_code == 200 and r.content == pdf.data and r.headers["content-type"] == "application/pdf"
    other = token_of(register(client, "b@gmail.com"))
    assert client.get(f"/t/{other}/attachments/{att['id']}").status_code == 404
    assert client.get(f"/t/{token}/attachments/999").status_code == 404
    # the sandbox admin can open it with the passcode
    assert client.get(f"/api/attachments/{att['id']}").status_code == 401
    assert client.get(f"/api/attachments/{att['id']}", headers=H).content == pdf.data
