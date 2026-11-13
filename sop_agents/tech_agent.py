"""Tech Support Agent that books an engineer meeting.

Like real scheduling tools, calendars are the source of truth: the agent reads free/busy for an engineer who knows the part and for the client's account manager (AM), offers or books an open slot, and the engineer and the AM each get exactly ONE invite. Nobody is asked about times.

The LLM only sees tokens (<PART_TOKEN_1>, <LINK_TOKEN_1>) and dates/times. Engineer and AM identities, part numbers, and meeting links stay in local Python.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agents import Agent, ModelSettings, RunContextWrapper, function_tool
from agents.models.interface import Model

from core import db
from core import scheduling as sch
from sop_agents.return_agent import (
    CUSTOMER_INBOX,
    ESCALATED_TO_HUMAN,
    RETURN_AGENT_MODEL,
    SOP_EMPLOYEE_INBOX,
    SOPSharedContext,
    _dispatch,
    _resolve,
    _send,
    guard_tool_result,
    run_agent,
)
from core.masker import add_to_vault, tokens_of

TECH_AGENT_MODEL = RETURN_AGENT_MODEL
ENGINEER_INBOX = "ENGINEER_INBOX"

AWAITING_CUSTOMER_SCHEDULE = "AWAITING_CUSTOMER_SCHEDULE"
MEETING_CONFIRMED = "MEETING_CONFIRMED"
MAX_SCHEDULING_ROUNDS = (
    3  # emails asking the customer for info/times before a human takes over
)

HOLDING_REPLY = (
    "Thank you for reaching out. Your request has been forwarded to your account manager, "
    "who will follow up with you personally to arrange the technical session.\n\n"
    "Sales Operations Team"
)
# For a sender with no orders (e.g. a prospect asking about a product): no account manager yet.
NEW_CONTACT_REPLY = (
    "Thank you for reaching out to AxMatt Supply. Your request has been forwarded to our team, "
    "who will follow up with you as soon as possible.\n\n"
    "Sales Operations Team"
)


@dataclass
class TechSupportContext(SOPSharedContext):
    """Local-only state for the Tech Support Agent. Never serialized to the LLM."""

    customer_message: str = (
        ""  # cleaned (unmasked) text of this email, for the engineer's invite
    )
    part_number: str | None = None
    am_email: str | None = None
    availability_checked: bool = False
    bookable_slots: set[str] = field(
        default_factory=set
    )  # slots the customer asked for that checked AVAILABLE
    rescheduling: bool = False  # this run is moving an already-booked meeting
    meeting_link_token: str | None = None
    now: datetime | None = None  # injectable clock for tests

    def clock(self) -> datetime:
        return sch.to_local(self.now) if self.now else sch.now_local()


# -----------helpers-----------


def _result(ctx: TechSupportContext, tool: str, text: str) -> str:
    ctx.log("tool_result", tool=tool, result=text)
    return text


def _slot_line(i: int, slot: datetime) -> str:
    return f"{i}) {sch.iso_local(slot)} ({sch.human(slot)})"


def _schedule(ctx: TechSupportContext, **fields: Any) -> dict[str, Any]:
    return db.save_tech_schedule(
        ctx.thread_id, ctx.customer_email, ctx.db_path, **fields
    )


def _escalate(
    ctx: TechSupportContext,
    recipient: str,
    subject: str,
    body: str,
    lock: bool = False,
    reply: str = HOLDING_REPLY,
) -> None:
    """Hand the thread to a human: internal alert + holding reply. No engineer is contacted."""
    ctx.locked = ctx.locked or lock
    ctx.thread_status = ESCALATED_TO_HUMAN
    _send(
        ctx,
        CUSTOMER_INBOX,
        ctx.customer_email,
        "We've received your technical support request",
        reply,
    )
    _send(ctx, SOP_EMPLOYEE_INBOX, recipient, subject, body)  # after the reply, so a live copy quotes it
    _schedule(ctx, status="ESCALATED")


def _booked_start(sched: dict[str, Any] | None, now: datetime) -> datetime | None:
    """Start of this thread's meeting if it is still upcoming (a past meeting no longer counts)."""
    if not sched or not sched.get("meeting_start"):
        return None
    start = datetime.fromisoformat(sched["meeting_start"])
    return start if start > now else None


