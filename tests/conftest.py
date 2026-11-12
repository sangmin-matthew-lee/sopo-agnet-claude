import pytest

from core import db


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test_sop.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    return path


# ------------------------------------------------------------- live (Resend) mode


class FakeResend:
    """Stands in for api.resend.com. Records every request; never touches the network."""

    def __init__(self) -> None:
        self.requests: list = []
        self.received: dict[str, dict] = {}  # email_id -> GET /emails/receiving/{id} body
        self.usage: dict | None = None  # GET /usage body (None -> 404)
        self.send_status = 200
        self.quota_headers: dict[str, str] = {}

    def sent(self) -> list[dict]:
        import json

        return [json.loads(r.content) for r in self.requests if r.method == "POST" and r.url.path == "/emails"]

    def handler(self, request):
        import httpx

        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/emails":
            if self.send_status != 200:
                return httpx.Response(self.send_status, json={"message": "nope"})
            return httpx.Response(200, json={"id": f"re-{len(self.sent())}"}, headers=self.quota_headers)
        if path.startswith("/emails/receiving/"):
            body = self.received.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=body) if body else httpx.Response(404, json={})
        if path == "/usage" and self.usage is not None:
            return httpx.Response(200, json=self.usage)
        return httpx.Response(404, json={})


@pytest.fixture
def resend(db_path, monkeypatch):
    import httpx

    from core.mail import resend_api, usage

    fake = FakeResend()
    for k, v in {
        "MAIL_MODE": "resend",
        "RESEND_API_KEY": "re_test_key",
        "RESEND_WEBHOOK_SECRET": "whsec_plJ3nmyCDGBKInavdOK15jsl",
        "TEST_DOMAIN": "test.example.com",
        "TESTER_INVITE_CODE": "invite-123",
        "PUBLIC_BASE_URL": "https://sopo.example.com",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(resend_api, "TRANSPORT", httpx.MockTransport(fake.handler))
    usage.reset_cache()
    yield fake
    usage.reset_cache()
