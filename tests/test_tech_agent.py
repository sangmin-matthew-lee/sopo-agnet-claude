from core import db
from core import scheduling as sch
from core.masker import contains_raw_pii
from sop_agents.tech_agent import (
    ENGINEER_INBOX,
    MAX_SCHEDULING_ROUNDS,
    TechSupportContext,
    book_tech_meeting_impl,
    build_tech_agent,
    build_tech_agent_input,
    check_meeting_availability_impl,
    demo_slots,
    run_tech_agent,
    send_tech_support_emails_impl,
)
from tests.fake_model import ScriptedModel

VAULT = {"<PART_TOKEN_1>": "AB-1234"}


def ctx_for(db_path, email="john@acmecorp.com", vault=None, thread_id="t1"):
    return TechSupportContext(thread_id=thread_id, customer_email=email, vault=dict(VAULT if vault is None else vault),
                              db_path=db_path, customer_message="The AB-1234 unit overheats after 10 minutes.")


def inbox(db_path, kind):
    """Oldest first."""
    return sorted((e for e in db.list_emails(db_path) if e["inbox_type"] == kind), key=lambda e: e["id"])


def slots(db_path):
    return demo_slots(db_path)


# --------------------------------------------------------- availability tool

def test_requested_free_slot_is_available(db_path):
    free = slots(db_path)["free_slot"]["iso"]
    ctx = ctx_for(db_path)
    out = check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [free])
    assert out.startswith("AVAILABLE") and free in out
    assert not contains_raw_pii(out) and "axmattsupply" not in out
    assert ctx.am_email == db.AM_JOHN
    assert db.list_emails(db_path) == []  # checking calendars never emails anyone


def test_busy_slot_offers_three_alternatives(db_path):
    busy = slots(db_path)["busy_slot"]["iso"]
    out = check_meeting_availability_impl(ctx_for(db_path), "<PART_TOKEN_1>", [busy])
    assert out.startswith("UNAVAILABLE") and "1)" in out and "3)" in out
    offered = db.get_tech_schedule("t1", db_path)["offered_slots"]
    assert len(offered) == 3 and busy not in offered
    assert db.list_emails(db_path) == []


def test_no_times_requested_offers_slots(db_path):
    out = check_meeting_availability_impl(ctx_for(db_path), "<PART_TOKEN_1>", [])
    assert out.startswith("NO_TIME_REQUESTED")
    assert len(db.get_tech_schedule("t1", db_path)["offered_slots"]) == 3


def test_engineer_busy_preset_for_another_client(db_path):
    """Preset 3: Mary's EF-9012 engineer is busy; independent of John's AB-1234 meetings."""
    book(db_path, slots(db_path)["free_slot"]["iso"])  # John already has a meeting (presets 1/2/6)
    busy = slots(db_path)["busy_slot_mary"]["iso"]
    ctx = ctx_for(db_path, email="mary@betaindustries.com", vault={"<PART_TOKEN_1>": "EF-9012"}, thread_id="t3")
    out = check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [busy])
    assert out.startswith("UNAVAILABLE") and "3)" in out
    assert ctx.am_email == db.AM_MARY
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", busy).startswith("BLOCKED")
    assert len(inbox(db_path, ENGINEER_INBOX)) == 1  # only John's invite; Marco not contacted


def test_reoffer_excludes_previous_slots(db_path):
    check_meeting_availability_impl(ctx_for(db_path), "<PART_TOKEN_1>", [])
    first = db.get_tech_schedule("t1", db_path)["offered_slots"]
    check_meeting_availability_impl(ctx_for(db_path), "<PART_TOKEN_1>", [])
    second = db.get_tech_schedule("t1", db_path)["offered_slots"]
    assert not set(first) & set(second)


def test_invalid_times_are_reported(db_path):
    out = check_meeting_availability_impl(ctx_for(db_path), "<PART_TOKEN_1>", ["tomorrow-ish"])
    assert out.startswith("NO_TIME_REQUESTED") and "Invalid" in out