def _existing_booking(
    ctx: TechSupportContext, sched: dict[str, Any] | None
) -> str | None:
    start = _booked_start(sched, ctx.clock())
    if start is None or sched is None:
        return None
    ctx.meeting_link_token = add_to_vault(ctx.vault, "LINK", sched["meeting_link"])
    ctx.am_email = ctx.sop_owner_email = sched["am_email"] or ctx.sop_owner_email
    ctx.thread_status = MEETING_CONFIRMED
    return (
        f"ALREADY_BOOKED: a meeting is already confirmed for {sch.iso_local(start)} ({sch.human(start)}) "
        f"with link {ctx.meeting_link_token}. No new invites were sent. If the customer says this time does "
        "not work or asks to change it, call check_meeting_availability again with reschedule=true and "
        "the new times they asked for (or [])."
    )


def _load_schedule(
    ctx: TechSupportContext, part: str | None, now: datetime
) -> dict[str, Any] | None:
    """This thread's schedule; drops a meeting that already happened; adopts an upcoming meeting that the
    same customer booked on another thread (a new email about an existing meeting)."""
    sched = db.get_tech_schedule(ctx.thread_id, ctx.db_path)
    if sched and sched.get("meeting_start") and _booked_start(sched, now) is None:
        ctx.log(
            "meeting_lookup",
            note="previous meeting is in the past; starting a fresh booking",
            local_only=True,
        )
        sched = _schedule(
            ctx,
            meeting_start=None,
            meeting_link=None,
            engineer_email=None,
            rounds=0,
            offered_slots=[],
            status="NEW",
        )
    if _booked_start(sched, now) is None:
        other = db.find_upcoming_meeting(
            ctx.customer_email, part, sch.to_utc_iso(now), ctx.thread_id, ctx.db_path
        )
        if other:
            ctx.log(
                "meeting_lookup",
                adopted_from_thread=other["thread_id"],
                part_number=other["part_number"],
                meeting_start=other["meeting_start"],
                local_only=True,
            )
            sched = db.move_tech_schedule(
                other["thread_id"], ctx.thread_id, ctx.db_path
            )
    return sched


# -----------tools-----------


