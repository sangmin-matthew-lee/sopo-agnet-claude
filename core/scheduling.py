"""Scheduling helpers for the Tech Support Agent.

Meetings are 30 minutes, on business days, 09:00–17:00 in BUSINESS_TZ, starting no earlier than the next business day and no later than HORIZON_BUSINESS_DAYS business days out.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

BUSINESS_TZ_NAME = os.environ.get("BUSINESS_TZ", "America/Los_Angeles")
BUSINESS_TZ = ZoneInfo(BUSINESS_TZ_NAME)

MEETING = timedelta(minutes=30)
DAY_START = time(9, 0)
DAY_END = time(17, 0)
HORIZON_BUSINESS_DAYS = 10

Interval = tuple[datetime, datetime]


def now_local() -> datetime:
    return datetime.now(BUSINESS_TZ)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BUSINESS_TZ)
    return dt.astimezone(BUSINESS_TZ)


def to_utc_iso(dt: datetime) -> str:
    return to_local(dt).astimezone(timezone.utc).isoformat(timespec="seconds")


def iso_local(dt: datetime) -> str:
    """Compact local form used with the LLM, e.g. 2026-11-12T14:00."""
    return to_local(dt).strftime("%Y-%m-%dT%H:%M")


def human(dt: datetime) -> str:
    """e.g. 'Thu Nov 12, 2:00 PM PST'."""
    d = to_local(dt)
    return (
        d.strftime("%a %b %d, ")
        + d.strftime("%I:%M %p").lstrip("0")
        + " "
        + d.strftime("%Z")
    )


def business_days(now: datetime, count: int = HORIZON_BUSINESS_DAYS) -> list[date]:
    """The next `count` business days, starting the business day after `now` (1-day lead time)."""
    days, d = [], to_local(now).date()
    while len(days) < count:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return days


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=BUSINESS_TZ)


def candidate_slots(now: datetime) -> list[datetime]:
    slots = []
    for day in business_days(now):
        t = at(day, DAY_START.hour, DAY_START.minute)
        end = at(day, DAY_END.hour, DAY_END.minute)
        while t + MEETING <= end:
            slots.append(t)
            t += MEETING
    return slots


def is_free(busy: list[Interval], start: datetime, end: datetime | None = None) -> bool:
    end = end or start + MEETING
    return all(not (start < b_end and b_start < end) for b_start, b_end in busy)


def common_free_slots(
    busy_lists: list[list[Interval]],
    now: datetime,
    near: list[datetime] | None = None,
    limit: int = 3,
    exclude: list[datetime] | None = None,
) -> list[datetime]:
    """Slots free for everyone, ranked by closeness to `near` (else soonest), one per day first."""
    excluded = {to_local(e) for e in exclude or []}
    free = [
        s
        for s in candidate_slots(now)
        if s not in excluded and all(is_free(b, s) for b in busy_lists)
    ]
    if near:
        free.sort(
            key=lambda s: (min(abs((s - to_local(n)).total_seconds()) for n in near), s)
        )
    picked: list[datetime] = []
    days_used: set[date] = set()
    for s in free:  # first pass: spread across different days
        if s.date() not in days_used:
            picked.append(s)
            days_used.add(s.date())
        if len(picked) == limit:
            break
    for s in free:  # second pass: fill up if there were too few days
        if len(picked) == limit:
            break
        if s not in picked:
            picked.append(s)
    return sorted(picked)


class SlotError(ValueError):
    """A requested time that can't be a meeting slot. The message is safe to show the LLM."""


def parse_slot(value: str, now: datetime) -> datetime:
    try:
        dt = to_local(datetime.fromisoformat((value or "").strip()))
    except ValueError:
        raise SlotError(
            f"'{value}' is not an ISO datetime like 2026-11-12T14:00"
        ) from None
    days = business_days(now)
    if dt.date() not in days:
        raise SlotError(
            f"{iso_local(dt)} is not within the next {HORIZON_BUSINESS_DAYS} business days "
            f"({days[0].isoformat()} to {days[-1].isoformat()})"
        )
    if dt.minute % 30 or dt.second:
        raise SlotError(f"{iso_local(dt)} must start on the hour or half hour")
    if not (
        DAY_START <= dt.time()
        and (dt + MEETING).time() <= DAY_END
        and dt.date() == (dt + MEETING).date()
    ):
        raise SlotError(
            f"{iso_local(dt)} is outside business hours "
            f"({DAY_START:%H:%M}-{DAY_END:%H:%M} {BUSINESS_TZ_NAME})"
        )
    return dt