def test_missing_part_token(db_path):
    out = check_meeting_availability_impl(ctx_for(db_path, vault={}), "<PART_TOKEN_1>", [])
    assert out.startswith("MISSING_INFO")


def test_unknown_part_not_found(db_path):
    ctx = ctx_for(db_path, vault={"<PART_TOKEN_1>": "ZZ-0000"})
    assert check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", []).startswith("NOT_FOUND")
    assert not ctx.locked


def test_sender_not_owner_is_rejected_and_escalated(db_path):
    ctx = ctx_for(db_path, email="mary@betaindustries.com")
    out = check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [])
    assert out.startswith("REJECTED") and not contains_raw_pii(out)
    assert ctx.locked and ctx.thread_status == "ESCALATED_TO_HUMAN"
    assert inbox(db_path, ENGINEER_INBOX) == []
    sop = inbox(db_path, "SOP_EMPLOYEE_INBOX")
    assert len(sop) == 1 and sop[0]["recipient_email"] == db.SOP_OWNER_EMAIL and "AB-1234" in sop[0]["body"]
    assert "AB-1234" not in inbox(db_path, "CUSTOMER_INBOX")[0]["body"]
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", slots(db_path)["free_slot"]["iso"]).startswith("SKIPPED")
    assert send_tech_support_emails_impl(ctx, "x", "y").startswith("SKIPPED")


# --------------------------------------------------------- booking tool

def test_prospect_with_no_orders_is_handed_off_not_flagged(db_path):
    """Someone who never ordered asking about a product is a possible customer, not an attacker."""
    ctx = ctx_for(db_path, email="buyer@newprospect.com")
    out = check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [])
    assert out.startswith("ESCALATED") and not contains_raw_pii(out)
    assert ctx.thread_status == "ESCALATED_TO_HUMAN" and not ctx.locked
    sop = inbox(db_path, "SOP_EMPLOYEE_INBOX")[0]
    assert "[SECURITY]" not in sop["subject"] and "no previous orders" in sop["subject"]
    assert "our team" in inbox(db_path, "CUSTOMER_INBOX")[0]["body"]
    assert inbox(db_path, ENGINEER_INBOX) == []


def test_book_before_check_is_blocked(db_path):
    out = book_tech_meeting_impl(ctx_for(db_path), "<PART_TOKEN_1>", slots(db_path)["free_slot"]["iso"])
    assert out.startswith("BLOCKED") and db.list_emails(db_path) == []


def test_booking_sends_exactly_one_invite_each(db_path):
    free = slots(db_path)["free_slot"]["iso"]
    ctx = ctx_for(db_path)
    check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [free])
    out = book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", free)
    assert out.startswith("BOOKED") and "<LINK_TOKEN_1>" in out
    assert not contains_raw_pii(out) and "axmattsupply" not in out
    assert ctx.thread_status == "MEETING_CONFIRMED"
    link = ctx.vault["<LINK_TOKEN_1>"]
    assert link.startswith("https://meet.axmattsupply.com/TS-")

    eng = inbox(db_path, ENGINEER_INBOX)
    assert len(eng) == 1 and eng[0]["recipient_email"] == db.ENG_ALEX
    assert link in eng[0]["body"] and "overheats" in eng[0]["body"] and "AB-1234" in eng[0]["body"]
    am = inbox(db_path, "SOP_EMPLOYEE_INBOX")
    assert len(am) == 1 and am[0]["recipient_email"] == db.AM_JOHN

    events = [e for e in db.list_calendar_events(db_path) if e["kind"] == "MEETING"]
    assert {e["owner_email"] for e in events} == {db.ENG_ALEX, db.AM_JOHN}
    sched = db.get_tech_schedule("t1", db_path)
    assert sched["status"] == "CONFIRMED" and sched["meeting_link"] == link

    # Booking again (same turn or a later turn) reuses the meeting and never re-invites.
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", free).startswith("ALREADY_BOOKED")
    ctx2 = ctx_for(db_path)
    assert check_meeting_availability_impl(ctx2, "<PART_TOKEN_1>", []).startswith("ALREADY_BOOKED")
    assert ctx2.vault["<LINK_TOKEN_1>"] == link
    assert len(inbox(db_path, ENGINEER_INBOX)) == 1