def check_meeting_availability_impl(
    ctx: TechSupportContext,
    part_token: str,
    requested_starts: list[str] | None = None,
    reschedule: bool = False,
) -> str:
    tool = "check_meeting_availability"
    requested_starts = list(requested_starts or [])
    ctx.log(
        "tool_call",
        tool=tool,
        args={
            "part_token": part_token,
            "requested_starts": requested_starts,
            "reschedule": reschedule,
        },
    )
    ctx.availability_checked = True
    ctx.bookable_slots.clear()
    now = ctx.clock()

    part = _resolve(ctx, part_token, "PART")
    sched = _load_schedule(ctx, part, now)
    if part is None and sched and _booked_start(
        sched, now
    ):  # e.g. "I need to move my meeting" with no part number
        part = sched["part_number"]
        part_token = add_to_vault(ctx.vault, "PART", part)
    if part is None:
        return _result(
            ctx,
            tool,
            "MISSING_INFO: unknown or missing part token. Ask the customer for the part "
            "number with send_tech_support_emails(awaiting_customer=true).",
        )

    current = _booked_start(sched, now)
    ctx.rescheduling = bool(current) and (
        reschedule or (sched or {}).get("status") == "RESCHEDULING"
    )
    if current and not ctx.rescheduling:
        booked = _existing_booking(ctx, sched)
        assert booked is not None  # `current` is set, so there is a booking
        if part_token not in booked:
            booked += f" The meeting is about {part_token}."
        return _result(ctx, tool, booked)

    # Ownership check: the sender must have bought this part.
    orders = db.find_orders_by_part_for_customer(part, ctx.customer_email, ctx.db_path)
    if not orders:
        if db.part_exists(part, ctx.db_path):
            return _reject_unowned(ctx, part, part_token)
        return _result(
            ctx,
            tool,
            f"NOT_FOUND: {part_token} does not match any product we sell. Ask the customer "
            "to double-check the part number (awaiting_customer=true).",
        )
    ctx.part_number = part
    am_email: str = orders[0]["sop_owner_email"]
    ctx.am_email = ctx.sop_owner_email = am_email
    ctx.log(
        "ownership",
        passed=True,
        sender=ctx.customer_email,
        part_number=part,
        local_only=True,
    )

    engineers = db.engineers_for_part(part, ctx.db_path)
    if not engineers:
        _schedule(ctx, part_number=part, am_email=ctx.am_email, status="NO_ENGINEER")
        return _result(
            ctx,
            tool,
            f"NO_ENGINEER: no engineer is assigned to {part_token}. Tell the customer their "
            "account manager will follow up and include a sop_summary (awaiting_customer=false).",
        )

    valid, errors = [], []
    for raw in requested_starts:
        try:
            slot = sch.parse_slot(raw, now)
        except sch.SlotError as exc:
            errors.append(str(exc))
            continue
        if current and slot == sch.to_local(current):
            errors.append(
                f"{sch.iso_local(slot)} is the meeting time they want to change"
            )
        else:
            valid.append(slot)

    # When moving a meeting, its own calendar entries don't count as busy.
    skip = ctx.thread_id if ctx.rescheduling else None
    am_busy = db.busy_intervals(am_email, ctx.db_path, exclude_thread=skip)
    eng_busy = {
        e: db.busy_intervals(e, ctx.db_path, exclude_thread=skip) for e in engineers
    }
    checks = []
    for slot in valid:
        free_eng = next((e for e in engineers if sch.is_free(eng_busy[e], slot)), None)
        checks.append(
            {
                "slot": sch.iso_local(slot),
                "account_manager_free": sch.is_free(am_busy, slot),
                "engineer_free": free_eng is not None,
                "engineer": free_eng,
            }
        )
    available = [
        c["slot"] for c in checks if c["engineer_free"] and c["account_manager_free"]
    ]
    ctx.log(
        "calendar_check",
        part_number=part,
        account_manager=ctx.am_email,
        engineers=engineers,
        requested=checks,
        invalid=errors,
        local_only=True,
    )

    if available:
        _schedule(
            ctx,
            part_number=part,
            am_email=ctx.am_email,
            status="RESCHEDULING" if ctx.rescheduling else "SLOT_AVAILABLE",
        )
        ctx.bookable_slots.add(available[0])
        slot = sch.parse_slot(available[0], now)
        verb = "move the meeting to it" if ctx.rescheduling else "book it"
        return _result(
            ctx,
            tool,
            f"AVAILABLE: {available[0]} ({sch.human(slot)}) works for the engineer and the "
            f'account manager. Call book_tech_meeting with start="{available[0]}" to {verb}.',
        )

    # Nothing requested works: offer the shared open slots closest to what was asked (or the soonest).
    prior = [
        sch.parse_slot(s, now)
        for s in (sched or {}).get("offered_slots", [])
        if _valid(s, now)
    ]
    exclude = ([] if valid else prior) + ([sch.to_local(current)] if current else [])
    offers: list[datetime] = []
    for e in engineers:
        for s in sch.common_free_slots(
            [eng_busy[e], am_busy],
            now,
            near=valid or None,
            limit=3,
            exclude=exclude or None,
        ):
            if s not in offers and len(offers) < 3:
                offers.append(s)
    offers.sort()
    if not offers:
        _schedule(ctx, part_number=part, am_email=ctx.am_email, status="NO_SLOTS")
        return _result(
            ctx,
            tool,
            "NO_SLOTS: no shared open time in the next two weeks. Tell the customer their "
            "account manager will follow up and include a sop_summary (awaiting_customer=false).",
        )
    _schedule(
        ctx,
        part_number=part,
        am_email=ctx.am_email,
        status="RESCHEDULING" if ctx.rescheduling else "SLOTS_OFFERED",
        offered_slots=[sch.iso_local(s) for s in offers],
    )
    head = (
        "UNAVAILABLE: the requested time(s) conflict with the engineer's or account manager's calendar."
        if valid
        else "NO_TIME_REQUESTED: the customer did not give a usable time."
    )
    if errors:
        head += " Invalid: " + "; ".join(errors) + "."
    if current:
        head += (
            f" Their current meeting ({sch.iso_local(current)}, {sch.human(current)}) stays booked until "
            "they pick a new time."
        )
    lines = "\n".join(_slot_line(i, s) for i, s in enumerate(offers, 1))
    return _result(
        ctx,
        tool,
        f"{head}\nDo NOT book any of these yourself: the customer must choose. Offer them "
        f"with send_tech_support_emails(awaiting_customer=true):\n{lines}",
    )


def _valid(s: str, now: datetime) -> bool:
    try:
        sch.parse_slot(s, now)
        return True
    except sch.SlotError:
        return False


