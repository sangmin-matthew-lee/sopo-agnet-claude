from __future__ import annotations

import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from core import db
from core.ratelimit import client_ip as _client_ip, rate_limiter
from pipeline import process_email, process_email_stream
from sop_agents.tech_agent import demo_slots
from core.mail import usage
from live.tester import router as tester_router
from live.webhook import router as webhook_router

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="SOP Return & Tech Support Orchestrator", lifespan=lifespan)
app.include_router(webhook_router)  # real email (Resend); signature-checked, no passcode
app.include_router(tester_router)  # beta testers: invite code, then a private /t/<token> link


# -----------security-----------


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
    """Sandbox reset. Real-email (live) testers, orders and threads are kept."""
    db.reset_db()
    return {"ok": True}


RESET_LIVE_CONFIRMATION = "DELETE ALL LIVE DATA"


class ResetLive(BaseModel):
    confirm: str = ""


@app.post("/api/admin/reset-live", dependencies=[Depends(require_passcode)])
async def reset_live(req: ResetLive) -> dict[str, Any]:
    """Delete every tester and all real-email data. Needs the exact confirmation phrase."""
    if req.confirm != RESET_LIVE_CONFIRMATION:
        raise HTTPException(400, f'Send {{"confirm": "{RESET_LIVE_CONFIRMATION}"}} to delete all live data.')
    db.reset_live()
    return {"ok": True}


@app.get("/api/state", dependencies=[Depends(require_passcode)])
async def state(include_live: bool = False) -> dict[str, Any]:
    """Sandbox data only; include_live=true (the admin toggle) adds real-email data."""
    return {
        "orders": db.list_orders(include_live=include_live),
        "emails": db.list_emails(include_live=include_live),
        "threads": db.list_threads(include_live=include_live),
        "staff": db.list_staff(),
        "calendar_events": db.list_calendar_events(include_live=include_live),
        "tech_schedules": db.list_tech_schedules(include_live=include_live),
        "tech_demo": demo_slots(),
        "usage": usage.snapshot(),
        "include_live": include_live,
        "config": {
            "jev_live": bool(os.environ.get("TYPESAFE_API_KEY")),
            "openai_live": bool(os.environ.get("OPENAI_API_KEY")),
        },
    }