def test_booking_falls_back_to_second_engineer(db_path):
    free = slots(db_path)["free_slot"]["iso"]
    start = sch.parse_slot(free, sch.now_local())
    db.add_calendar_event(db.ENG_ALEX, sch.to_utc_iso(start), sch.to_utc_iso(start + sch.MEETING),
                          "Surprise", "BUSY", None, db_path)
    ctx = ctx_for(db_path)
    assert check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [free]).startswith("AVAILABLE")
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", free).startswith("BOOKED")
    assert [e["recipient_email"] for e in inbox(db_path, ENGINEER_INBOX)] == [db.ENG_PRIYA]


def test_booking_a_slot_taken_after_the_check(db_path):
    free = slots(db_path)["free_slot"]["iso"]
    ctx = ctx_for(db_path)
    assert check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [free]).startswith("AVAILABLE")
    start = sch.parse_slot(free, sch.now_local())
    for who in (db.AM_JOHN,):  # someone grabs the AM's slot between check and booking
        db.add_calendar_event(who, sch.to_utc_iso(start), sch.to_utc_iso(start + sch.MEETING), "x", "BUSY",
                              None, db_path)
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", free).startswith("SLOT_TAKEN")
    assert db.list_emails(db_path) == []


def test_busy_requested_slot_is_never_booked(db_path):
    busy = slots(db_path)["busy_slot"]["iso"]
    ctx = ctx_for(db_path)
    check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [busy])
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", busy).startswith("BLOCKED")
    assert db.list_emails(db_path) == []


def test_offered_slot_is_not_booked_until_the_customer_picks_it(db_path):
    """Bug: with no times given, the model booked an offered slot itself instead of asking."""
    ctx = ctx_for(db_path)
    assert check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", []).startswith("NO_TIME_REQUESTED")
    offered = db.get_tech_schedule("t1", db_path)["offered_slots"]
    out = book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", offered[0])
    assert out.startswith("BLOCKED") and db.list_emails(db_path) == []
    assert not [e for e in db.list_calendar_events(db_path) if e["kind"] == "MEETING"]

    # Next turn: the customer replies "option 1 works" -> re-checked -> AVAILABLE -> booked.
    ctx2 = ctx_for(db_path)
    assert check_meeting_availability_impl(ctx2, "<PART_TOKEN_1>", [offered[0]]).startswith("AVAILABLE")
    assert book_tech_meeting_impl(ctx2, "<PART_TOKEN_1>", offered[0]).startswith("BOOKED")


# --------------------------------------------------------- rescheduling

def book(db_path, start, thread_id="t1", vault=None):
    ctx = ctx_for(db_path, thread_id=thread_id, vault=vault)
    assert check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [start]).startswith("AVAILABLE")
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", start).startswith("BOOKED")
    return ctx


def meetings(db_path):
    return [e for e in db.list_calendar_events(db_path) if e["kind"] == "MEETING"]


def test_reschedule_to_a_requested_time(db_path):
    s = slots(db_path)
    free, free2 = s["free_slot"]["iso"], s["free_slot_2"]["iso"]
    link = book(db_path, free).vault["<LINK_TOKEN_1>"]

    ctx = ctx_for(db_path)
    assert check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", []).startswith("ALREADY_BOOKED")
    ctx = ctx_for(db_path)
    out = check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [free2], reschedule=True)
    assert out.startswith("AVAILABLE")
    out = book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", free2)
    assert out.startswith("RESCHEDULED") and "<LINK_TOKEN_1>" in out and not contains_raw_pii(out)
    assert ctx.vault["<LINK_TOKEN_1>"] == link and ctx.thread_status == "MEETING_CONFIRMED"

    new_start = sch.to_utc_iso(sch.parse_slot(free2, sch.now_local()))
    assert {e["start_at"] for e in meetings(db_path)} == {new_start} and len(meetings(db_path)) == 2
    eng = inbox(db_path, ENGINEER_INBOX)
    assert [e["subject"].split("]")[0] for e in eng] == ["[Invite", "[Updated invite"]
    assert {e["recipient_email"] for e in eng} == {db.ENG_ALEX} and link in eng[1]["body"]
    sched = db.get_tech_schedule("t1", db_path)
    assert sched["status"] == "CONFIRMED" and sched["meeting_start"] == new_start