def _reject_unowned(ctx: TechSupportContext, part: str, part_token: str) -> str:
    ctx.log(
        "ownership",
        passed=False,
        sender=ctx.customer_email,
        part_number=part,
        local_only=True,
    )
    if not db.customer_has_orders(ctx.customer_email, ctx.db_path):
        # Never ordered anything: most likely a prospect asking about a product. A normal hand-off,
        # not a security event (no lock). Existing customers asking about a part they didn't buy
        # still get the [SECURITY] path below.
        _escalate(
            ctx,
            db.SOP_OWNER_EMAIL,
            f"[ESCALATION] Tech question from a sender with no previous orders ({part})",
            (
                f"{ctx.customer_email} asked for an engineering meeting about {part} but has never ordered "
                f"from us (possible new customer).\n\nNo engineer was contacted and nothing was booked. "
                f"Thread: {ctx.thread_id}.\n\n--- Customer message ---\n{ctx.customer_message}"
            ),
            reply=NEW_CONTACT_REPLY,
        )
        return _result(
            ctx,
            "check_meeting_availability",
            f"ESCALATED: the sender has no orders with us, so {part_token} support was handed to the SOP team "
            "and the customer was already notified. Do not send any further emails; just stop.",
        )
    _escalate(
        ctx,
        db.SOP_OWNER_EMAIL,
        f"[SECURITY] Tech support request for a part the sender never bought ({part})",
        (
            f"{ctx.customer_email} asked for an engineering meeting about {part}, but no order of {part} "
            f"belongs to this sender.\n\nNo engineer was contacted and nothing was booked. "
            f"Thread: {ctx.thread_id}. Please review manually."
        ),
        lock=True,
    )
    return _result(
        ctx,
        "check_meeting_availability",
        f"REJECTED: the sender has no purchase of {part_token}. The request was escalated to the SOP "
        "team and the customer was already notified. Do not send any further emails; just stop.",
    )


