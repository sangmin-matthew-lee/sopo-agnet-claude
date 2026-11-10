# Implementation Checklist

- [x] **Milestone 1: Environment & Database Layer (`db.py`)**
  - Initialize `uv` project with `fastapi`, `uvicorn`, `openai-agents`, `pydantic`, `httpx`, and `pytest`.
  - Implement SQLite schema creation (`orders`, `thread_vaults`, `processed_messages`, `simulated_emails`) and seed initial test data (`ORD-9988` + `AB-1234` owned by `john@acmecorp.com`, delivered 10 days ago; plus an expired order `ORD-1111` delivered 45 days ago).
  - Write and pass unit tests for DB seeding and reset.

- [x] **Milestone 2: Phase 1 Preprocessor & Flexible Regex Vault (`masker.py`)**
  - Implement signature stripping and the "Catch Messy, Store Clean" Regex normalizer (`ab - 1234` -> `<PART_TOKEN_1>` mapped to `"AB-1234"` in vault; `ord/9988` -> `<ORDER_TOKEN_1>` mapped to `"ORD-9988"`).
  - Support merging new tokens into an existing thread's `vault_json` without overwriting previous tokens.
  - Write and pass `pytest` tests covering messy spaces, slashes, lowercase inputs, and multi-turn vault merging.

- [x] **Milestone 3: Phase 2 Jev Gatekeeper & Router (`jev_router.py`)**
  - Implement async client for `https://api.typesafe.ai/v1/systemone` with `is_spam_or_injection` (`noul`), `intent` (`choice`), and `frustration_level` (`score`), plus a deterministic fallback when `TYPESAFE_API_KEY` is unset.
  - Implement deterministic routing logic for Path A (Block), Path B (Human Escalation), and Path C (Return Agent).
  - Write and pass `pytest` tests verifying all 3 routing paths.

- [x] **Milestone 4: Phase 3 & 4 OpenAI Return Agent & Tools (`agent.py`)**
  - Implement `verify_order_and_create_label` and `send_return_emails` using `RunContextWrapper[SOPSharedContext]`.
  - Enforce 3-Way Anti-Spoofing, 30-day eligibility check, DB label idempotency, and token unmasking on email dispatch.
  - Wire `Return Agent` (`gpt-4o-mini`) with `SQLiteSession`.

- [x] **Milestone 5: FastAPI Server, 3-Column Sandbox UI & Docker (`main.py`, `static/index.html`, `Dockerfile`)**
  - Build FastAPI endpoints: `POST /api/simulate-email`, `POST /api/reset-db`, `GET /api/state`.
  - Enforce `X-Demo-Passcode` header check and in-memory rate limiting.
  - Build the 3-Column HTML/Tailwind CSS Sandbox UI with the 5 One-Click Scenario Preset buttons and multi-turn "Reply in Thread" capability.
  - Create `Dockerfile` ready for GCP Cloud Run (`PORT=8080`).

- [x] **Milestone 6: Tech Support Agent (`tech_agent.py`, `scheduling.py`)**
  - Route Jev `tech_support` to a second OpenAI specialist that books a 30-min call with an engineer who knows the part and the client's account manager.
  - Calendars (`calendar_events`) are the source of truth; the engineer and AM each get exactly one invite. Ownership check on part + sender; 3-round scheduling limit before human escalation; `<LINK_TOKEN_N>` masking for meeting links.
  - Add the **Tech Support** tab in the sandbox (5 presets, `ENGINEER_INBOX`, calendar view, `tech_schedules`).
