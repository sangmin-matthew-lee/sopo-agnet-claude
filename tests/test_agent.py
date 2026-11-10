import db
from agent import (
    SOPSharedContext,
    build_agent_input,
    build_return_agent,
    run_return_agent,
    send_return_emails_impl,
    verify_order_and_create_label_impl,
)
from masker import contains_raw_pii
from tests.fake_model import ScriptedModel

VAULT = {"<ORDER_TOKEN_1>": "ORD-9988", "<PART_TOKEN_1>": "AB-1234"}


def ctx_for(db_path, email="john@acmecorp.com", vault=None, thread_id="t1"):
    return SOPSharedContext(thread_id=thread_id, customer_email=email, vault=dict(vault or VAULT), db_path=db_path)


def inbox(db_path, kind):
    return [e for e in db.list_emails(db_path) if e["inbox_type"] == kind]


# ------------------------------------------------------- verify tool

def test_happy_path_creates_label_and_returns_masked_result(db_path):
    ctx = ctx_for(db_path)
    out = verify_order_and_create_label_impl(ctx, "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 1)
    assert out.startswith("APPROVED") and "<LABEL_TOKEN_1>" in out
    assert not contains_raw_pii(out)
    assert ctx.vault["<LABEL_TOKEN_1>"] == "UPS-RMA-ORD-9988"
    order = db.find_order("ORD-9988", "AB-1234", db_path)
    assert order["status"] == "RETURN_INITIATED" and order["return_label"] == "UPS-RMA-ORD-9988"
    assert ctx.thread_status == "RETURN_LABEL_ISSUED"


def test_label_idempotency_reuses_existing_label(db_path):
    verify_order_and_create_label_impl(ctx_for(db_path), "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 1)
    with db.get_conn(db_path) as conn:  # pretend the label was issued earlier with a different value
        conn.execute("UPDATE orders SET return_label='UPS-RMA-ORD-9988-ORIG' WHERE order_id='ORD-9988'")
    ctx2 = ctx_for(db_path, thread_id="t2")
    out = verify_order_and_create_label_impl(ctx2, "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 1)
    assert "reused" in out
    assert ctx2.vault["<LABEL_TOKEN_1>"] == "UPS-RMA-ORD-9988-ORIG"
    assert db.find_order("ORD-9988", "AB-1234", db_path)["return_label"] == "UPS-RMA-ORD-9988-ORIG"


def test_spoofed_sender_rejected_and_escalated(db_path):
    ctx = ctx_for(db_path, email="hacker@gmail.com")
    out = verify_order_and_create_label_impl(ctx, "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 1)
    assert out.startswith("REJECTED") and not contains_raw_pii(out)
    assert ctx.locked and ctx.thread_status == "ESCALATED_TO_HUMAN"
    assert db.find_order("ORD-9988", "AB-1234", db_path)["return_label"] is None
    sop = inbox(db_path, "SOP_EMPLOYEE_INBOX")
    assert len(sop) == 1 and "hacker@gmail.com" in sop[0]["body"] and "ORD-9988" in sop[0]["body"]
    cust = inbox(db_path, "CUSTOMER_INBOX")
    assert len(cust) == 1 and "ORD-9988" not in cust[0]["body"]
    # A locked thread can't be used to send more mail to the attacker.
    assert send_return_emails_impl(ctx, "Label <ORDER_TOKEN_1>", "x", "", False).startswith("SKIPPED")
    assert len(inbox(db_path, "CUSTOMER_INBOX")) == 1


def test_expired_order_ineligible(db_path):
    ctx = ctx_for(db_path, vault={"<ORDER_TOKEN_1>": "ORD-1111", "<PART_TOKEN_1>": "CD-5678"})
    out = verify_order_and_create_label_impl(ctx, "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 1)
    assert out.startswith("INELIGIBLE") and "30-day" in out
    assert ctx.thread_status == "RETURN_DENIED"
    assert db.find_order("ORD-1111", "CD-5678", db_path)["return_label"] is None


def test_quantity_over_purchased_ineligible(db_path):
    out = verify_order_and_create_label_impl(ctx_for(db_path), "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 99)
    assert out.startswith("INELIGIBLE") and "quantity" in out


def test_not_delivered_ineligible(db_path):
    ctx = ctx_for(db_path, email="mary@betaindustries.com",
                  vault={"<ORDER_TOKEN_1>": "ORD-7777", "<PART_TOKEN_1>": "EF-9012"})
    out = verify_order_and_create_label_impl(ctx, "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 1)
    assert out.startswith("INELIGIBLE") and "SHIPPED" in out


def test_unknown_token_and_unknown_order(db_path):
    out = verify_order_and_create_label_impl(ctx_for(db_path), "<ORDER_TOKEN_7>", "<PART_TOKEN_1>", 1)
    assert out.startswith("MISSING_INFO")
    out = verify_order_and_create_label_impl(ctx_for(db_path), "ORD-9988", "<PART_TOKEN_1>", 1)
    assert out.startswith("MISSING_INFO")  # raw values are not accepted as tokens
    ctx = ctx_for(db_path, vault={"<ORDER_TOKEN_1>": "ORD-0000", "<PART_TOKEN_1>": "AB-1234"})
    assert verify_order_and_create_label_impl(ctx, "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 1).startswith("NOT_FOUND")


# ------------------------------------------------------- send tool

def test_send_emails_unmasks_tokens(db_path):
    ctx = ctx_for(db_path)
    verify_order_and_create_label_impl(ctx, "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 1)
    out = send_return_emails_impl(ctx, "Return for <ORDER_TOKEN_1>",
                                  "Use label <LABEL_TOKEN_1> for <PART_TOKEN_1>.", "Label <LABEL_TOKEN_1> issued.", False)
    assert out.startswith("SENT: 2")
    cust = inbox(db_path, "CUSTOMER_INBOX")[0]
    assert cust["subject"] == "Return for ORD-9988"
    assert cust["body"] == "Use label UPS-RMA-ORD-9988 for AB-1234."
    assert cust["recipient_email"] == "john@acmecorp.com"
    sop = inbox(db_path, "SOP_EMPLOYEE_INBOX")[0]
    assert sop["body"] == "Label UPS-RMA-ORD-9988 issued." and sop["recipient_email"] == db.AM_JOHN
    assert ctx.thread_status == "RETURN_LABEL_ISSUED"


def test_send_missing_info_sets_awaiting(db_path):
    ctx = ctx_for(db_path, vault={"<PART_TOKEN_1>": "AB-1234"})
    out = send_return_emails_impl(ctx, "Need your order number", "Which order was <PART_TOKEN_1> on?", "", True)
    assert out.startswith("SENT: 1")
    assert ctx.thread_status == "AWAITING_CUSTOMER_INFO"
    assert inbox(db_path, "CUSTOMER_INBOX")[0]["body"] == "Which order was AB-1234 on?"


# ------------------------------------------------------- SDK wiring

def test_agent_definition():
    agent = build_return_agent()
    assert agent.model == "gpt-4o-mini"
    names = {t.name for t in agent.tools}
    assert names == {"verify_order_and_create_label", "send_return_emails"}
    for t in agent.tools:  # ctx must be hidden from the schema sent to OpenAI
        assert "ctx" not in t.params_json_schema["properties"]


def test_agent_input_lists_token_names_not_values():
    text = build_agent_input("return <PART_TOKEN_1>", VAULT)
    assert "<ORDER_TOKEN_1>" in text and "ORD-9988" not in text and "AB-1234" not in text


async def test_end_to_end_runner_with_session_never_leaks_pii(db_path):
    model = ScriptedModel([
        ("tool", "verify_order_and_create_label",
         {"order_token": "<ORDER_TOKEN_1>", "part_token": "<PART_TOKEN_1>", "quantity": 1}),
        ("tool", "send_return_emails", {
            "customer_subject": "Your return for <ORDER_TOKEN_1>",
            "customer_body": "Attach label <LABEL_TOKEN_1> to <PART_TOKEN_1>.",
            "sop_summary": "Label <LABEL_TOKEN_1> issued.", "request_missing_info": False}),
        ("text", "Issued label and emailed customer."),
    ])
    ctx = ctx_for(db_path)
    out = await run_return_agent(ctx, "return <PART_TOKEN_1> from <ORDER_TOKEN_1>", model=model)
    assert out["final_output"] == "Issued label and emailed customer."
    assert ctx.thread_status == "RETURN_LABEL_ISSUED"
    assert inbox(db_path, "CUSTOMER_INBOX")[0]["body"] == "Attach label UPS-RMA-ORD-9988 to AB-1234."
    # Nothing the model saw contained raw order/part/label values.
    for seen in model.seen_inputs:
        assert not contains_raw_pii(seen), seen
    # SQLiteSession persisted history for the thread in the same DB.
    with db.get_conn(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM agent_messages WHERE session_id='t1'").fetchone()[0]
    assert n >= 4


async def test_multi_turn_session_history_carries_over(db_path):
    m1 = ScriptedModel([
        ("tool", "send_return_emails", {"customer_subject": "Order number needed",
         "customer_body": "Which order was <PART_TOKEN_1> on?", "sop_summary": "", "request_missing_info": True}),
        ("text", "Asked for order."),
    ])
    ctx1 = ctx_for(db_path, vault={"<PART_TOKEN_1>": "AB-1234"})
    await run_return_agent(ctx1, "return <PART_TOKEN_1>", model=m1)
    assert ctx1.thread_status == "AWAITING_CUSTOMER_INFO"

    m2 = ScriptedModel([("text", "ok")])
    ctx2 = ctx_for(db_path)
    await run_return_agent(ctx2, "it was <ORDER_TOKEN_1>", model=m2)
    # Second turn's model input includes turn-1 history from SQLiteSession.
    assert "Asked for order." in m2.seen_inputs[0]
    assert not contains_raw_pii(m2.seen_inputs[0])


def test_label_appended_when_llm_omits_it(db_path):
    ctx = ctx_for(db_path)
    verify_order_and_create_label_impl(ctx, "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 1)
    send_return_emails_impl(ctx, "Your return", "Your label is ready for <PART_TOKEN_1>.", "", False)
    body = inbox(db_path, "CUSTOMER_INBOX")[0]["body"]
    assert body.endswith("Your return label number: UPS-RMA-ORD-9988")
    assert any(s["step"] == "label_appended" for s in ctx.step_logs)


def test_label_not_duplicated_when_present(db_path):
    ctx = ctx_for(db_path)
    verify_order_and_create_label_impl(ctx, "<ORDER_TOKEN_1>", "<PART_TOKEN_1>", 1)
    send_return_emails_impl(ctx, "Your return", "Label: <LABEL_TOKEN_1>.", "", False)
    assert inbox(db_path, "CUSTOMER_INBOX")[0]["body"] == "Label: UPS-RMA-ORD-9988."


def test_instructions_forbid_attachment_wording():
    from agent import RETURN_AGENT_INSTRUCTIONS
    assert "never say the label" in RETURN_AGENT_INSTRUCTIONS and "MUST contain the" in RETURN_AGENT_INSTRUCTIONS


def test_parallel_tool_calls_disabled():
    assert build_return_agent().model_settings.parallel_tool_calls is False


def test_send_blocked_before_verify_when_both_tokens_known(db_path):
    ctx = ctx_for(db_path, email="hacker@gmail.com")
    out = send_return_emails_impl(ctx, "Return", "About <ORDER_TOKEN_1> / <PART_TOKEN_1>", "", True)
    assert out.startswith("BLOCKED")
    assert db.list_emails(db_path) == []


def test_send_allowed_without_verify_when_a_token_is_missing(db_path):
    ctx = ctx_for(db_path, vault={"<PART_TOKEN_1>": "AB-1234"})
    assert send_return_emails_impl(ctx, "Need order", "Which order for <PART_TOKEN_1>?", "", True).startswith("SENT")


async def test_spoof_send_first_then_verify_never_reaches_attacker(db_path):
    """Replays the live failure: the model emails first, then verifies."""
    model = ScriptedModel([
        ("tool", "send_return_emails", {"customer_subject": "Return Label Request",
         "customer_body": "Checking <ORDER_TOKEN_1> and <PART_TOKEN_1>.", "sop_summary": "",
         "request_missing_info": True}),
        ("tool", "verify_order_and_create_label",
         {"order_token": "<ORDER_TOKEN_1>", "part_token": "<PART_TOKEN_1>", "quantity": 1}),
        ("text", "escalated"),
    ])
    ctx = ctx_for(db_path, email="hacker@gmail.com", thread_id="t-spoof")
    await run_return_agent(ctx, "return <PART_TOKEN_1> from <ORDER_TOKEN_1>", model=model)
    attacker_mail = [e for e in db.list_emails(db_path) if e["recipient_email"] == "hacker@gmail.com"]
    assert [e["subject"] for e in attacker_mail] == ["Your return request is under review"]
    assert "ORD-9988" not in attacker_mail[0]["body"]


# ------------------------------------------------------- OpenAI privacy guard

import pytest  # noqa: E402

from agent import guard_agent_input, guard_tool_result, run_agent  # noqa: E402
from jev_router import PIILeakError  # noqa: E402


async def test_raw_data_in_agent_input_never_reaches_openai(db_path):
    model = ScriptedModel([("text", "should not run")])
    ctx = ctx_for(db_path)
    with pytest.raises(PIILeakError):
        await run_agent(build_return_agent(model), ctx, "Return AB-1234 from ORD-9988")
    assert model.calls == 0
    assert ctx.step_logs[-1]["step"] == "guard" and ctx.step_logs[-1]["where"] == "agent_input"


@pytest.mark.parametrize("leak", [
    "label UPS-RMA-ORD-9988", "part AB-1234", "https://meet.axmattsupply.com/TS-12ab34cd",
    "ask alex.chen@axmattsupply.com", "Sarah Kim will join", "sent to john@acmecorp.com",
    "write to sales@axmattsupply.com",
])
def test_tool_result_guard_blocks_local_data(db_path, leak):
    with pytest.raises(PIILeakError):
        guard_tool_result(ctx_for(db_path), "some_tool", f"APPROVED: {leak}")


def test_guards_pass_masked_text(db_path):
    ctx = ctx_for(db_path)
    text = "APPROVED: label <LABEL_TOKEN_1> for <PART_TOKEN_1>; the engineer and the account manager were invited."
    assert guard_tool_result(ctx, "t", text) == text
    assert guard_agent_input(ctx, "Return <PART_TOKEN_1> from <ORDER_TOKEN_1>") .startswith("Return")


async def test_tool_result_leak_aborts_the_run(db_path, monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "verify_order_and_create_label_impl",
                        lambda ctx, o, p, q: "APPROVED: label UPS-RMA-ORD-9988")  # a buggy tool
    model = ScriptedModel([
        ("tool", "verify_order_and_create_label",
         {"order_token": "<ORDER_TOKEN_1>", "part_token": "<PART_TOKEN_1>", "quantity": 1}),
        ("text", "never reached"),
    ])
    with pytest.raises(Exception) as err:  # the SDK wraps tool exceptions in UserError
        await run_return_agent(ctx_for(db_path), "Return <PART_TOKEN_1> from <ORDER_TOKEN_1>", model=model)
    assert isinstance(err.value.__cause__, PIILeakError)
    assert model.calls == 1  # the leaking result was never sent back to the model