def book_tech_meeting_impl(ctx: TechSupportContext, part_token: str, start: str) -> str:
    tool = "book_tech_meeting"
    ctx.log("tool_call", tool=tool, args={"part_token": part_token, "start": start})
    if ctx.locked:
        return _result(ctx, tool, "SKIPPED: this thread is locked; nothing was booked.")
    if not ctx.availability_checked or ctx.am_email is None:
        sched = db.get_tech_schedule(ctx.thread_id, ctx.db_path)
        if booked := _existing_booking(ctx, sched):
            return _result(ctx, tool, booked)
        return _result(
            ctx,
            tool,
            "BLOCKED: call check_meeting_availability first. Nothing was booked.",
        )

    part = _resolve(ctx, part_token, "PART")
    if part is None or part != ctx.part_number:
        return _result(
            ctx,
            tool,
            "BLOCKED: use the same part token that check_meeting_availability verified.",
        )

    sched = db.get_tech_schedule(ctx.thread_id, ctx.db_path)
    now = ctx.clock()
    current = _booked_start(sched, now)
    if (
        current and not ctx.rescheduling
    ):  # DB idempotency: one meeting (and one invite) per thread
        booked = _existing_booking(ctx, sched)
        assert booked is not None  # `current` is set, so there is a booking
        return _result(ctx, tool, booked)

    try:
        slot = sch.parse_slot(start, now)
    except sch.SlotError as exc:
        return _result(
            ctx, tool, f"INVALID: {exc}. Call check_meeting_availability again."
        )

    # The customer must choose: only a time they asked for that the check confirmed can be booked.
    if sch.iso_local(slot) not in ctx.bookable_slots:
        ctx.log(
            "guard",
            rule="customer_must_choose",
            start=sch.iso_local(slot),
            local_only=True,
        )
        return _result(
            ctx,
            tool,
            f"BLOCKED: {sch.iso_local(slot)} was not requested by the customer and confirmed "
            "AVAILABLE by check_meeting_availability. Nothing was booked. Offer the open "
            "slots with send_tech_support_emails(awaiting_customer=true) and let the "
            "customer pick.",
        )

    skip = ctx.thread_id if current else None
    old_engineer = (sched or {}).get("engineer_email") if current else None
    candidates = db.engineers_for_part(part, ctx.db_path)
    if (
        old_engineer in candidates
    ):  # keep the same engineer when moving, if they're free
        candidates = [old_engineer] + [e for e in candidates if e != old_engineer]
    engineer = next(
        (
            e
            for e in candidates
            if sch.is_free(db.busy_intervals(e, ctx.db_path, exclude_thread=skip), slot)
        ),
        None,
    )
    if engineer is None or not sch.is_free(
        db.busy_intervals(ctx.am_email, ctx.db_path, exclude_thread=skip), slot
    ):
        return _result(
            ctx,
            tool,
            f"SLOT_TAKEN: {sch.iso_local(slot)} is no longer free. Call "
            "check_meeting_availability with an empty list to get new options.",
        )

    if current:
        db.delete_thread_meetings(ctx.thread_id, ctx.db_path)
        assert sched is not None  # `current` comes from this schedule
        link = sched[
            "meeting_link"
        ]  # a moved meeting keeps its link, like a moved calendar invite
    else:
        link = f"https://meet.axmattsupply.com/TS-{secrets.token_hex(4)}"
    end = slot + sch.MEETING
    title = f"Tech support: {part} with {ctx.customer_email}"
    for owner in (engineer, ctx.am_email):
        db.add_calendar_event(
            owner,
            sch.to_utc_iso(slot),
            sch.to_utc_iso(end),
            title,
            "MEETING",
            ctx.thread_id,
            ctx.db_path,
        )

    link_token = add_to_vault(ctx.vault, "LINK", link)
    ctx.meeting_link_token = link_token
    ctx.thread_status = MEETING_CONFIRMED
    ctx.bookable_slots.clear()
    issue = (sched or {}).get("issue_text") or ctx.customer_message
    _schedule(
        ctx,
        engineer_email=engineer,
        meeting_start=sch.to_utc_iso(slot),
        meeting_link=link,
        offered_slots=[],
        rounds=0,
        status="CONFIRMED",
    )  # a later reschedule gets fresh rounds

    when = sch.human(slot)
    eng_name, am_name = (
        db.staff_name(engineer, ctx.db_path),
        db.staff_name(ctx.am_email, ctx.db_path),
    )
    invite = (
        f"When: {when} (30 min)\nJoin: {link}\n\nCustomer: {ctx.customer_email}\nPart: {part}\n"
        f"Engineer: {eng_name}\nAccount manager: {am_name}\n\n--- Customer's description ---\n{issue}"
    )
    invites = [ENGINEER_INBOX, SOP_EMPLOYEE_INBOX]
    if current:
        was = sch.human(current)
        moved = f"The customer asked to move this call.\nWas: {was}\nNow: {when}\n\n"
        if engineer == old_engineer:  # one updated invite, no questions
            _send(
                ctx,
                ENGINEER_INBOX,
                engineer,
                f"[Updated invite] Tech support call: {part} · {when}",
                f"Hi {eng_name.split()[0]},\n\n{moved}The new slot is open on your calendar.\n\n{invite}",
            )
        else:
            if old_engineer:
                old_name = db.staff_name(old_engineer, ctx.db_path)
                _send(
                    ctx,
                    ENGINEER_INBOX,
                    old_engineer,
                    f"[Cancelled] Tech support call: {part} · {was}",
                    f"Hi {old_name.split()[0]},\n\nThe customer moved this call to a time you're busy, so a "
                    f"colleague will cover it. It has been removed from your calendar.\n\nNo action needed.",
                )
                invites.insert(0, ENGINEER_INBOX)
            _send(
                ctx,
                ENGINEER_INBOX,
                engineer,
                f"[Invite] Tech support call: {part} · {when}",
                f"Hi {eng_name.split()[0]},\n\nThis slot was open on your calendar, so it has been booked for "
                f"you.\n\n{invite}",
            )
        _send(
            ctx,
            SOP_EMPLOYEE_INBOX,
            ctx.am_email,
            f"[Updated invite] Tech support call: {part} · {when}",
            f"Hi {am_name.split()[0]},\n\n{moved}{invite}",
        )
        ctx.log(
            "booking",
            rescheduled=True,
            engineer=engineer,
            previous_engineer=old_engineer,
            account_manager=ctx.am_email,
            previous_start=sch.iso_local(current),
            start=sch.iso_local(slot),
            meeting_link=link,
            link_token=link_token,
            invites=invites,
            local_only=True,
        )
        return _result(
            ctx,
            tool,
            f"RESCHEDULED: the meeting moved from {sch.iso_local(current)} to "
            f"{sch.iso_local(slot)} ({when}), same link {link_token}. The engineer and the "
            "account manager were updated. Now call send_tech_support_emails confirming the "
            "new date/time with the link token; use an empty sop_summary.",
        )

    # Exactly one email to the engineer: the invite itself. No availability questions.
    _send(
        ctx,
        ENGINEER_INBOX,
        engineer,
        f"[Invite] Tech support call: {part} · {when}",
        f"Hi {eng_name.split()[0]},\n\nThis slot was open on your calendar, so it has been booked for you.\n\n"
        f"{invite}",
    )
    _send(
        ctx,
        SOP_EMPLOYEE_INBOX,
        ctx.am_email,
        f"[Invite] Tech support call: {part} · {when}",
        f"Hi {am_name.split()[0]},\n\nA technical session for your account was booked.\n\n{invite}",
    )
    ctx.log(
        "booking",
        engineer=engineer,
        account_manager=ctx.am_email,
        start=sch.iso_local(slot),
        meeting_link=link,
        link_token=link_token,
        invites=invites,
        local_only=True,
    )
    return _result(
        ctx,
        tool,
        f"BOOKED: meeting on {sch.iso_local(slot)} ({when}), link {link_token}. Invites were "
        "sent to the engineer and the account manager. Now call send_tech_support_emails "
        "confirming the date/time with the link token; use an empty sop_summary.",
    )


