"""The 4-phase orchestration pipeline. Return and Tech Support"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

Emit = Callable[[dict[str, Any]], None]

from core import db
from core import scheduling as sch
from core.jev_router import (
    AWAITING_INFO_CONTEXT,
    NON_SOP,
    TECH_SCHEDULING_CONTEXT,
    JevResult,
    RouteDecision,
    classify,
    route,
)
from core.masker import mask_text
from sop_agents.return_agent import (
    AWAITING_CUSTOMER_INFO,
    CUSTOMER_INBOX,
    ESCALATED_TO_HUMAN,
    SOP_EMPLOYEE_INBOX,
    SOPSharedContext,
    run_return_agent,
)
from sop_agents.tech_agent import (
    AWAITING_CUSTOMER_SCHEDULE,
    MEETING_CONFIRMED,
    TechSupportContext,
    run_tech_agent,
)

HOLDING_REPLY = (
    "Thank you for reaching out. Your message has been forwarded to your account manager and you manager will follow up with you as soon as possible.\n\n"
    "SOPO"
)
# For senders with no orders (e.g. a prospect): they have no account manager yet.
NEW_CONTACT_REPLY = (
    "Thank you for reaching out to AxMatt Supply. Your message has been forwarded to our team, "
    "and someone will follow up with you as soon as possible.\n\n"
    "SOPO"
)

# Machines: never auto-reply to these (prevents reply loops with other auto-responders).
_AUTOMATED_LOCALPARTS = re.compile(
    r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounces?)([+._-].*)?$", re.IGNORECASE
)
_AUTOMATED_BODY = re.compile(
    r"\b(out of (the )?office|auto(matic)?[- ]?reply|delivery (status notification|has failed|failure)"
    r"|undeliverable|mail delivery failed)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Sender:
    """Local facts about who sent the email. Decided in Python, never by an AI model."""

    email: str
    internal: bool  # a company address: goes to a human, gets no extra privileges
    automated: bool  # auto-reply / bounce / no-reply: no holding reply, low priority
    has_orders: bool  # an existing customer (has an account manager)

    @classmethod
    def of(cls, email: str, clean_text: str) -> "Sender":
        local, _, domain = email.partition("@")
        return cls(
            email=email,
            internal=domain == db.COMPANY_DOMAIN,
            automated=bool(_AUTOMATED_LOCALPARTS.match(local) or _AUTOMATED_BODY.search(clean_text)),
            has_orders=db.customer_has_orders(email),
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
        """To show external API call step-by-step in UI (X-ray)"""

        if self.emit:
            self.emit(
                {
                    "type": "phase_start",
                    "phase": phase,
                    "title": title,
                    "ms": self._ms(),
                }
            )


def _escalate(
    thread_id: str,
    sender: Sender,
    clean_text: str,
    reasons: list[str],
    trace: Trace,
    other_inquiry: bool = False,
) -> None:
    """Unmasked alert to SOP AM inbox (+ holding reply to a human sender). No OpenAI call."""
    if sender.internal:
        subject = f"[INTERNAL] Message from {sender.email}"
    elif sender.automated:
        subject = f"[LOW PRIORITY] Automated message from {sender.email}"
    elif other_inquiry:
        subject = f"[ESCALATION] Other inquiry (not return/tech): {sender.email}"
    else:
        subject = f"[ESCALATION] Customer needs a human: {sender.email}"
    if not sender.has_orders and not sender.internal:
        subject += " (no previous orders)"
    sop_body = (
        f"Escalated thread {thread_id} from {sender.email}.\n"
        f"Reasons: {'; '.join(reasons)}\n\n--- Customer message (unmasked) ---\n{clean_text}"
    )
    db.insert_email(thread_id, SOP_EMPLOYEE_INBOX, db.SOP_OWNER_EMAIL, subject, sop_body)
    inboxes = [SOP_EMPLOYEE_INBOX]
    if not (sender.internal or sender.automated):  # colleagues and machines get no holding reply
        reply = HOLDING_REPLY if sender.has_orders else NEW_CONTACT_REPLY
        db.insert_email(thread_id, CUSTOMER_INBOX, sender.email, "We've received your message", reply)
        inboxes.append(CUSTOMER_INBOX)
    trace.add("phase2", "Escalated to human", inboxes=inboxes, subject=subject)


async def process_email(
    sender_email: str,
    thread_id: str,
    message_id: str,
    body: str,
    emit: Emit | None = None,
) -> dict[str, Any]:
    trace = Trace(emit)
    sender_email = sender_email.strip().lower()

    # Phase 1: idempotency, vault load, preprocessing, masking
    if not db.mark_message_processed(message_id, thread_id):
        trace.add("phase1", "Duplicate message_id — aborted", message_id=message_id)
        return {
            "status": "DUPLICATE_IGNORED",
            "thread_status": None,
            "trace": trace.steps,
        }
    trace.add("phase1", "Idempotency check passed", message_id=message_id)

    existing = db.load_thread(thread_id)
    if existing and existing["customer_email"].lower() != sender_email:
        """Hijacking attempt"""
        reasons = [
            f"sender {sender_email} does not own thread (owner: {existing['customer_email']})"
        ]
        trace.add("phase1", "Thread hijack attempt", reasons=reasons)
        clean = mask_text(body).clean_text
        _escalate(thread_id, Sender.of(sender_email, clean), clean, reasons, trace)
        return {
            "status": ESCALATED_TO_HUMAN,
            "thread_status": existing["thread_status"],
            "trace": trace.steps,
        }

    prior_vault = existing["vault"] if existing else {}
    trace.add(
        "phase1",
        "Thread vault loaded" if existing else "New thread vault initialized",
        thread_id=thread_id,
        prior_vault=prior_vault,
    )

    m = mask_text(body, vault=prior_vault)
    trace.add(
        "phase1",
        "Preprocess + PII masking",
        raw_text=m.raw_text,
        clean_text=m.clean_text,
        masked_text=m.masked_text,
        new_tokens=m.new_tokens,
        vault=m.vault,
    )
    db.save_thread(thread_id, sender_email, m.vault, "PROCESSING")
    sender = Sender.of(sender_email, m.clean_text)
    if sender.internal or sender.automated or not sender.has_orders:
        trace.add(
            "phase1",
            "Sender checked locally",
            internal=sender.internal,
            automated=sender.automated,
            has_orders=sender.has_orders,
            local_only=True,
        )

    # Phase 2: Triage - Jev gatekeeper + deterministic router
    prior_status = existing["thread_status"] if existing else None
    awaiting = prior_status == AWAITING_CUSTOMER_INFO

    # A booked meeting keeps the thread open too: "that time doesn't work" is a reschedule, not spam.
    awaiting_tech = prior_status in (AWAITING_CUSTOMER_SCHEDULE, MEETING_CONFIRMED)
    if (
        not awaiting
        and not awaiting_tech
        and db.has_upcoming_meeting(sender_email, sch.to_utc_iso(sch.now_local()))
    ):
        # A NEW email from someone with a booked meeting ("I can't make our call") is about that meeting.
        awaiting_tech = True
        trace.add(
            "phase2",
            "Sender has an upcoming tech support meeting → scheduling context",
            local_only=True,
        )
    thread_context = (
        AWAITING_INFO_CONTEXT
        if awaiting
        else TECH_SCHEDULING_CONTEXT
        if awaiting_tech
        else None
    )
    trace.start("phase2", "Calling Jev…")
    jev: JevResult = await classify(m.masked_text, thread_context=thread_context)
    trace.add(
        "phase2",
        "Jev classification",
        sent_to_jev=m.masked_text,
        thread_context=thread_context,
        jev=jev.to_dict(),
    )
    decision: RouteDecision = route(
        jev,
        awaiting_customer_info=awaiting,
        awaiting_tech_schedule=awaiting_tech,
        internal_sender=sender.internal,
        automated=sender.automated,
    )
    trace.add("phase2", f"Route → {decision.path}", decision=decision.to_dict())

    if decision.path == "A_BLOCK":
        db.set_thread_status(thread_id, "BLOCKED_SPAM")
        trace.add("phase2", "Blocked. Zero OpenAI tokens spent.")
        return _done("BLOCKED_SPAM", thread_id, trace, decision, jev)

    if decision.path == "B_ESCALATE":
        other = jev.source == "jev" and jev.intent == NON_SOP
        _escalate(thread_id, sender, m.clean_text, decision.reasons, trace, other_inquiry=other)
        # An auto-reply (e.g. out-of-office) on an open thread must not end that conversation.
        keep = sender.automated and prior_status is not None and prior_status != "PROCESSING"
        db.set_thread_status(thread_id, prior_status if keep and prior_status else ESCALATED_TO_HUMAN)
        return _done(ESCALATED_TO_HUMAN, thread_id, trace, decision, jev)

    # Phase 3 & 4: specialist agent + local tools
    tech = (
        decision.path == "C_TECH_AGENT"
    )  # Need to update once refund agent is added later.
    agent_name = "Tech Support Agent" if tech else "Return Agent"
    if not os.environ.get("OPENAI_API_KEY"):
        reasons = [f"OPENAI_API_KEY is not set; {agent_name} unavailable"]
        trace.add(
            "phase3", f"{agent_name} unavailable — failing closed", reasons=reasons
        )
        _escalate(thread_id, sender, m.clean_text, reasons, trace)
        db.set_thread_status(thread_id, ESCALATED_TO_HUMAN)
        return _done(ESCALATED_TO_HUMAN, thread_id, trace, decision, jev)

    ctx: SOPSharedContext
    if tech:
        tech_ctx = TechSupportContext(
            thread_id=thread_id,
            customer_email=sender_email,
            vault=dict(m.vault),
            emit=emit,
            customer_message=m.clean_text,
        )
        ctx, run_agent = tech_ctx, lambda: run_tech_agent(tech_ctx, m.masked_text)
    else:
        return_ctx = SOPSharedContext(
            thread_id=thread_id,
            customer_email=sender_email,
            vault=dict(m.vault),
            emit=emit,
        )
        ctx, run_agent = return_ctx, lambda: run_return_agent(return_ctx, m.masked_text)
    trace.start("phase3", f"{agent_name} (gpt-4o-mini) thinking…")

    try:
        out = await run_agent()
        trace.add(
            "phase3",
            f"{agent_name} finished",
            final_output=out["final_output"],
            agent=agent_name,
        )
    except Exception as exc:
        trace.add(
            "phase3",
            f"{agent_name} error — failing closed",
            error=f"{type(exc).__name__}: {exc}",
            agent=agent_name,
        )
        if not ctx.locked and ctx.thread_status != ESCALATED_TO_HUMAN:
            _escalate(
                thread_id,
                sender,
                m.clean_text,
                [f"agent error: {type(exc).__name__}"],
                trace,
            )
        ctx.thread_status = ctx.thread_status or ESCALATED_TO_HUMAN
    finally:
        trace.add(
            "phase4", "Agent tool calls & local de-masking", agent_steps=ctx.step_logs
        )

    status = ctx.thread_status
    if status is None:
        _escalate(
            thread_id,
            sender,
            m.clean_text,
            [f"{agent_name} took no action"],
            trace,
        )
        status = ESCALATED_TO_HUMAN

    db.save_thread(thread_id, sender_email, ctx.vault, status)
    trace.add("phase4", "Thread vault saved", vault=ctx.vault, thread_status=status)
    return _done(status, thread_id, trace, decision, jev)


def _done(
    status: str, thread_id: str, trace: Trace, decision: RouteDecision, jev: JevResult
) -> dict[str, Any]:
    return {
        "status": status,
        "thread_status": status,
        "thread_id": thread_id,
        "route": decision.path,
        "jev_source": jev.source,
        "trace": trace.steps,
    }


# Strong refs so a pipeline keeps running (and finishes its DB writes) if the client disconnects.
_BACKGROUND: set[asyncio.Task[None]] = set()


async def process_email_stream(
    sender_email: str, thread_id: str, message_id: str, body: str
) -> AsyncIterator[dict[str, Any]]:
    """Make sure pipeline events live: phase_start -> step -> agent_log -> agent_delta -> done/error."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def run() -> None:
        try:
            result = await process_email(
                sender_email, thread_id, message_id, body, emit=queue.put_nowait
            )
            queue.put_nowait({"type": "done", "result": result})
        except Exception as exc:
            queue.put_nowait({"type": "error", "error": f"{type(exc).__name__}: {exc}"})

    task = asyncio.create_task(run())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)

    while True:
        event = await queue.get()
        yield event
        if event["type"] in ("done", "error"):
            return
