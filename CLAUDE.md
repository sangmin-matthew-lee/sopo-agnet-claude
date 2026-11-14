# SOP Orchestrator Agent — System Architecture & Engineering Spec

## 1. Project Overview

Build a full-stack **Sales Operations (SOP) Return Orchestrator & 3-Column Live Sandbox Demo** using **FastAPI**, **SQLite**, **TypeSafe AI (Jev - System 1)**, **OpenAI Agents SDK (`openai-agents` - System 2)**, and a **Single-File HTML/Tailwind CSS Frontend**.

- **Core Security Rule (Zero PII/Secret Leakage):** External AI providers (Jev and OpenAI) must NEVER see raw Order IDs (e.g., `ORD-9988`), Part Numbers (e.g., `AB-1234`), or generated Shipping Labels (e.g., `UPS-RMA-ORD-9988`). All sensitive entities must be masked into tokens (`<ORDER_TOKEN_1>`, `<PART_TOKEN_1>`, `<LABEL_TOKEN_1>`) in local Python memory/SQLite before calling any external AI API, and unmasked only inside local Python tool execution.
- **Reference Syntax:** Always read `docs/sdk_reference.md` before implementing Jev or OpenAI Agents SDK code.

---

## 2. The 4-Phase Data Flow Pipeline

### Phase 1: Ingestion, Persistence & Sanitization (100% Local Python)

1. **Passcode & Rate Limit Check:** Validate `X-Demo-Passcode` header against `DEMO_ACCESS_CODE` env var. Enforce basic rate limiting.
2. **Webhook-Level Idempotency (Pillar 6A):** Check `processed_messages` table for `message_id`. If duplicate, abort immediately.
3. **Thread-Level State & Vault Loader (Pillar 3):** Check `thread_vaults` table by `thread_id`. Reload existing `vault_json` if this is a multi-turn reply; otherwise initialize a new vault.
4. **Email Preprocessor:** Strip HTML tags, reply quotes, and signatures (e.g., everything after `--` or `Best regards`).
5. **Flexible Regex PII Masker ("Catch Messy, Store Clean Policy" - Pillar 1):**
   - Match sloppy customer input for Order IDs (`ord/9988`, `ORD - 9988`, `ord9988`) and Part Numbers (`ab - 1234`, `AB/1234`, `ab1234`).
   - Replace matches in the email text with `<ORDER_TOKEN_X>` and `<PART_TOKEN_X>`.
   - **Normalize** the real values to strict uppercase hyphenated format (`ORD-9988`, `AB-1234`) before storing in `vault` (`{"<PART_TOKEN_1>": "AB-1234"}`).

### Phase 2: System 1 Gatekeeper & Router (Jev API - Pillar 2)

Evaluate the **masked email text** in a single parallel Jev API call:

- `is_spam_or_injection` (`noul`): Probability `0.0` to `1.0`.
- `intent` (`choice`): `["return", "tech_support", "refund", "human_needed", "non_sop"]`. `non_sop` means "other legitimate business" (purchase/quote requests, account questions, colleagues, other departments), not junk.
- `frustration_level` (`score`): 3-level ordered scale (`0` = calm, `1` = annoyed, `2` = furious/urgent).

**Deterministic Routing Rules in Python:**

- **Path A (Block):** Only if `is_spam_or_injection >= 0.75`: update `thread_status = "BLOCKED_SPAM"` and halt. Zero OpenAI tokens spent. Nothing else is ever dropped silently.
- **Path B (Human Escalation - Pillar 7):** If `frustration_level >= 1.5` OR `intent in ("human_needed", "refund", "non_sop")` OR `intent.confidence < 0.60` OR the sender is internal (`@axmattsupply.com`) or automated (no-reply / mailer-daemon / out-of-office / bounce), bypass OpenAI, write an unmasked alert email into `simulated_emails` (`SOP_EMPLOYEE_INBOX`) and a holding reply into `CUSTOMER_INBOX`, and set `thread_status = "ESCALATED_TO_HUMAN"`. Sender facts are checked locally: internal and automated senders get **no** holding reply (no loops, no "we received your message" to colleagues) and get no extra privileges; senders with no orders get a reply that doesn't mention an account manager, and their alert says "(no previous orders)". An auto-reply on an open thread keeps that thread's status.
- **Path C (Return Agent):** If `intent == "return"` and `confidence >= 0.60`, invoke Phase 3.

