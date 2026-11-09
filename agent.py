"""Phase 3 & 4: OpenAI Return Specialist agent + local de-masking tools.

The LLM only ever sees tokens (<ORDER_TOKEN_1>, <PART_TOKEN_1>, <LABEL_TOKEN_1>).
Real values live in SOPSharedContext.vault and are resolved only inside these local tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agents import Agent, ModelSettings, RunConfig, RunContextWrapper, Runner, SQLiteSession, function_tool
from agents.models.interface import Model

import db
from masker import TOKEN_RE, add_to_vault, tokens_of, unmask_text

RETURN_WINDOW_DAYS = 30
RETURN_AGENT_MODEL = os.environ.get("RETURN_AGENT_MODEL", "gpt-4o-mini")

CUSTOMER_INBOX = "CUSTOMER_INBOX"
SOP_EMPLOYEE_INBOX = "SOP_EMPLOYEE_INBOX"

# Thread statuses set by tools.
AWAITING_CUSTOMER_INFO = "AWAITING_CUSTOMER_INFO"
RETURN_LABEL_ISSUED = "RETURN_LABEL_ISSUED"
RETURN_DENIED = "RETURN_DENIED"
ESCALATED_TO_HUMAN = "ESCALATED_TO_HUMAN"
SPOOF_ESCALATED = "ESCALATED_TO_HUMAN"  # spoofing is escalated to SOP like any human case


@dataclass
class SOPSharedContext:
    """Local-only state. Never serialized to the LLM."""

    thread_id: str
    customer_email: str
    vault: dict[str, str]
    db_path: str | None = None
    sop_owner_email: str = db.SOP_OWNER_EMAIL
    thread_status: str | None = None
    locked: bool = False  # set after a security rejection; blocks further outbound customer mail
    label_token: str | None = None  # token of the label issued/reused in this run
    verify_attempted: bool = False  # ownership/eligibility check has run in this turn
    step_logs: list[dict[str, Any]] = field(default_factory=list)

    def log(self, step: str, **data: Any) -> None:
        self.step_logs.append({"step": step, **data})


# --------------------------------------------------------------------- helpers

def _resolve(ctx: SOPSharedContext, token: str, kind: str) -> str | None:
    token = (token or "").strip()
    m = TOKEN_RE.fullmatch(token)
    if not m or m.group(1) != kind:
        return None
    return ctx.vault.get(token)


def _days_since(iso: str) -> float:
    delivered = datetime.fromisoformat(iso)
    if delivered.tzinfo is None:
        delivered = delivered.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - delivered).total_seconds() / 86400


def _send(ctx: SOPSharedContext, inbox: str, recipient: str, subject: str, body: str) -> int:
    return db.insert_email(ctx.thread_id, inbox, recipient, subject, body, ctx.db_path)


# ---------------------------------------------------------- tool implementations
# Plain functions so they can be unit tested without an LLM; wrapped as tools below.

def verify_order_and_create_label_impl(
    ctx: SOPSharedContext, order_token: str, part_token: str, quantity: int = 1
) -> str:
    ctx.log("tool_call", tool="verify_order_and_create_label",
            args={"order_token": order_token, "part_token": part_token, "quantity": quantity})
    ctx.verify_attempted = True

    order_id = _resolve(ctx, order_token, "ORDER")
    part_number = _resolve(ctx, part_token, "PART")
    if order_id is None or part_number is None:
        missing = [n for n, v in (("order", order_id), ("part", part_number)) if v is None]
        result = (f"MISSING_INFO: unknown or missing {' and '.join(missing)} token. "
                  "Ask the customer for it with send_return_emails(request_missing_info=true).")
        ctx.log("tool_result", tool="verify_order_and_create_label", result=result)
        return result

    ctx.log("local_unmask", order_token=order_token, order_id=order_id,
            part_token=part_token, part_number=part_number, local_only=True)

    # Pillar 4 — 3-way anti-spoofing check.
    order = db.find_order_for_customer(order_id, part_number, ctx.customer_email, ctx.db_path)
    if order is None:
        existing = db.find_order(order_id, part_number, ctx.db_path)
        if existing is not None:
            return _reject_spoof(ctx, existing, order_token, part_token)
        result = (f"NOT_FOUND: no order matches {order_token} with part {part_token}. "
                  "Ask the customer to double-check both values (request_missing_info=true).")
        ctx.log("tool_result", tool="verify_order_and_create_label", result=result)
        return result

    ctx.sop_owner_email = order["sop_owner_email"]

    # Pillar 5 — eligibility engine.
    reasons = []
    if order["status"] not in ("DELIVERED", "RETURN_INITIATED"):
        reasons.append(f"order status is {order['status']} (must be DELIVERED)")
    age = _days_since(order["delivered_at"])
    if age > RETURN_WINDOW_DAYS:
        reasons.append(f"delivered {int(age)} days ago, outside the {RETURN_WINDOW_DAYS}-day return window")
    if quantity < 1 or quantity > order["purchased_qty"]:
        reasons.append(f"requested quantity {quantity} exceeds purchased quantity {order['purchased_qty']}")
    if reasons:
        ctx.thread_status = RETURN_DENIED
        result = (f"INELIGIBLE: return for {part_token} on {order_token} denied: " + "; ".join(reasons)
                  + ". Politely explain this to the customer and notify SOP via send_return_emails.")
        ctx.log("eligibility", passed=False, reasons=reasons)
        ctx.log("tool_result", tool="verify_order_and_create_label", result=result)
        return result
    ctx.log("eligibility", passed=True, days_since_delivery=round(age, 1), quantity=quantity)

    # Pillar 6B — DB idempotency.
    if order["status"] == "RETURN_INITIATED" and order["return_label"]:
        label = order["return_label"]
        reused = True
    else:
        label = f"UPS-RMA-{order_id}"
        db.set_return_label(order_id, part_number, label, ctx.db_path)
        reused = False
    label_token = add_to_vault(ctx.vault, "LABEL", label)
    ctx.label_token = label_token
    ctx.thread_status = RETURN_LABEL_ISSUED
    ctx.log("label", label_token=label_token, label=label, reused=reused, local_only=True)

    verb = "already existed and was reused" if reused else "was created"
    result = (f"APPROVED: return label {label_token} {verb} for part {part_token} on order {order_token}. "
              "Now call send_return_emails with the label token in the customer reply.")
    ctx.log("tool_result", tool="verify_order_and_create_label", result=result)
    return result


def _reject_spoof(ctx: SOPSharedContext, order: dict[str, Any], order_token: str, part_token: str) -> str:
    ctx.locked = True
    ctx.thread_status = SPOOF_ESCALATED
    ctx.log("anti_spoofing", passed=False, sender=ctx.customer_email, owner=order["customer_email"], local_only=True)
    _send(
        ctx, SOP_EMPLOYEE_INBOX, order["sop_owner_email"],
        f"[SECURITY] Possible spoofed return request on {order['order_id']}",
        (f"A return was requested for order {order['order_id']} / part {order['part_number']} "
         f"by {ctx.customer_email}, but this order belongs to {order['customer_email']}.\n\n"
         f"No label was created. Thread: {ctx.thread_id}. Please review manually."),
    )
    _send(
        ctx, CUSTOMER_INBOX, ctx.customer_email,
        "Your return request is under review",
        ("Thank you for contacting us. We were unable to verify your request automatically, "
         "so it has been forwarded to our Sales Operations team for manual review."),
    )
    result = ("REJECTED: the sender is not the owner of this order. The request was escalated to the "
              "SOP team and the customer was already notified. Do not send any further emails; just stop.")
    ctx.log("tool_result", tool="verify_order_and_create_label", result=result)
    return result


def send_return_emails_impl(
    ctx: SOPSharedContext,
    customer_subject: str,
    customer_body: str,
    sop_summary: str = "",
    request_missing_info: bool = False,
) -> str:
    ctx.log("tool_call", tool="send_return_emails", args={
        "customer_subject": customer_subject, "customer_body": customer_body,
        "sop_summary": sop_summary, "request_missing_info": request_missing_info})

    if ctx.locked:
        result = "SKIPPED: this thread is locked after a security rejection; no emails were sent."
        ctx.log("tool_result", tool="send_return_emails", result=result)
        return result

    # Never email a sender about an order before the 3-way ownership check has run.
    if not ctx.verify_attempted and tokens_of(ctx.vault, "ORDER") and tokens_of(ctx.vault, "PART"):
        result = ("BLOCKED: an order and a part token are both known, so call "
                  "verify_order_and_create_label first. No emails were sent.")
        ctx.log("guard", rule="verify_before_send", local_only=True)
        ctx.log("tool_result", tool="send_return_emails", result=result)
        return result

    # Deterministic backstop: an approved return email must always carry the label number.
    if ctx.label_token and ctx.label_token not in customer_body:
        customer_body = customer_body.rstrip() + f"\n\nYour return label number: {ctx.label_token}"
        ctx.log("label_appended", label_token=ctx.label_token,
                note="LLM omitted the label token; appended locally")

    # Token de-masking happens here, locally, right before dispatch.
    subject = unmask_text(customer_subject, ctx.vault)
    body = unmask_text(customer_body, ctx.vault)
    sent = [_send(ctx, CUSTOMER_INBOX, ctx.customer_email, subject, body)]
    if sop_summary.strip():
        sent.append(_send(ctx, SOP_EMPLOYEE_INBOX, ctx.sop_owner_email,
                          f"[SOP] {subject}", unmask_text(sop_summary, ctx.vault)))

    if request_missing_info:
        ctx.thread_status = AWAITING_CUSTOMER_INFO
    elif ctx.thread_status is None:
        ctx.thread_status = "REPLIED"
    ctx.log("dispatch", email_ids=sent, local_only=True)
    result = f"SENT: {len(sent)} email(s) dispatched."
    ctx.log("tool_result", tool="send_return_emails", result=result)
    return result


# ------------------------------------------------------------------ SDK tools

@function_tool
def verify_order_and_create_label(
    ctx: RunContextWrapper[SOPSharedContext], order_token: str, part_token: str, quantity: int
) -> str:
    """Verify the order belongs to the sender, check return eligibility, and create (or reuse) a return label.

    Args:
        order_token: The order token exactly as it appears, e.g. <ORDER_TOKEN_1>.
        part_token: The part token exactly as it appears, e.g. <PART_TOKEN_1>.
        quantity: Number of units to return. Use 1 if the customer did not say.
    """
    return verify_order_and_create_label_impl(ctx.context, order_token, part_token, quantity)


@function_tool
def send_return_emails(
    ctx: RunContextWrapper[SOPSharedContext],
    customer_subject: str,
    customer_body: str,
    sop_summary: str,
    request_missing_info: bool,
) -> str:
    """Send the customer reply and an internal SOP summary. Use tokens like <ORDER_TOKEN_1>; they are filled in locally.

    Args:
        customer_subject: Subject line of the reply to the customer.
        customer_body: Full plain-text reply to the customer. Reference orders/parts/labels only by token.
        sop_summary: Short internal summary for the Sales Operations team. Use an empty string to skip.
        request_missing_info: True if this email asks the customer for a missing order or part number.
    """
    return send_return_emails_impl(ctx.context, customer_subject, customer_body, sop_summary, request_missing_info)


RETURN_AGENT_INSTRUCTIONS = """\
You are the Return Specialist for a B2B Sales Operations team.