def send_tech_support_emails_impl(
    ctx: TechSupportContext,
    customer_subject: str,
    customer_body: str,
    sop_summary: str = "",
    awaiting_customer: bool = False,
) -> str:
    tool = "send_tech_support_emails"
    ctx.log(
        "tool_call",
        tool=tool,
        args={
            "customer_subject": customer_subject,
            "customer_body": customer_body,
            "sop_summary": sop_summary,
            "awaiting_customer": awaiting_customer,
        },
    )
    if ctx.locked or ctx.thread_status == ESCALATED_TO_HUMAN:
        return _result(
            ctx,
            tool,
            "SKIPPED: this thread was handed to a human; no emails were sent.",
        )

    # Never email a sender about a part before the ownership/calendar check has run.
    if not ctx.availability_checked and tokens_of(ctx.vault, "PART"):
        ctx.log("guard", rule="check_before_send", local_only=True)
        return _result(
            ctx,
            tool,
            "BLOCKED: a part token is known, so call check_meeting_availability first. "
            "No emails were sent.",
        )

    sched = db.get_tech_schedule(ctx.thread_id, ctx.db_path)
    rounds = (sched or {}).get("rounds", 0)
    if awaiting_customer and rounds >= MAX_SCHEDULING_ROUNDS:
        ctx.log("guard", rule="max_scheduling_rounds", rounds=rounds, local_only=True)
        _escalate(
            ctx,
            ctx.am_email or db.SOP_OWNER_EMAIL,
            f"[ESCALATION] Could not agree on a tech support meeting with {ctx.customer_email}",
            (
                f"After {rounds} scheduling emails, {ctx.customer_email} has not accepted a slot.\n"
                f"Part: {(sched or {}).get('part_number') or 'unknown'}. Thread: {ctx.thread_id}.\n"
                f"Last slots offered: {', '.join((sched or {}).get('offered_slots') or []) or 'none'}.\n\n"
                f"--- Latest customer message ---\n{ctx.customer_message}"
            ),
        )
        return _result(
            ctx,
            tool,
            f"ESCALATED: the {MAX_SCHEDULING_ROUNDS}-round scheduling limit was reached. "
            "A human was notified and the customer got a holding reply. Stop.",
        )

    if ctx.meeting_link_token and ctx.meeting_link_token not in customer_body:
        customer_body = (
            customer_body.rstrip() + f"\n\nMeeting link: {ctx.meeting_link_token}"
        )
        ctx.log(
            "link_appended",
            link_token=ctx.meeting_link_token,
            note="LLM omitted the link token; appended locally",
        )

    sent = _dispatch(ctx, customer_subject, customer_body, sop_summary)
    if awaiting_customer:
        ctx.thread_status = AWAITING_CUSTOMER_SCHEDULE
        keep = (sched or {}).get("status") == "RESCHEDULING"
        _schedule(
            ctx,
            rounds=rounds + 1,
            status="RESCHEDULING" if keep else "AWAITING_CUSTOMER",
        )
    elif ctx.thread_status is None:
        ctx.thread_status = "REPLIED"
    ctx.log("dispatch", email_ids=sent, local_only=True)
    return _result(ctx, tool, f"SENT: {len(sent)} email(s) dispatched.")


# -----------SDK tools-----------


@function_tool(
    failure_error_function=None
)  # tool errors abort the run -> human escalation
def check_meeting_availability(
    ctx: RunContextWrapper[TechSupportContext],
    part_token: str,
    requested_starts: list[str],
    reschedule: bool = False,
) -> str:
    """Verify the sender owns the part, then check engineer + account manager calendars.

    Args:
        part_token: The part token exactly as it appears, e.g. <PART_TOKEN_1>. Use "" if none is known.
        requested_starts: Meeting start times the customer asked for, as local ISO datetimes like
            2026-11-12T14:00. Use an empty list if the customer gave no usable time.
        reschedule: True if the customer wants to move a meeting that is already booked.
    """
    return guard_tool_result(
        ctx.context,
        "check_meeting_availability",
        check_meeting_availability_impl(
            ctx.context, part_token, requested_starts, reschedule
        ),
    )


