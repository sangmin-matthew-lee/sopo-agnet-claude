"""The 4-phase orchestration pipeline. Returns a step-by-step trace for the X-Ray UI."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, AsyncIterator, Callable

Emit = Callable[[dict[str, Any]], None]

import db
import scheduling as sch
from agent import (
    AWAITING_CUSTOMER_INFO,
    CUSTOMER_INBOX,
    ESCALATED_TO_HUMAN,
    SOP_EMPLOYEE_INBOX,
    SOPSharedContext,
    run_return_agent,
)
from jev_router import AWAITING_INFO_CONTEXT, TECH_SCHEDULING_CONTEXT, JevResult, RouteDecision, classify, route
from masker import mask_text
from tech_agent import AWAITING_CUSTOMER_SCHEDULE, MEETING_CONFIRMED, TechSupportContext, run_tech_agent

HOLDING_REPLY = (
    "Thank you for reaching out. Your message has been forwarded to a member of our "
    "Sales Operations team, who will follow up with you personally as soon as possible.\n\n"
    "Sales Operations Team"
)


class Trace:
    def __init__(self, emit: Emit | None = None) -> None:
        self.steps: list[dict[str, Any]] = []
        self.emit = emit
        self._t0 = time.perf_counter()

    def _ms(self) -> int:
        return round((time.perf_counter() - self._t0) * 1000)

    def add(self, phase: str, title: str, **data: Any) -> None:
        step = {"phase": phase, "title": title, "ms": self._ms(), **data}
        self.steps.append(step)
        if self.emit:
            self.emit({"type": "step", **step})

    def start(self, phase: str, title: str) -> None:
        """Announce a slow operation (external API call) so the UI can show a spinner."""
        if self.emit:
            self.emit({"type": "phase_start", "phase": phase, "title": title, "ms": self._ms()})


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


async def process_email(
    sender_email: str, thread_id: str, message_id: str, body: str, emit: Emit | None = None
) -> dict[str, Any]:
    trace = Trace(emit)
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
    prior_status = existing["thread_status"] if existing else None
    awaiting = prior_status == AWAITING_CUSTOMER_INFO
    # A booked meeting keeps the thread open too: "that time doesn't work" is a reschedule, not spam.
    awaiting_tech = prior_status in (AWAITING_CUSTOMER_SCHEDULE, MEETING_CONFIRMED)
    if not awaiting and not awaiting_tech and db.has_upcoming_meeting(sender_email, sch.to_utc_iso(sch.now_local())):
        # A NEW email from someone with a booked meeting ("I can't make our call") is about that meeting.
        awaiting_tech = True
        trace.add("phase2", "Sender has an upcoming tech meeting → scheduling context", local_only=True)
    thread_context = AWAITING_INFO_CONTEXT if awaiting else TECH_SCHEDULING_CONTEXT if awaiting_tech else None
    trace.start("phase2", "Calling Jev System 1…")
    jev: JevResult = await classify(m.masked_text, thread_context=thread_context)
    trace.add("phase2", "Jev System 1 classification", sent_to_jev=m.masked_text,
              thread_context=thread_context, jev=jev.to_dict())
    decision: RouteDecision = route(jev, awaiting_customer_info=awaiting, awaiting_tech_schedule=awaiting_tech)
    trace.add("phase2", f"Route → {decision.path}", decision=decision.to_dict())

    if decision.path == "A_BLOCK":
        db.set_thread_status(thread_id, "BLOCKED_SPAM")
        trace.add("phase2", "Blocked. Zero OpenAI tokens spent.")
        return _done("BLOCKED_SPAM", thread_id, trace, decision, jev)

    if decision.path == "B_ESCALATE":
        _escalate(thread_id, sender_email, m.clean_text, decision.reasons, trace)
        db.set_thread_status(thread_id, ESCALATED_TO_HUMAN)
        return _done(ESCALATED_TO_HUMAN, thread_id, trace, decision, jev)

    # ---------------- Phase 3 & 4: specialist agent + local tools
    tech = decision.path == "C_TECH_AGENT"
    name = "Tech Support Agent" if tech else "Return Agent"
    if not os.environ.get("OPENAI_API_KEY"):
        reasons = [f"OPENAI_API_KEY is not set; {name} unavailable"]
        trace.add("phase3", f"{name} unavailable — failing closed", reasons=reasons)
        _escalate(thread_id, sender_email, m.clean_text, reasons, trace)
        db.set_thread_status(thread_id, ESCALATED_TO_HUMAN)
        return _done(ESCALATED_TO_HUMAN, thread_id, trace, decision, jev)

    common = dict(thread_id=thread_id, customer_email=sender_email, vault=dict(m.vault), emit=emit)
    ctx = TechSupportContext(**common, customer_message=m.clean_text) if tech else SOPSharedContext(**common)
    trace.start("phase3", f"{name} (gpt-4o-mini) thinking…")
    try:
        out = await (run_tech_agent if tech else run_return_agent)(ctx, m.masked_text)
        trace.add("phase3", f"{name} finished", final_output=out["final_output"], agent=name)
    except Exception as exc:  # noqa: BLE001 — any agent failure must fail closed
        trace.add("phase3", f"{name} error — failing closed", error=f"{type(exc).__name__}: {exc}", agent=name)
        if not ctx.locked and ctx.thread_status != ESCALATED_TO_HUMAN:
            _escalate(thread_id, sender_email, m.clean_text, [f"agent error: {type(exc).__name__}"], trace)
        ctx.thread_status = ctx.thread_status or ESCALATED_TO_HUMAN
    finally:
        trace.add("phase4", "Agent tool calls & local de-masking", agent_steps=ctx.step_logs)

    status = ctx.thread_status
    if status is None:
        _escalate(thread_id, sender_email, m.clean_text, [f"{name} took no action"], trace)
        status = ESCALATED_TO_HUMAN
    db.save_thread(thread_id, sender_email, ctx.vault, status)
    trace.add("phase4", "Thread vault saved", vault=ctx.vault, thread_status=status)
    return _done(status, thread_id, trace, decision, jev)


def _done(status: str, thread_id: str, trace: Trace, decision: RouteDecision, jev: JevResult) -> dict[str, Any]:
    return {"status": status, "thread_status": status, "thread_id": thread_id,
            "route": decision.path, "jev_source": jev.source, "trace": trace.steps}


# Strong refs so a pipeline keeps running (and finishes its DB writes) if the client disconnects.
_BACKGROUND: set[asyncio.Task] = set()


async def process_email_stream(
    sender_email: str, thread_id: str, message_id: str, body: str
) -> AsyncIterator[dict[str, Any]]:
    """Yield pipeline events live: phase_start, step, agent_log, agent_delta, then done/error."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def run() -> None:
        try:
            result = await process_email(sender_email, thread_id, message_id, body, emit=queue.put_nowait)
            queue.put_nowait({"type": "done", "result": result})
        except Exception as exc:  # noqa: BLE001
            queue.put_nowait({"type": "error", "error": f"{type(exc).__name__}: {exc}"})

    task = asyncio.create_task(run())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)

    while True:
        event = await queue.get()
        yield event
        if event["type"] in ("done", "error"):
            return