Privacy: order numbers, part numbers and shipping labels are replaced by tokens such as
<ORDER_TOKEN_1>, <PART_TOKEN_1>, <LABEL_TOKEN_1>. Always copy tokens exactly. Never invent
real-looking order numbers, part numbers, or labels, and never ask the customer to explain tokens.

Procedure:
1. Look at the customer's message and the list of known tokens for this thread.
2. If an <ORDER_TOKEN_N> or a <PART_TOKEN_N> is missing, call send_return_emails with
   request_missing_info=true, politely asking ONLY for what is missing, then stop.
3. If both are known, call verify_order_and_create_label once (quantity=1 unless stated).
4. Act on its result:
   - APPROVED: call send_return_emails with a short friendly reply. customer_body MUST contain the
     label token verbatim (e.g. "Your return label number is <LABEL_TOKEN_1>.") and tell the customer
     to write or print that label number on the package. There are no attachments: never say the label
     is "attached" or "enclosed". Include a one-line sop_summary.
   - INELIGIBLE: call send_return_emails explaining the reason politely; include a sop_summary.
   - NOT_FOUND or MISSING_INFO: call send_return_emails with request_missing_info=true.
   - REJECTED: do nothing else.
5. Call send_return_emails at most once. Finish with one short sentence describing what you did.
Sign customer emails as "Sales Operations Team".
"""


def build_return_agent(model: str | Model | None = None) -> Agent[SOPSharedContext]:
    return Agent[SOPSharedContext](
        name="Return Agent",
        instructions=RETURN_AGENT_INSTRUCTIONS,
        model=model or RETURN_AGENT_MODEL,
        tools=[verify_order_and_create_label, send_return_emails],
        # One tool call per model turn, so the ownership check always finishes before any email.
        model_settings=ModelSettings(parallel_tool_calls=False),
    )


def build_agent_input(masked_text: str, vault: dict[str, str]) -> str:
    """Masked message + token *names* (never values) known for this thread."""
    known = {k: tokens_of(vault, k) for k in ("ORDER", "PART", "LABEL")}
    lines = [f"Known {k} tokens in this thread: {', '.join(v) if v else 'none'}" for k, v in known.items()]
    return "New customer email (PII masked):\n" + masked_text + "\n\n" + "\n".join(lines)


async def run_return_agent(
    ctx: SOPSharedContext,
    masked_text: str,
    model: str | Model | None = None,
    session_db_path: str | None = None,
) -> dict[str, Any]:
    agent = build_return_agent(model)
    session = SQLiteSession(session_id=ctx.thread_id, db_path=session_db_path or ctx.db_path or db.DB_PATH)
    agent_input = build_agent_input(masked_text, ctx.vault)
    ctx.log("agent_input", text=agent_input)
    result = await Runner.run(
        agent,
        agent_input,
        context=ctx,
        session=session,
        max_turns=8,
        # Traces would include tool payloads; keep everything local.
        run_config=RunConfig(tracing_disabled=True),
    )
    final = str(result.final_output)
    ctx.log("agent_final", text=final)
    return {"final_output": final}