@function_tool(
    failure_error_function=None
)  # tool errors abort the run -> human escalation
def book_tech_meeting(
    ctx: RunContextWrapper[TechSupportContext], part_token: str, start: str
) -> str:
    """Book (or move) the meeting at a time the customer asked for that check_meeting_availability returned AVAILABLE.

    Invites/updates go to the engineer and account manager automatically.

    Args:
        part_token: The part token verified by check_meeting_availability.
        start: Local ISO start time, e.g. 2026-11-12T14:00.
    """
    return guard_tool_result(
        ctx.context,
        "book_tech_meeting",
        book_tech_meeting_impl(ctx.context, part_token, start),
    )


@function_tool(
    failure_error_function=None
)  # tool errors abort the run -> human escalation
def send_tech_support_emails(
    ctx: RunContextWrapper[TechSupportContext],
    customer_subject: str,
    customer_body: str,
    sop_summary: str,
    awaiting_customer: bool,
) -> str:
    """Send the customer reply (and an optional internal note to the account manager). Tokens are filled in locally.

    Args:
        customer_subject: Subject line of the reply to the customer.
        customer_body: Full plain-text reply. Reference parts and links only by token.
        sop_summary: Short internal note for the account manager. Use an empty string to skip.
        awaiting_customer: True if this email asks the customer for a part number or to pick a time.
    """
    return guard_tool_result(
        ctx.context,
        "send_tech_support_emails",
        send_tech_support_emails_impl(
            ctx.context, customer_subject, customer_body, sop_summary, awaiting_customer
        ),
    )


TECH_AGENT_INSTRUCTIONS = """\
You are the Tech Support Scheduling Specialist for a B2B Sales Operations team. You set up a 30-minute
troubleshooting video call between the customer, one of our engineers who knows their part, and the
customer's account manager.

Privacy: part numbers and meeting links are replaced by tokens such as <PART_TOKEN_1> and <LINK_TOKEN_1>.
Copy tokens exactly. Never invent part numbers, links, names, or email addresses. You do not know who the
engineer or account manager is: call them "one of our engineers" and "your account manager".

Times: use the timezone and the list of upcoming business days given in the input. Convert what the
customer asked for ("Thursday 2pm", "the 12th at 10:30") into local ISO datetimes like 2026-11-12T14:00.
If the customer picks one of the slots offered earlier ("option 2 works"), use that exact slot.

Procedure:
1. Call check_meeting_availability once with the part token (or "" if none is known), the times the customer
   asked for (or []), and reschedule=true if they want to change, move or can't make a meeting that is
   already booked (otherwise false). It may find their existing meeting even if this is a new email.
2. Act on its result:
   - AVAILABLE: call book_tech_meeting with that start time.
   - UNAVAILABLE or NO_TIME_REQUESTED: call send_tech_support_emails with awaiting_customer=true. If their
     time did not work, say so briefly. List the offered slots as a numbered list with the date, time and
     timezone, and ask them to reply with the option that works. NEVER book an offered slot yourself; only
     the customer chooses.
   - ALREADY_BOOKED: if the customer wants a different time, call check_meeting_availability again with
     reschedule=true. Otherwise remind them of the existing meeting (time + link token), awaiting_customer=false.
   - NOT_FOUND or MISSING_INFO: call send_tech_support_emails with awaiting_customer=true asking for (or to
     double-check) the part number and two or three times that work for them.
   - NO_SLOTS or NO_ENGINEER: tell the customer their account manager will follow up personally; include a
     sop_summary; awaiting_customer=false.
   - REJECTED or ESCALATED: do nothing else.
3. After BOOKED or RESCHEDULED: call send_tech_support_emails (awaiting_customer=false, sop_summary="")
   confirming the date, time and timezone, that one of our engineers and their account manager will join,
   and the meeting link token verbatim.
4. Never email or ask the engineer anything; invites and updates are sent automatically when you book. Call
   send_tech_support_emails at most once. Finish with one short sentence describing what you did.
Sign customer emails as "Sales Operations Team".
"""