def test_reschedule_without_times_offers_slots_and_keeps_the_meeting(db_path):
    free = slots(db_path)["free_slot"]["iso"]
    book(db_path, free)
    ctx = ctx_for(db_path)
    out = check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [], reschedule=True)
    assert out.startswith("NO_TIME_REQUESTED") and "stays booked" in out
    offered = db.get_tech_schedule("t1", db_path)["offered_slots"]
    assert len(offered) == 3 and free not in offered
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", offered[0]).startswith("BLOCKED")
    send_tech_support_emails_impl(ctx, "New times", "Which works?", "", True)
    assert db.get_tech_schedule("t1", db_path)["status"] == "RESCHEDULING"
    assert len(meetings(db_path)) == 2  # old meeting kept until a new time is confirmed

    # The customer's pick continues the reschedule even if the model forgets reschedule=true.
    ctx = ctx_for(db_path)
    assert check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [offered[0]]).startswith("AVAILABLE")
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", offered[0]).startswith("RESCHEDULED")
    assert len(meetings(db_path)) == 2


def test_asking_for_the_current_time_is_not_a_move(db_path):
    free = slots(db_path)["free_slot"]["iso"]
    book(db_path, free)
    out = check_meeting_availability_impl(ctx_for(db_path), "<PART_TOKEN_1>", [free], reschedule=True)
    assert out.startswith("NO_TIME_REQUESTED") and "want to change" in out


def test_reschedule_hands_over_when_the_engineer_is_busy(db_path):
    s = slots(db_path)
    free, free2 = s["free_slot"]["iso"], s["free_slot_2"]["iso"]
    book(db_path, free)
    start = sch.parse_slot(free2, sch.now_local())
    db.add_calendar_event(db.ENG_ALEX, sch.to_utc_iso(start), sch.to_utc_iso(start + sch.MEETING), "x", "BUSY",
                          None, db_path)
    with db.get_conn(db_path) as conn:  # make sure the backup engineer is free
        conn.execute("DELETE FROM calendar_events WHERE owner_email = ?", (db.ENG_PRIYA,))
    ctx = ctx_for(db_path)
    assert check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [free2], reschedule=True).startswith("AVAILABLE")
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", free2).startswith("RESCHEDULED")
    eng = [(e["recipient_email"], e["subject"].split("]")[0]) for e in inbox(db_path, ENGINEER_INBOX)]
    assert eng == [(db.ENG_ALEX, "[Invite"), (db.ENG_ALEX, "[Cancelled"), (db.ENG_PRIYA, "[Invite")]
    assert {e["owner_email"] for e in meetings(db_path)} == {db.ENG_PRIYA, db.AM_JOHN}


def test_new_email_thread_adopts_the_existing_meeting(db_path):
    s = slots(db_path)
    link = book(db_path, s["free_slot"]["iso"]).vault["<LINK_TOKEN_1>"]
    ctx = ctx_for(db_path, thread_id="t2")
    out = check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [])
    assert out.startswith("ALREADY_BOOKED") and ctx.vault["<LINK_TOKEN_1>"] == link
    assert db.get_tech_schedule("t1", db_path) is None
    assert {e["thread_id"] for e in meetings(db_path)} == {"t2"}
    assert len(inbox(db_path, ENGINEER_INBOX)) == 1  # no second meeting, no second invite

    # ...and can move it from the new thread.
    ctx = ctx_for(db_path, thread_id="t2")
    free2 = s["free_slot_2"]["iso"]
    assert check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [free2], reschedule=True).startswith("AVAILABLE")
    assert book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", free2).startswith("RESCHEDULED")
    assert len(meetings(db_path)) == 2


