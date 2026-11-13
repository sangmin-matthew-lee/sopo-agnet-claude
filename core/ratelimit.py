"""Per-IP sliding-window rate limits (for demo only: in memory, one process)."""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from fastapi import Request


class RateLimiter:
    """Rate limiter keyed by client IP (For demo only)."""

    def __init__(self, limit: int, window: int) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def reset(self) -> None:
        self.hits.clear()


def client_ip(request: Request) -> str:
    # Trust exactly one proxy (Cloud Run's front end), which APPENDS the real client IP.
    # Earlier entries are whatever the client sent, so they can't be used as the key.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "30"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))

rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)  # sandbox API
register_limiter = RateLimiter(5, 60)  # tester sign-up: invite-code guessing
tester_limiter = RateLimiter(60, 60)  # tester dashboard (polls every 10 s)
