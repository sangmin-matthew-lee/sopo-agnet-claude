from __future__ import annotations

import hmac
import json
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from core import db
from pipeline import process_email, process_email_stream
from sop_agents.tech_agent import demo_slots

STATIC_DIR = Path(__file__).parent / "static"

RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "30"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="SOP Return & Tech Support Orchestrator", lifespan=lifespan)


# -----------security-----------


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


rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


def _client_ip(request: Request) -> str:
    # Trust exactly one proxy (Cloud Run's front end), which APPENDS the real client IP.
    # Earlier entries are whatever the client sent, so they can't be used as the key.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def require_passcode(
    request: Request, x_demo_passcode: str | None = Header(default=None)
) -> None:
    expected = os.environ.get("DEMO_ACCESS_CODE")
    if not expected:
        raise HTTPException(503, "Server misconfigured: DEMO_ACCESS_CODE is not set.")
    # Count every attempt, wrong passcodes included, so passcode guessing is throttled too.
    if not rate_limiter.allow(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded. Please slow down.")
    if not x_demo_passcode or not hmac.compare_digest(
        x_demo_passcode.encode(), expected.encode()
    ):
        raise HTTPException(401, "Invalid or missing Demo-Passcode.")


# -----------endpoints-----------


class SimulatedEmail(BaseModel):
    sender_email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    thread_id: str = Field(min_length=1, max_length=100)
    message_id: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=10_000)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, Any]:
    return {"ok": True}


@app.post("/api/simulate-email", dependencies=[Depends(require_passcode)])
async def simulate_email(email: SimulatedEmail) -> dict[str, Any]:
    return await process_email(
        email.sender_email, email.thread_id, email.message_id, email.body
    )


@app.post("/api/simulate-email/stream", dependencies=[Depends(require_passcode)])
async def simulate_email_stream(email: SimulatedEmail) -> StreamingResponse:
    """Streamed as server-sent events(sse) while it runs."""

    async def sse():
        async for event in process_email_stream(
            email.sender_email, email.thread_id, email.message_id, email.body
        ):
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/reset-db", dependencies=[Depends(require_passcode)])
async def reset_db() -> dict[str, Any]:
    db.reset_db()
    return {"ok": True}


@app.get("/api/state", dependencies=[Depends(require_passcode)])
async def state() -> dict[str, Any]:
    return {
        "orders": db.list_orders(),
        "emails": db.list_emails(),
        "threads": db.list_threads(),
        "staff": db.list_staff(),
        "calendar_events": db.list_calendar_events(),
        "tech_schedules": db.list_tech_schedules(),
        "tech_demo": demo_slots(),
        "config": {
            "jev_live": bool(os.environ.get("TYPESAFE_API_KEY")),
            "openai_live": bool(os.environ.get("OPENAI_API_KEY")),
        },
    }
