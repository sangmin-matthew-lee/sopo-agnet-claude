from datetime import datetime, timedelta

import pytest

from core import scheduling as sch

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=sch.BUSINESS_TZ)  # a Friday


def test_business_days_skip_weekend_and_today():
    days = sch.business_days(NOW)
    assert len(days) == sch.HORIZON_BUSINESS_DAYS
    assert days[0].isoformat() == "2026-11-09"  # Monday, not Friday the 6th
    assert all(d.weekday() < 5 for d in days)


def test_candidate_slots_are_in_business_hours():
    slots = sch.candidate_slots(NOW)
    assert len(slots) == sch.HORIZON_BUSINESS_DAYS * 16
    assert slots[0] == sch.at(sch.business_days(NOW)[0], 9)
    assert all(sch.DAY_START <= s.time() and (s + sch.MEETING).time() <= sch.DAY_END for s in slots)


def test_is_free_overlap_rules():
    day = sch.business_days(NOW)[0]
    busy = [(sch.at(day, 10), sch.at(day, 11))]
    assert sch.is_free(busy, sch.at(day, 9, 30))       # ends exactly when busy starts
    assert sch.is_free(busy, sch.at(day, 11))          # starts exactly when busy ends
    assert not sch.is_free(busy, sch.at(day, 10, 30))


def test_common_free_slots_intersects_and_spreads_days():
    d0, d1 = sch.business_days(NOW)[:2]
    eng = [(sch.at(d0, 9), sch.at(d0, 12))]
    am = [(sch.at(d0, 12), sch.at(d0, 17))]  # day 0 fully blocked between the two calendars
    slots = sch.common_free_slots([eng, am], NOW, limit=3)
    assert len(slots) == 3 and all(s.date() != d0 for s in slots)
    assert slots[0] == sch.at(d1, 9)
    assert len({s.date() for s in slots}) == 3


def test_common_free_slots_ranked_near_request():
    d2 = sch.business_days(NOW)[2]
    near = sch.at(d2, 14)
    busy = [(sch.at(d2, 14), sch.at(d2, 15))]
    slots = sch.common_free_slots([busy], NOW, near=[near], limit=1)
    assert slots[0] in (sch.at(d2, 13, 30), sch.at(d2, 15))


def test_common_free_slots_exclude():
    first = sch.common_free_slots([[]], NOW, limit=1)
    again = sch.common_free_slots([[]], NOW, limit=1, exclude=first)
    assert again[0] != first[0]


def test_parse_slot_valid_and_naive_is_local():
    s = sch.parse_slot("2026-11-10T14:00", NOW)
    assert s.tzinfo is not None and sch.iso_local(s) == "2026-11-10T14:00"


@pytest.mark.parametrize("value,msg", [
    ("next tuesday", "not an ISO"),
    ("2026-11-06T14:00", "not within"),   # today: no same-day bookings
    ("2026-11-07T14:00", "not within"),   # Saturday
    ("2026-11-10T14:15", "half hour"),
    ("2026-11-10T08:30", "business hours"),
    ("2026-11-10T16:45", "half hour"),
    ("2026-11-10T17:00", "business hours"),
    ((NOW + timedelta(days=40)).strftime("%Y-%m-%dT10:00"), "not within"),
])
def test_parse_slot_rejects(value, msg):
    with pytest.raises(sch.SlotError, match=msg):
        sch.parse_slot(value, NOW)