def test_booking_resets_the_round_counter(db_path):
    """Rounds spent before a booking must not count against a later reschedule."""
    free = slots(db_path)["free_slot"]["iso"]
    db.save_tech_schedule("t1", "john@acmecorp.com", db_path, rounds=MAX_SCHEDULING_ROUNDS, part_number="AB-1234")
    book(db_path, free)
    assert db.get_tech_schedule("t1", db_path)["rounds"] == 0
    ctx = ctx_for(db_path)
    assert check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [], reschedule=True).startswith("NO_TIME_REQUESTED")
    assert send_tech_support_emails_impl(ctx, "New times", "Which works?", "", True).startswith("SENT")


def test_escalated_reschedule_still_blocks_a_duplicate_meeting(db_path):
    """A reschedule that hit the round limit keeps its meeting; a new email adopts it instead of booking again."""
    s = slots(db_path)
    link = book(db_path, s["free_slot"]["iso"]).vault["<LINK_TOKEN_1>"]
    db.save_tech_schedule("t1", "john@acmecorp.com", db_path, rounds=MAX_SCHEDULING_ROUNDS)
    ctx = ctx_for(db_path)
    check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [], reschedule=True)
    assert send_tech_support_emails_impl(ctx, "More", "How about?", "", True).startswith("ESCALATED")
    assert db.get_tech_schedule("t1", db_path)["status"] == "ESCALATED"
    assert db.has_upcoming_meeting("john@acmecorp.com", sch.to_utc_iso(sch.now_local()), db_path)

    ctx = ctx_for(db_path, thread_id="t2")
    out = check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [s["free_slot_2"]["iso"]])
    assert out.startswith("ALREADY_BOOKED") and ctx.vault["<LINK_TOKEN_1>"] == link
    assert not book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", s["free_slot_2"]["iso"]).startswith("BOOKED")
    assert len(meetings(db_path)) == 2  # still one meeting (engineer + AM), not two
    sched = db.get_tech_schedule("t2", db_path)
    assert sched["status"] == "CONFIRMED" and sched["rounds"] == 0


def test_new_email_without_part_number_finds_the_only_meeting(db_path):
    book(db_path, slots(db_path)["free_slot"]["iso"])
    ctx = ctx_for(db_path, thread_id="t2", vault={})
    out = check_meeting_availability_impl(ctx, "", [])
    assert out.startswith("ALREADY_BOOKED") and "<PART_TOKEN_1>" in out and not contains_raw_pii(out)
    assert ctx.vault["<PART_TOKEN_1>"] == "AB-1234"


def test_past_meeting_does_not_block_a_new_booking(db_path):
    free = slots(db_path)["free_slot"]["iso"]
    book(db_path, free)
    db.save_tech_schedule("t1", "john@acmecorp.com", db_path, rounds=2)
    ctx = ctx_for(db_path)
    ctx.now = sch.parse_slot(free, sch.now_local()) + sch.MEETING * 2  # the meeting is over
    out = check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [])
    assert out.startswith("NO_TIME_REQUESTED")
    sched = db.get_tech_schedule("t1", db_path)
    assert sched["meeting_start"] is None and sched["rounds"] == 0


# --------------------------------------------------------- send tool

def test_send_blocked_before_check(db_path):
    out = send_tech_support_emails_impl(ctx_for(db_path), "Hi", "About <PART_TOKEN_1>", "", True)
    assert out.startswith("BLOCKED") and db.list_emails(db_path) == []


def test_send_asking_for_part_counts_a_round(db_path):
    ctx = ctx_for(db_path, vault={})
    out = send_tech_support_emails_impl(ctx, "Part number?", "Which part is it?", "", True)
    assert out.startswith("SENT: 1") and ctx.thread_status == "AWAITING_CUSTOMER_SCHEDULE"
    assert db.get_tech_schedule("t1", db_path)["rounds"] == 1