### Phase 3: System 2 Return Specialist (`openai-agents` SDK)

- Initialize `Return Agent` (`gpt-4o-mini`) with `SQLiteSession(session_id=thread_id, db_path="sop_demo.db")` and pass `SOPSharedContext` via `RunContextWrapper`.
- **Missing Info Edge Case (Pillar 3):** If the masked email (plus existing thread vault) is missing either an `<ORDER_TOKEN_X>` or `<PART_TOKEN_X>`, the Return Agent must call `send_return_emails` to ask the customer for the missing information, and set `thread_status = "AWAITING_CUSTOMER_INFO"`.
- If both tokens are present, the Return Agent calls `verify_order_and_create_label(order_token, part_token)`.

### Phase 4: Backend De-Masking, Deterministic Business Rules & Dispatch

1. **`verify_order_and_create_label` Tool:**
   - Unmask `order_token` and `part_token` using `ctx.context.vault`.
   - **3-Way Anti-Spoofing Check (Pillar 4):** Query `orders` matching `order_id`, `part_number`, AND `customer_email`. If sender email does not match order owner, reject and escalate to SOP Employee Inbox.
   - **Eligibility Engine (Pillar 5):** Verify `status` is `DELIVERED` (or `RETURN_INITIATED`), delivery was within 30 days, and requested quantity <= `purchased_qty`.
   - **DB Idempotency (Pillar 6B):** If `status == "RETURN_INITIATED"` and `return_label` already exists, reuse the existing label instead of creating a new one. Otherwise, generate `UPS-RMA-{real_order_id}`, update `orders`, store `<LABEL_TOKEN_1>` in `vault`, and return ONLY the masked confirmation string to the LLM.
2. **`send_return_emails` Tool:**
   - Receive drafted customer reply and SOP summary containing `<TOKENS>`.
   - Replace all `<TOKENS>` with real values from `ctx.context.vault`.
   - Insert final unmasked emails into `simulated_emails` table (`CUSTOMER_INBOX` and `SOP_EMPLOYEE_INBOX`).

### Phase 3B / 4B: Tech Support Agent (`sop_agents/tech_agent.py`)

