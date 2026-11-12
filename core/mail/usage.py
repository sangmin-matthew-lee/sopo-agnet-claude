"""Resend quota tracking. On the free plan, sent AND received emails share one quota (100/day, 3,000/month).

The daily quota is a UTC calendar day: it resets at 00:00 UTC (https://resend.com/docs/api-reference/rate-limit).
Remaining = the latest real number we saw from Resend (GET /usage, or the x-resend-*-quota headers of a send),
plus what we counted locally since then. With no real number yet, the local count alone is used.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core import db
from core.mail import resend_api
from core.mail.config import LiveConfig

log = logging.getLogger("sopo.mail")

SENT, RECEIVED = "sent", "received"
DEFAULT_PER_SCENARIO = 6.0  # no data yet: a return takes ~5 emails, a tech meeting ~7
LOW_WARNING = 20
API_REFRESH_SECONDS = 60


@dataclass
class _Observation:
    at: datetime  # when Resend told us
    daily_used: int | None
    monthly_used: int | None
    daily_limit: int | None = None
    monthly_limit: int | None = None
    source: str = "resend_api"


_observed: _Observation | None = None
_last_api_fetch = 0.0


def reset_cache() -> None:
    global _observed, _last_api_fetch
    _observed, _last_api_fetch = None, 0.0


def record(direction: str, thread_id: str | None = None, db_path: str | None = None) -> int:
    with db.get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO email_usage (direction, thread_id, created_at) VALUES (?,?,?)",
            (direction, thread_id, db.now_iso()),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid


def attach(usage_id: int, thread_id: str, db_path: str | None = None) -> None:
    """A received email is counted on arrival; its thread is known only after it's been read."""
    with db.get_conn(db_path) as conn:
        conn.execute("UPDATE email_usage SET thread_id = ? WHERE id = ?", (thread_id, usage_id))


def note_send_headers(quota: dict[str, int]) -> None:
    """Real usage from a send response's x-resend-daily-quota / x-resend-monthly-quota headers."""
    global _observed
    if quota:
        prev = _observed
        _observed = _Observation(
            at=_now(),
            daily_used=quota.get("daily_used"),
            monthly_used=quota.get("monthly_used"),
            daily_limit=prev.daily_limit if prev else None,
            monthly_limit=prev.monthly_limit if prev else None,
            source="resend_headers",
        )


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _refresh_from_api(cfg: LiveConfig) -> None:
    global _observed, _last_api_fetch
    if not (cfg.live_enabled and cfg.api_key) or time.monotonic() - _last_api_fetch < API_REFRESH_SECONDS:
        return
    _last_api_fetch = time.monotonic()
    try:
        emails = resend_api.get_usage().get("emails", {})
    except resend_api.ResendError as exc:
        log.warning("Resend usage lookup failed: %s", exc)
        return
    daily, monthly = emails.get("daily") or {}, emails.get("monthly") or {}
    _observed = _Observation(
        at=_now(),
        daily_used=daily.get("used"),
        monthly_used=monthly.get("used"),
        daily_limit=daily.get("limit"),
        monthly_limit=monthly.get("limit"),
    )


def _local_count(since: datetime, db_path: str | None, after_exclusive: bool = False) -> int:
    op = ">" if after_exclusive else ">="
    with db.get_conn(db_path) as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM email_usage WHERE created_at {op} ?",
            (since.isoformat(timespec="seconds"),),
        ).fetchone()[0]


def _avg_per_scenario(db_path: str | None) -> tuple[float, int]:
    """Average emails (sent + received) per live thread, over the 20 most recent threads."""
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT COUNT(*) AS n FROM email_usage WHERE thread_id IS NOT NULL
               GROUP BY thread_id ORDER BY MAX(id) DESC LIMIT 20"""
        ).fetchall()
    if not rows:
        return DEFAULT_PER_SCENARIO, 0
    return sum(r["n"] for r in rows) / len(rows), len(rows)


def snapshot(db_path: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    cfg = LiveConfig.from_env()
    if now is None:
        _refresh_from_api(cfg)
    now = now or _now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    obs, source = _observed, "local"
    daily_used = _local_count(day_start, db_path)
    monthly_used = _local_count(month_start, db_path)
    daily_limit, monthly_limit = cfg.daily_limit, cfg.monthly_limit
    if obs is not None:
        since_obs = _local_count(obs.at, db_path, after_exclusive=True)
        if obs.daily_used is not None and obs.at >= day_start:
            daily_used, source = obs.daily_used + since_obs, obs.source
        if obs.monthly_used is not None and obs.at >= month_start:
            monthly_used, source = obs.monthly_used + since_obs, obs.source
        daily_limit = obs.daily_limit if obs.daily_limit is not None else daily_limit
        monthly_limit = obs.monthly_limit if obs.monthly_limit is not None else monthly_limit

    daily_left = max(0, daily_limit - daily_used)
    monthly_left = max(0, monthly_limit - monthly_used)
    avg, sample = _avg_per_scenario(db_path)
    left = min(daily_left, monthly_left)
    return {
        "daily_used": daily_used,
        "daily_limit": daily_limit,
        "daily_remaining": daily_left,
        "monthly_used": monthly_used,
        "monthly_limit": monthly_limit,
        "monthly_remaining": monthly_left,
        "remaining": left,
        "avg_per_scenario": round(avg, 1),
        "avg_sample_threads": sample,
        "scenarios_left": int(left // avg) if avg > 0 else 0,
        "low": left <= LOW_WARNING,
        "exhausted": left == 0,
        "source": source,  # resend_api | resend_headers | local
        "resets_at": (day_start + timedelta(days=1)).isoformat(),  # 00:00 UTC
        "live_enabled": cfg.live_enabled,
    }


def can_send(db_path: str | None = None) -> bool:
    return not snapshot(db_path)["exhausted"]