def build_tech_agent(model: str | Model | None = None) -> Agent[TechSupportContext]:
    return Agent[TechSupportContext](
        name="Tech Support Agent",
        instructions=TECH_AGENT_INSTRUCTIONS,
        model=model or TECH_AGENT_MODEL,
        tools=[check_meeting_availability, book_tech_meeting, send_tech_support_emails],
        # One tool call per model turn: the ownership/calendar check always finishes before booking or email.
        model_settings=ModelSettings(parallel_tool_calls=False),
    )


def build_tech_agent_input(
    masked_text: str,
    vault: dict[str, str],
    now: datetime,
    sched: dict[str, Any] | None = None,
) -> str:
    """Masked message + token names + calendar framing. No real values, names, or emails."""
    days = sch.business_days(now)
    parts = tokens_of(vault, "PART")
    lines = [
        "New customer email (PII masked):",
        masked_text,
        "",
        f"Now: {sch.human(now)} ({sch.BUSINESS_TZ_NAME}), today is {now:%A %Y-%m-%d}.",
        "Bookable business days: "
        + ", ".join(f"{d:%a} {d.isoformat()}" for d in days)
        + ".",
        f"Meetings: 30 minutes, starting on the hour or half hour, "
        f"{sch.DAY_START:%H:%M}-{sch.DAY_END:%H:%M} local time.",
        f"Known PART tokens in this thread: {', '.join(parts) if parts else 'none'}",
    ]
    offered = (sched or {}).get("offered_slots") or []
    if offered:
        lines.append(
            "Slots offered to this customer previously: "
            + "; ".join(
                _slot_line(i, sch.parse_slot(s, now))
                if _valid(s, now)
                else f"{i}) {s} (no longer bookable)"
                for i, s in enumerate(offered, 1)
            )
        )
    booked = _booked_start(sched, now)
    if booked:
        lines.append(
            f"A meeting is already booked on this thread for {sch.iso_local(booked)} "
            f"({sch.human(booked)})."
        )
    return "\n".join(lines)


async def run_tech_agent(
    ctx: TechSupportContext,
    masked_text: str,
    model: str | Model | None = None,
    session_db_path: str | None = None,
) -> dict[str, Any]:
    sched = db.get_tech_schedule(ctx.thread_id, ctx.db_path)
    if not sched or not sched.get(
        "issue_text"
    ):  # keep the first description for the engineer's invite
        sched = db.save_tech_schedule(
            ctx.thread_id,
            ctx.customer_email,
            ctx.db_path,
            issue_text=ctx.customer_message,
            status=(sched or {}).get("status", "NEW"),
        )
    agent_input = build_tech_agent_input(masked_text, ctx.vault, ctx.clock(), sched)
    return await run_agent(
        build_tech_agent(model), ctx, agent_input, session_db_path=session_db_path
    )


# -----------demo helpers-----------


def _spoken(dt: datetime) -> str:
    d = sch.to_local(dt)
    return f"{d:%A}, {d:%B} {d.day} at " + d.strftime("%I:%M %p").lstrip("0")


def demo_slots(
    db_path: str | None = None,
    part: str = "AB-1234",
    customer: str = "john@acmecorp.com",
) -> dict[str, Any]:
    """Live calendar facts the UI presets quote: open shared slots and a slot where every engineer is busy."""
    now = sch.now_local()
    orders = db.find_orders_by_part_for_customer(part, customer, db_path)
    engineers = db.engineers_for_part(part, db_path)
    if not orders or not engineers:
        return {}
    am_busy = db.busy_intervals(orders[0]["sop_owner_email"], db_path)
    eng_busy = [db.busy_intervals(e, db_path) for e in engineers]
    free = sch.common_free_slots([eng_busy[0], am_busy], now, limit=2)
    busy = next(
        (
            s
            for s in sch.candidate_slots(now)
            if sch.is_free(am_busy, s) and not any(sch.is_free(b, s) for b in eng_busy)
        ),
        None,
    )
    out: dict[str, Any] = {"tz": sch.BUSINESS_TZ_NAME}
    for key, slot in (
        ("free_slot", free[0] if free else None),
        ("free_slot_2", free[1] if len(free) > 1 else None),
        ("busy_slot", busy),
    ):
        if slot:
            out[key] = {
                "iso": sch.iso_local(slot),
                "spoken": _spoken(slot),
                "human": sch.human(slot),
            }
    if (
        part == "AB-1234"
    ):  # "Engineer Busy" preset: Mary asks about EF-9012 when Marco is busy, David is free
        other = demo_slots(
            db_path, part="EF-9012", customer="mary@betaindustries.com"
        ).get("busy_slot")
        if other:
            out["busy_slot_mary"] = other
    return out