- **Path C (Tech):** If `intent == "tech_support"` and `confidence >= 0.60` (and Paths A/B don't apply), invoke the `Tech Support Agent` (`gpt-4o-mini`, same `SQLiteSession(thread_id)`). Replies on a thread in `AWAITING_CUSTOMER_SCHEDULE` continue to this agent (Jev gets a static `TECH_SCHEDULING_CONTEXT`).
- **Calendars are the source of truth.** The agent reads free/busy for an engineer who knows the part (`engineer_parts`, by priority) and the client's account manager (`orders.sop_owner_email`). It never asks the engineer about times. The engineer and the AM each get **exactly one** invite, at booking time.
- Tools: `check_meeting_availability(part_token, requested_starts, reschedule)` (ownership check: sender must have an order of the part, else lock + `[SECURITY]` escalation; a sender with no orders at all is a likely prospect and gets a normal hand-off instead, no lock; returns `AVAILABLE` or up to 3 shared open slots), `book_tech_meeting(part_token, start)` (idempotent per thread; writes `MEETING` events, mints `<LINK_TOKEN_N>`, sends the two invites), `send_tech_support_emails(...)` (unmasks tokens; `awaiting_customer=true` sets `AWAITING_CUSTOMER_SCHEDULE` and counts a round).
- **Customer chooses:** `book_tech_meeting` only accepts a slot that the customer requested and that `check_meeting_availability` returned `AVAILABLE` in the same run; offered slots can't be booked by the agent.
- **Rescheduling:** replies on `MEETING_CONFIRMED` threads also continue to the agent. `check_meeting_availability(..., reschedule=true)` ignores the meeting's own calendar entries and never re-offers its current time; offers set `tech_schedules.status = RESCHEDULING`. Booking then moves the meeting (same link): one `[Updated invite]` to the engineer and AM, or `[Cancelled]` + a new invite if another engineer must take over. A new email thread adopts the customer's upcoming meeting (by part, or their only one) instead of booking a duplicate. Past meetings don't count as booked. A plain confirmation ("I will be attending") on a booked meeting gets a short reply with no calendar check and no new offers; slots offered before a booking stay on record, so a late pick of one (a reply to an older email) moves the meeting there. Live email is processed one message at a time per thread.
- **Round limit:** after 3 scheduling emails, a 4th is replaced by a human escalation to the AM plus a holding reply.
- Engineer/AM names and emails, part numbers and meeting links never reach the LLM. Dates/times are not PII; slots are 30 min, business days 09:00–17:00 `BUSINESS_TZ`, from the next business day up to 10 business days out.

---

## 3. SQLite Database Schema (`sop_demo.db`)

Create and seed these tables on startup (and provide a `/api/reset-db` endpoint):

1. `orders`: `order_id TEXT`, `part_number TEXT`, `customer_email TEXT NOT NULL`, `sop_owner_email TEXT NOT NULL`, `purchased_qty INTEGER NOT NULL`, `status TEXT NOT NULL`, `delivered_at TEXT NOT NULL`, `return_label TEXT`, `PRIMARY KEY (order_id, part_number)`
2. `thread_vaults`: `thread_id TEXT PRIMARY KEY`, `customer_email TEXT NOT NULL`, `vault_json TEXT NOT NULL`, `thread_status TEXT NOT NULL`, `updated_at TEXT NOT NULL`
3. `processed_messages`: `message_id TEXT PRIMARY KEY`, `thread_id TEXT NOT NULL`, `received_at TEXT NOT NULL`
4. `simulated_emails`: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `thread_id TEXT NOT NULL`, `inbox_type TEXT NOT NULL`, `recipient_email TEXT NOT NULL`, `subject TEXT NOT NULL`, `body TEXT NOT NULL`, `created_at TEXT NOT NULL` (`inbox_type` ∈ `CUSTOMER_INBOX`, `SOP_EMPLOYEE_INBOX`, `ENGINEER_INBOX`)
5. Tech support: `staff(email, name, role)`, `engineer_parts(engineer_email, part_number, priority)`, `calendar_events(id, owner_email, start_at, end_at, title, kind BUSY|MEETING, thread_id)` (UTC ISO), `tech_schedules(thread_id PK, customer_email, part_number, am_email, engineer_email, rounds, offered_slots_json, meeting_start, meeting_link, issue_text, status, updated_at)`

---

## 4. 3-Column All-in-One Web Sandbox UI (`index.html` served by FastAPI)

- **Top Bar:** Passcode input (auto-filled if `?code=` query param is present), DB Reset button, and **5 One-Click Scenario Preset Buttons**:
  1. _Happy Path (Messy Regex)_: `john@acmecorp.com` returning `ab - 1234` from `ord/9988`.
  2. _Missing Order ID (Multi-Turn)_: `john@acmecorp.com` asking to return `AB-1234` without an Order ID, allowing a follow-up reply in the same `thread_id`.
  3. _Spoofed Sender Attack_: `hacker@gmail.com` trying to return `ORD-9988` / `AB-1234`.
  4. _Angry Customer Escalation_: Furious message triggering Jev score escalation straight to SOP inbox.
  5. _Spam / Prompt Injection_: Crypto spam blocked by Jev at Phase 2 with $0 OpenAI cost.
- **Column 1 (Customer Simulator):** Sender email, `thread_id`, `message_id`, email body textarea, and "Send to FastAPI" button.
- **Column 2 (Backend & AI X-Ray):** Live step-by-step trace showing Raw vs. Masked text, Local Vault JSON, Jev probabilities/confidence gauges, and OpenAI Agent tool calls.
- **Tabs:** `📦 Returns` and `🛠 Tech Support`. Same endpoint; the tab switches the presets (Tech: Happy Path, No Times Given, Engineer Busy, Missing Part Number, Not the Owner, Reschedule) and the column 3 panels.
- **Column 3 (Live Inboxes & DB Inspector):** Rendered cards for `CUSTOMER_INBOX` (with a "Reply in Thread" button that keeps the same `thread_id`) and `SOP_EMPLOYEE_INBOX`, plus a live table view of the `orders` SQLite table.

---

## 5. Live Email Mode (Resend) — closed beta, coexists with the sandbox

- **Transport by thread source, not by a global switch.** `thread_vaults.source` is `simulated` (created by `/api/simulate-email*`) or `live` (created by `/webhook/resend`) and never changes. `core/mail.send(...)` picks `SimulatedTransport` (writes `simulated_emails` only) or `ResendTransport`. `MAIL_MODE=resend` only turns live **receiving** on; with `simulated` the webhook returns 503. All agent/pipeline email goes through `core/mail.send`; business rules, masking and guards are unchanged.
- **Inbound (`live/webhook.py`, `live/inbound.py`):** verify the Svix signature (401 on failure, no Demo-Passcode), dedup by `svix-id` / `email_id` / `Message-ID` (`inbound_events`), count the email in `email_usage`, answer 200, then in the background fetch `GET /emails/receiving/{id}`. `support@` → pipeline only for registered testers with DKIM/DMARC pass, never for `Auto-Submitted != no`, mailer-daemon/postmaster, or our own domain; `In-Reply-To`/`References` join the sender's own live thread (`mail_messages`), else a new `live-…` thread. `am@`/`eng@` → `internal_inbox` (thread from `X-SOPO-Thread`, trusted only from our agent address). Errors fail closed to a human escalation.
- **Outbound (`core/mail/resend.py`):** customer replies only to registered testers, threaded (`In-Reply-To`, `References`, `Re: <original subject>`, `Auto-Submitted: auto-replied`). Staff identities in the DB stay as seeded; mail for AMs/SOP goes to `AM_ADDRESS`, for engineers to `ENG_ADDRESS`, with `X-SOPO-Thread`. Every attempt is logged in `simulated_emails` (`source`, `status` SENT/BLOCKED_QUOTA/FAILED, `delivered_to`). A failed agent send raises `MailNotSent` (pipeline fails closed); escalation sends are best-effort.
- **Quota (`core/mail/usage.py`):** sent and received share the Resend quota (free: 100/day resetting 00:00 UTC, 3,000/month). Prefer Resend's `/usage` or `x-resend-*-quota` headers, fall back to local `email_usage`. At 0, nothing is sent and live threads escalate before Jev.
- **Staff view and human takeover:** live staff mail ends with the conversation so far (`threads.transcript`: customer emails from `mail_messages`, sent customer replies) and carries the thread's `In-Reply-To`/`References`; customer-facing mail is sent before the staff alert so it is quoted. `/t/<token>/threads/<id>/reply` (`AM`|`ENGINEER`, owner's live thread only) sends in the customer's thread as that person and sets `HUMAN_HANDLING` (`paused_from`, `human_roles`); customer mail on such a thread skips Jev/agents and is forwarded to them, no auto-reply. `/resume` restores `paused_from`.
- **Direct mail to am@/eng@:** a registered tester's mail there takes the customer path (same checks); replies come from the address the thread started at (`mail_messages.mailbox`), signed "SOPO AI Assistant". Non-tester mail there stays internal.
- **Sample label:** an approved return (new or reused label) attaches `core/labels.py`'s PDF to the customer email only (`ctx.label_pdf`, never in tool results). It must stay visibly fake: SAMPLE/VOID marks, no carrier logo or tracking number. Files live in `email_attachments`.
- **Testers (`live/tester.py`):** `/tester/register` needs `TESTER_INVITE_CODE`, capped by `MAX_TESTERS`, no token re-issue; `/t/<token>` (only the sha256 is stored) creates test orders and returns **only that tester's** data. `/api/reset-db` keeps live data; `/api/admin/reset-live` (passcode + `{"confirm": "DELETE ALL LIVE DATA"}`) deletes it. `/api/state` hides live data unless `include_live=true`.
