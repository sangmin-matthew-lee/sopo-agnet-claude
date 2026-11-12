"""POST /webhook/resend — Resend's `email.received` events. No Demo-Passcode: the Svix signature is the auth."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from core.mail import usage
from core.mail.config import LiveConfig
from live import inbound
from live.signature import verify

router = APIRouter()


@router.post("/webhook/resend", include_in_schema=False)
async def resend_webhook(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    cfg = LiveConfig.from_env()
    if not cfg.live_enabled:
        raise HTTPException(503, "Live email mode is off (MAIL_MODE=simulated).")
    if not cfg.webhook_secret:
        raise HTTPException(503, "Server misconfigured: RESEND_WEBHOOK_SECRET is not set.")

    body = await request.body()
    h = request.headers
    if not verify(cfg.webhook_secret, h.get("svix-id"), h.get("svix-timestamp"), h.get("svix-signature"), body):
        raise HTTPException(401, "Invalid webhook signature.")

    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Body is not JSON.") from None
    if event.get("type") != "email.received":
        return {"ok": True, "ignored": event.get("type")}
    email_id = str((event.get("data") or {}).get("email_id") or "")
    if not email_id:
        raise HTTPException(400, "Missing data.email_id.")

    # Same event retried (svix-id) or the same email delivered twice (email_id): handle once.
    first_delivery = inbound.claim(f"svix:{h.get('svix-id')}")
    first_email = inbound.claim(f"email:{email_id}")
    if not (first_delivery and first_email):
        return {"ok": True, "duplicate": True}

    usage_id = usage.record(usage.RECEIVED)  # counts against the quota even if we end up ignoring it
    background.add_task(inbound.handle, email_id, usage_id)  # runs after the 200 has been sent
    return {"ok": True}