def test_confirmation_unmasks_link_and_appends_if_missing(db_path):
    free = slots(db_path)["free_slot"]["iso"]
    ctx = ctx_for(db_path)
    check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [free])
    book_tech_meeting_impl(ctx, "<PART_TOKEN_1>", free)
    send_tech_support_emails_impl(ctx, "Meeting about <PART_TOKEN_1>", "See you then.", "", False)
    cust = inbox(db_path, "CUSTOMER_INBOX")[0]
    assert cust["subject"] == "Meeting about AB-1234"
    assert ctx.vault["<LINK_TOKEN_1>"] in cust["body"] and "<LINK_TOKEN" not in cust["body"]
    assert ctx.thread_status == "MEETING_CONFIRMED"


def test_round_limit_escalates_instead_of_asking_again(db_path):
    db.save_tech_schedule("t1", "john@acmecorp.com", db_path, rounds=MAX_SCHEDULING_ROUNDS, part_number="AB-1234")
    ctx = ctx_for(db_path)
    check_meeting_availability_impl(ctx, "<PART_TOKEN_1>", [])
    out = send_tech_support_emails_impl(ctx, "More times", "How about these?", "", True)
    assert out.startswith("ESCALATED") and ctx.thread_status == "ESCALATED_TO_HUMAN"
    assert inbox(db_path, "SOP_EMPLOYEE_INBOX")[0]["recipient_email"] == db.AM_JOHN
    assert "How about these?" not in inbox(db_path, "CUSTOMER_INBOX")[0]["body"]
    assert inbox(db_path, ENGINEER_INBOX) == []


# --------------------------------------------------------- SDK wiring

def test_agent_definition():
    agent = build_tech_agent()
    assert agent.name == "Tech Support Agent" and agent.model == "gpt-4o-mini"
    assert {t.name for t in agent.tools} == {"check_meeting_availability", "book_tech_meeting",
                                             "send_tech_support_emails"}
    for t in agent.tools:
        assert "ctx" not in t.params_json_schema["properties"]


def test_agent_input_has_calendar_framing_and_no_values(db_path):
    now = sch.now_local()
    text = build_tech_agent_input("help with <PART_TOKEN_1>", VAULT, now,
                                  {"offered_slots": [slots(db_path)["free_slot"]["iso"]]})
    assert "<PART_TOKEN_1>" in text and "AB-1234" not in text
    assert sch.BUSINESS_TZ_NAME in text and "Bookable business days" in text and "1) " in text


async def test_end_to_end_runner_books_without_leaking(db_path):
    free = slots(db_path)["free_slot"]["iso"]
    model = ScriptedModel([
        ("tool", "check_meeting_availability", {"part_token": "<PART_TOKEN_1>", "requested_starts": [free]}),
        ("tool", "book_tech_meeting", {"part_token": "<PART_TOKEN_1>", "start": free}),
        ("tool", "send_tech_support_emails", {
            "customer_subject": "Your <PART_TOKEN_1> session is confirmed",
            "customer_body": "Join here: <LINK_TOKEN_1>", "sop_summary": "", "awaiting_customer": False}),
        ("text", "Booked."),
    ])
    ctx = ctx_for(db_path)
    out = await run_tech_agent(ctx, "My <PART_TOKEN_1> overheats. Can we meet?", model=model)
    assert out["final_output"] == "Booked." and ctx.thread_status == "MEETING_CONFIRMED"
    for seen in model.seen_inputs:
        assert not contains_raw_pii(seen)
        assert "axmattsupply.com" not in seen and "Alex" not in seen and "Sarah" not in seen
    assert ctx.vault["<LINK_TOKEN_1>"] in inbox(db_path, "CUSTOMER_INBOX")[0]["body"]
    assert len(inbox(db_path, ENGINEER_INBOX)) == 1
