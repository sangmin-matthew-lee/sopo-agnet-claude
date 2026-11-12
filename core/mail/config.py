"""Live (real email) mode settings, read from the environment on every call so tests can change them."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class LiveConfig:
    mail_mode: str  # simulated (live receiving off) | resend (live receiving on)
    api_key: str
    webhook_secret: str
    test_domain: str
    support_address: str
    am_address: str
    eng_address: str
    invite_code: str
    max_testers: int
    daily_limit: int
    monthly_limit: int
    public_base_url: str
    require_sender_auth: bool

    @property
    def live_enabled(self) -> bool:
        return self.mail_mode == "resend"

    @property
    def agent_address(self) -> str:
        """From-address for mail to the AM / engineer inboxes (not support@, so it can never loop back in)."""
        return f"sopo-agent@{self.test_domain}"

    @classmethod
    def from_env(cls) -> "LiveConfig":
        env = os.environ.get
        domain = env("TEST_DOMAIN", "").strip().lower()
        return cls(
            mail_mode=env("MAIL_MODE", "simulated").strip().lower(),
            api_key=env("RESEND_API_KEY", ""),
            webhook_secret=env("RESEND_WEBHOOK_SECRET", ""),
            test_domain=domain,
            support_address=env("SUPPORT_ADDRESS", f"support@{domain}").strip().lower(),
            am_address=env("AM_ADDRESS", f"am@{domain}").strip().lower(),
            eng_address=env("ENG_ADDRESS", f"eng@{domain}").strip().lower(),
            invite_code=env("TESTER_INVITE_CODE", ""),
            max_testers=_int("MAX_TESTERS", 10),
            daily_limit=_int("RESEND_DAILY_LIMIT", 100),
            monthly_limit=_int("RESEND_MONTHLY_LIMIT", 3000),
            public_base_url=env("PUBLIC_BASE_URL", "").rstrip("/"),
            require_sender_auth=env("REQUIRE_SENDER_AUTH", "true").strip().lower() != "false",
        )
