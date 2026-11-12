"""Resend webhook signature check. Resend signs with Svix:
https://resend.com/docs/dashboard/webhooks/verify-webhooks-requests
https://docs.svix.com/receiving/verifying-payloads/how-manual

signed content = "{svix-id}.{svix-timestamp}.{raw body}", key = base64-decoded part after "whsec_",
signature = base64(HMAC-SHA256); the svix-signature header holds space-separated "v1,<sig>" entries.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

TOLERANCE_SECONDS = 300  # reject replays older (or newer) than 5 minutes


def sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    content = f"{msg_id}.{timestamp}.".encode() + body
    return base64.b64encode(hmac.new(key, content, hashlib.sha256).digest()).decode()


def verify(
    secret: str,
    msg_id: str | None,
    timestamp: str | None,
    signature_header: str | None,
    body: bytes,
    now: float | None = None,
) -> bool:
    if not (secret and msg_id and timestamp and signature_header):
        return False
    try:
        ts = int(timestamp)
        expected = sign(secret, msg_id, timestamp, body)
    except (ValueError, TypeError):
        return False  # bad timestamp or a secret that isn't base64
    if abs((now if now is not None else time.time()) - ts) > TOLERANCE_SECONDS:
        return False
    for entry in signature_header.split():
        version, _, sig = entry.partition(",")
        if version == "v1" and hmac.compare_digest(sig.encode(), expected.encode()):
            return True
    return False
