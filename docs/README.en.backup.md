# sopo-agnet-claude

Agent for Sales Operation(SOP) Dept. Built using Claude Code

SOP Orchestrator: FastAPI + SQLite + TypeSafe Jev (System 1 gatekeeper) + OpenAI Agents SDK
(System 2 specialists: a **Return Agent** and a **Tech Support Agent**), with a 3-column live sandbox UI.
Order IDs, part numbers, shipping labels and meeting links are masked into tokens (`<ORDER_TOKEN_1>`,
`<PART_TOKEN_1>`, `<LABEL_TOKEN_1>`, `<LINK_TOKEN_1>`) locally and are never sent to any AI provider.
See `CLAUDE.md` for the full spec.

## Prerequisites

- [uv](https://docs.astral.sh/uv/). It installs Python 3.12 (pinned in `.python-version`) and all dependencies.
- A TypeSafe AI key (Jev) and an OpenAI key.

## Run locally

**1. Install dependencies**

```bash
uv sync
```

**2. Create `.env`**

```bash
cp .env.example .env
```

Then fill it in:

```dotenv
DEMO_ACCESS_CODE=pick-any-passcode
TYPESAFE_API_KEY=your-typesafe-key
OPENAI_API_KEY=sk-...
```

`DEMO_ACCESS_CODE` is required. Without it, every API request is rejected with `503`.

**3. Start the server, loading `.env`**

```bash
uv run --env-file .env uvicorn main:app --reload --port 8080
```

The app does not read
`.env` by itself, so a plain `uv run uvicorn ...` starts without your keys.

**4. Open the sandbox**

```
http://localhost:8080/?code=pick-any-passcode
```

`?code=` fills in the passcode box automatically (it's also remembered in the browser). The top bar shows
**Jev: live** and **OpenAI: live** when both keys were loaded.

## Using the sandbox

1. Click a scenario preset in the top bar. It fills in column ① with a sender, a new `thread_id` and an email.
2. Click **Send**. Column ② streams each phase live as it runs: raw vs. masked text, the local
   vault, Jev's scores, the routing decision, and the agent's tool calls. You can watch the agent write its
   email token by token. Click **⏩ Skip** to stop the typing animation.
3. Column ③ shows the resulting emails in `CUSTOMER_INBOX` / `SOP_EMPLOYEE_INBOX`, plus the `orders` and
   `thread_vaults` tables.

| Preset                            | Expected result                                                                                                                             |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 · Happy Path (Messy Regex)      | `RETURN_LABEL_ISSUED`: label `UPS-RMA-ORD-9988` emailed to the customer                                                                     |
| 2 · Missing Order ID (Multi-Turn) | `AWAITING_CUSTOMER_INFO`. Then click **↩ Reply in Thread** on the customer email, add e.g. `It's ord 9988`, and send: `RETURN_LABEL_ISSUED` |
| 3 · Spoofed Sender Attack         | `ESCALATED_TO_HUMAN`: security alert to SOP, generic "under review" reply, no label                                                         |
| 4 · Angry Customer Escalation     | `ESCALATED_TO_HUMAN` straight from Jev; OpenAI is never called                                                                              |
| 5 · Spam / Prompt Injection       | `BLOCKED_SPAM`; no emails, OpenAI is never called                                                                                           |

### Tech Support tab

Switch to the **🛠 Tech Support** tab for the meeting-scheduling presets. Both tabs post to the same
endpoint (Jev decides which agent runs); the tab only changes the presets and the column ③ panels
(`ENGINEER_INBOX`, a calendar view of engineers and account managers, and `tech_schedules`).

The Tech Support Agent books a 30-minute call between the customer, an engineer who knows the part, and
the customer's account manager. Calendars are the source of truth: the agent only books open slots, and
the engineer and account manager each get **exactly one invite**. The engineer is never asked about times.
The sender must have bought the part. After 3 scheduling emails without agreement, the thread goes to a human.
The agent never picks a time for the customer: it only books a time the customer asked for (or chose from
the offered options) that both calendars confirm. A booked meeting can be **rescheduled**, by replying in the
thread ("that time doesn't work, how about …") or from a brand-new email: the agent finds the customer's
upcoming meeting, moves it (same link) and sends the engineer and AM one updated invite. The old meeting
stays until a new time is confirmed. A meeting that already happened no longer blocks a new booking.

| Preset                          | Expected result                                                                                                              |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 1 · Happy Path (Slot Open)      | `MEETING_CONFIRMED`: customer gets the time + meeting link; one invite each in `ENGINEER_INBOX` and to the AM                 |
| 2 · No Times Given (Multi-Turn) | `AWAITING_CUSTOMER_SCHEDULE` with 3 open slots. **↩ Reply in Thread** with e.g. `Option 2 works for us.` → `MEETING_CONFIRMED` |
| 3 · Engineer Busy               | `mary@` asks about `EF-9012` when its engineer is busy (her AM is free) → 3 alternatives offered (`AWAITING_CUSTOMER_SCHEDULE`) |
| 4 · Missing Part Number         | Agent asks for the part number. Reply with the part and a time (the hint shows one) → `MEETING_CONFIRMED`                     |
| 5 · Not the Owner               | `mary@` never bought `AB-1234` → `ESCALATED_TO_HUMAN`, security alert to SOP, nothing booked, engineer not contacted          |
| 6 · Reschedule (New Email)      | Run preset 1 first. A new thread asks to move the call → the existing meeting is moved (no duplicate), `[Updated invite]`     |

Preset dates come from the seeded calendars, which are generated relative to the day the DB was seeded.
**Reset DB** if the demo has been running for a few days.

Click **Reset DB** to restore the seed orders and clear threads, emails and agent history. Once a label is
issued for `ORD-9988`, later requests reuse it, so reset between demos for a clean slate.

Seed orders:

| Order      | Part      | Owner                     | Status           | Delivered                               |
| ---------- | --------- | ------------------------- | ---------------- | --------------------------------------- |
| `ORD-9988` | `AB-1234` | `john@acmecorp.com`       | DELIVERED, qty 2 | 10 days ago (eligible)                  |
| `ORD-1111` | `CD-5678` | `john@acmecorp.com`       | DELIVERED, qty 1 | 45 days ago (outside the 30-day window) |
| `ORD-7777` | `EF-9012` | `mary@betaindustries.com` | SHIPPED, qty 5   | not yet delivered (ineligible)          |

Each order's `sop_owner_email` is the customer's account manager (`sarah.kim@` for John, `david.park@` for
Mary). Escalations and security alerts go to the shared `sop-team@axmattsupply.com` queue.

Seed staff for tech support: engineers `alex.chen@` (AB-1234, first choice), `priya.patel@` (AB-1234
backup, CD-5678) and `marco.rossi@` (EF-9012), with busy blocks over the next 10 business days.

## Environment variables

| Variable                    | Required             | Default       | Purpose                                               |
| --------------------------- | -------------------- | ------------- | ----------------------------------------------------- |
| `DEMO_ACCESS_CODE`          | yes                  | none          | Passcode checked against the `X-Demo-Passcode` header |
| `TYPESAFE_API_KEY`          | for live routing     | none          | Jev gatekeeper                                        |
| `OPENAI_API_KEY`            | for the Return Agent | none          | `gpt-4o-mini` Return Agent                            |
| `SOP_DB_PATH`               | no                   | `sop_demo.db` | SQLite file (app tables + agent sessions)             |
| `JEV_MODEL`                 | no                   | `jev-latest`  | Jev model ID                                          |
| `RETURN_AGENT_MODEL`        | no                   | `gpt-4o-mini` | Return Agent and Tech Support Agent model             |
| `BUSINESS_TZ`               | no                   | `America/Los_Angeles` | Timezone for business hours and meeting slots |
| `RATE_LIMIT_REQUESTS`       | no                   | `30`          | Requests allowed per IP per window                    |
| `RATE_LIMIT_WINDOW_SECONDS` | no                   | `60`          | Rate-limit window                                     |

**Fail-closed behavior:** if `TYPESAFE_API_KEY` or `OPENAI_API_KEY` is missing, or either API errors, no
message is auto-processed. It's escalated to the SOP human inbox instead, and the top bar shows
"no key → fail-closed".

## API

All `/api/*` endpoints require the `X-Demo-Passcode` header.

| Method | Path                         | Description                                                                                                                          |
| ------ | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `GET`  | `/`                          | Sandbox UI                                                                                                                           |
| `GET`  | `/healthz`                   | Health check (no passcode)                                                                                                           |
| `POST` | `/api/simulate-email`        | Body: `{sender_email, thread_id, message_id, body}`. Returns the final status and a step-by-step trace                               |
| `POST` | `/api/simulate-email/stream` | Same body and pipeline, streamed live as Server-Sent Events (`phase_start`, `step`, `agent_log`, `agent_delta`, then `done`/`error`) |
| `POST` | `/api/reset-db`              | Drop and reseed all tables                                                                                                           |
| `GET`  | `/api/state`                 | Orders, emails, thread vaults, staff, calendars, tech schedules, preset slots, and which API keys are loaded                         |

Example:

```bash
curl -X POST localhost:8080/api/simulate-email \
  -H "X-Demo-Passcode: pick-any-passcode" -H "Content-Type: application/json" \
  -d '{"sender_email":"john@acmecorp.com","thread_id":"t1","message_id":"m1","body":"Return ab - 1234 from ord/9988"}'
```

Reusing a `message_id` returns `DUPLICATE_IGNORED`.

## Test

```bash
uv run pytest
```

The 178 tests run fully offline and need no API keys. Jev is stubbed with `httpx.MockTransport`/monkeypatch,
and both agents run through the real `Runner` + `SQLiteSession` with a scripted fake model
(`tests/fake_model.py`).

To exercise the live APIs, start the server as above and run the presets on both tabs in the UI (or with `curl`).

## Docker

```bash
docker build -t sop-orchestrator .
docker run --rm -p 8080:8080 --env-file .env sop-orchestrator
```

## Deploy (Cloud Run)

```bash
gcloud run deploy sop-orchestrator --source . --port 8080 --max-instances 1 \
  --set-env-vars DEMO_ACCESS_CODE=... \
  --set-secrets TYPESAFE_API_KEY=typesafe-api-key:latest,OPENAI_API_KEY=openai-api-key:latest
```

This stores the API keys in Secret Manager (create the `typesafe-api-key` and `openai-api-key` secrets
first) rather than in plain environment variables. The container keeps SQLite at `/tmp/sop_demo.db`, which
is ephemeral and per instance, and the rate limiter is in-memory. `--max-instances 1` keeps the demo state
consistent.

## Layout

| File                | Role                                                                                        |
| ------------------- | ------------------------------------------------------------------------------------------- |
| `db.py`             | Schema, seed data, reset, query helpers                                                     |
| `masker.py`         | Phase 1: preprocessing + "Catch Messy, Store Clean" token vault                             |
| `jev_router.py`     | Phase 2: Jev client + deterministic Path A/B/C router (incl. multi-turn continuation)       |
| `agent.py`          | Phase 3/4: Return Agent, anti-spoofing, eligibility, label idempotency, de-masking dispatch |
| `tech_agent.py`     | Phase 3/4: Tech Support Agent: ownership check, calendar check, booking + one invite each  |
| `scheduling.py`     | Pure slot math: business hours, free/busy intersection, slot validation                    |
| `pipeline.py`       | Orchestrates the 4 phases and builds the X-Ray trace                                        |
| `main.py`           | FastAPI endpoints, passcode check, rate limiting                                            |
| `static/index.html` | 3-column sandbox UI with Returns / Tech Support tabs                                        |
| `tests/`            | Offline test suite                                                                          |

## Troubleshooting

| Symptom                                                    | Cause                                                                                 |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `503: DEMO_ACCESS_CODE is not set`                         | `.env` has no `DEMO_ACCESS_CODE`, or the server wasn't started with `--env-file .env` |
| `401: Invalid or missing X-Demo-Passcode`                  | The passcode box doesn't match `DEMO_ACCESS_CODE`                                     |
| `429: Rate limit exceeded`                                 | More than 30 requests in 60 s from one IP. Wait, or raise `RATE_LIMIT_REQUESTS`       |
| Every scenario ends in `ESCALATED_TO_HUMAN`                | Keys weren't loaded (check the top-bar badges). Restart with `--env-file .env`        |
| Happy path says the label "already existed and was reused" | A label was issued earlier. Click **Reset DB**                                        |
