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
- `intent` (`choice`): `["return", "tech_support", "refund", "human_needed", "non_sop"]`.
- `frustration_level` (`score`): 3-level ordered scale (`0` = calm, `1` = annoyed, `2` = furious/urgent).

**Deterministic Routing Rules in Python:**

- **Path A (Block):** If `is_spam_or_injection >= 0.75` OR `intent == "non_sop"`, update `thread_status = "BLOCKED_SPAM"` and halt. Zero OpenAI tokens spent.
- **Path B (Human Escalation - Pillar 7):** If `frustration_level >= 1.5` OR `intent == "human_needed"` OR `intent.confidence < 0.60`, bypass OpenAI, write an unmasked alert email into `simulated_emails` (`SOP_EMPLOYEE_INBOX`) and a holding reply into `CUSTOMER_INBOX`, and set `thread_status = "ESCALATED_TO_HUMAN"`.
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

---

## 3. SQLite Database Schema (`sop_demo.db`)

Create and seed these 4 tables on startup (and provide a `/api/reset-db` endpoint):

1. `orders`: `order_id TEXT`, `part_number TEXT`, `customer_email TEXT NOT NULL`, `sop_owner_email TEXT NOT NULL`, `purchased_qty INTEGER NOT NULL`, `status TEXT NOT NULL`, `delivered_at TEXT NOT NULL`, `return_label TEXT`, `PRIMARY KEY (order_id, part_number)`
2. `thread_vaults`: `thread_id TEXT PRIMARY KEY`, `customer_email TEXT NOT NULL`, `vault_json TEXT NOT NULL`, `thread_status TEXT NOT NULL`, `updated_at TEXT NOT NULL`
3. `processed_messages`: `message_id TEXT PRIMARY KEY`, `thread_id TEXT NOT NULL`, `received_at TEXT NOT NULL`
4. `simulated_emails`: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `thread_id TEXT NOT NULL`, `inbox_type TEXT NOT NULL`, `recipient_email TEXT NOT NULL`, `subject TEXT NOT NULL`, `body TEXT NOT NULL`, `created_at TEXT NOT NULL`

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
- **Column 3 (Live Inboxes & DB Inspector):** Rendered cards for `CUSTOMER_INBOX` (with a "Reply in Thread" button that keeps the same `thread_id`) and `SOP_EMPLOYEE_INBOX`, plus a live table view of the `orders` SQLite table.
