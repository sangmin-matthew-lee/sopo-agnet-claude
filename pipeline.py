"""The 4-phase orchestration pipeline. Returns a step-by-step trace for the X-Ray UI."""

from __future__ import annotations

import os
import time
from typing import Any

import db
from agent import (
    AWAITING_CUSTOMER_INFO,
    CUSTOMER_INBOX,
    ESCALATED_TO_HUMAN,
    SOP_EMPLOYEE_INBOX,
    SOPSharedContext,
    run_return_agent,
)
from jev_router import AWAITING_INFO_CONTEXT, JevResult, RouteDecision, classify, route
from masker import mask_text

HOLDING_REPLY = (
    "Thank you for reaching out. Your message has been forwarded to a member of our "
    "Sales Operations team, who will follow up with you personally as soon as possible.\n\n"
    "Sales Operations Team"
)


class Trace:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self._t0 = time.perf_counter()

    def add(self, phase: str, title: str, **data: Any) -> None:
        self.steps.append({"phase": phase, "title": title,
                           "ms": round((time.perf_counter() - self._t0) * 1000), **data})


def _escalate(thread_id: str, sender: str, clean_text: str, reasons: list[str], trace: Trace) -> None:
    """Path B: unmasked alert to SOP inbox + holding reply to customer. No OpenAI call."""
    sop_body = (
        f"Escalated thread {thread_id} from {sender}.\n"
        f"Reasons: {'; '.join(reasons)}\n\n--- Customer message (unmasked) ---\n{clean_text}"
    )
    db.insert_email(thread_id, SOP_EMPLOYEE_INBOX, db.SOP_OWNER_EMAIL,
                    f"[ESCALATION] Customer needs a human: {sender}", sop_body)
    db.insert_email(thread_id, CUSTOMER_INBOX, sender, "We've received your message", HOLDING_REPLY)
    trace.add("phase2", "Escalated to human", inboxes=[SOP_EMPLOYEE_INBOX, CUSTOMER_INBOX])


async def process_email(sender_email: str, thread_id: str, message_id: str, body: str) -> dict[str, Any]:
    trace = Trace()
    sender_email = sender_email.strip().lower()

    # ---------------- Phase 1: idempotency, vault load, preprocessing, masking
    if not db.mark_message_processed(message_id, thread_id):
        trace.add("phase1", "Duplicate message_id — aborted", message_id=message_id)
        return {"status": "DUPLICATE_IGNORED", "thread_status": None, "trace": trace.steps}
    trace.add("phase1", "Idempotency check passed", message_id=message_id)

    existing = db.load_thread(thread_id)
    if existing and existing["customer_email"].lower() != sender_email:
        # Another sender is replying into someone else's thread: never hand them that vault.
        reasons = [f"sender {sender_email} does not own thread (owner: {existing['customer_email']})"]
        trace.add("phase1", "Thread hijack attempt", reasons=reasons)
        clean = mask_text(body).clean_text
        _escalate(thread_id, sender_email, clean, reasons, trace)
        return {"status": ESCALATED_TO_HUMAN, "thread_status": existing["thread_status"], "trace": trace.steps}

    prior_vault = existing["vault"] if existing else {}
    trace.add("phase1", "Thread vault loaded" if existing else "New thread vault initialized",
              thread_id=thread_id, prior_vault=prior_vault)

    m = mask_text(body, vault=prior_vault)
    trace.add("phase1", "Preprocess + PII masking", raw_text=m.raw_text, clean_text=m.clean_text,
              masked_text=m.masked_text, new_tokens=m.new_tokens, vault=m.vault)
    db.save_thread(thread_id, sender_email, m.vault, "PROCESSING")

    # ---------------- Phase 2: Jev gatekeeper + deterministic router
    awaiting = bool(existing) and existing["thread_status"] == AWAITING_CUSTOMER_INFO
    thread_context = AWAITING_INFO_CONTEXT if awaiting else None
    jev: JevResult = await classify(m.masked_text, thread_context=thread_context)
    trace.add("phase2", "Jev System 1 classification", sent_to_jev=m.masked_text,
              thread_context=thread_context, jev=jev.to_dict())
    decision: RouteDecision = route(jev, awaiting_customer_info=awaiting)
    trace.add("phase2", f"Route → {decision.path}", decision=decision.to_dict())

    if decision.path == "A_BLOCK":
        db.set_thread_status(thread_id, "BLOCKED_SPAM")
        trace.add("phase2", "Blocked. Zero OpenAI tokens spent.")
        return _done("BLOCKED_SPAM", thread_id, trace, decision, jev)

    if decision.path == "B_ESCALATE":
        _escalate(thread_id, sender_email, m.clean_text, decision.reasons, trace)
        db.set_thread_status(thread_id, ESCALATED_TO_HUMAN)
        return _done(ESCALATED_TO_HUMAN, thread_id, trace, decision, jev)

    # ---------------- Phase 3 & 4: Return agent + local tools
    if not os.environ.get("OPENAI_API_KEY"):
        reasons = ["OPENAI_API_KEY is not set; Return Agent unavailable"]
        trace.add("phase3", "Return Agent unavailable — failing closed", reasons=reasons)
        _escalate(thread_id, sender_email, m.clean_text, reasons, trace)
        db.set_thread_status(thread_id, ESCALATED_TO_HUMAN)
        return _done(ESCALATED_TO_HUMAN, thread_id, trace, decision, jev)

    ctx = SOPSharedContext(thread_id=thread_id, customer_email=sender_email, vault=dict(m.vault))
    try:
        out = await run_return_agent(ctx, m.masked_text)
        trace.add("phase3", "Return Agent finished", final_output=out["final_output"])
    except Exception as exc:  # noqa: BLE001 — any agent failure must fail closed
        trace.add("phase3", "Return Agent error — failing closed", error=f"{type(exc).__name__}: {exc}")
        if not ctx.locked:
            _escalate(thread_id, sender_email, m.clean_text, [f"agent error: {type(exc).__name__}"], trace)
        ctx.thread_status = ctx.thread_status or ESCALATED_TO_HUMAN
    finally:
        trace.add("phase4", "Agent tool calls & local de-masking", agent_steps=ctx.step_logs)

    status = ctx.thread_status
    if status is None:
        _escalate(thread_id, sender_email, m.clean_text, ["Return Agent took no action"], trace)
        status = ESCALATED_TO_HUMAN
    db.save_thread(thread_id, sender_email, ctx.vault, status)
    trace.add("phase4", "Thread vault saved", vault=ctx.vault, thread_status=status)
    return _done(status, thread_id, trace, decision, jev)


def _done(status: str, thread_id: str, trace: Trace, decision: RouteDecision, jev: JevResult) -> dict[str, Any]:
    return {"status": status, "thread_status": status, "thread_id": thread_id,
            "route": decision.path, "jev_source": jev.source, "trace": trace.steps}
