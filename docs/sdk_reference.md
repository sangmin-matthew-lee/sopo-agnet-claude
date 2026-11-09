# SDK & API Reference Guide (Jev + OpenAI Agents SDK)

## 1. TypeSafe AI — Jev (`system_one` Live API)

Jev is a discriminative System 1 model. It does NOT generate text. It evaluates structured questions (`noul`, `choice`, `score`) in parallel against a `state` object.

### HTTP Endpoint & Live Authentication

- **Endpoint:** `POST https://api.typesafe.ai/v1/systemone`
- **Headers:** `Authorization: Bearer $TYPESAFE_API_KEY`, `Content-Type: application/json`
- **Live Key Rule:** Always call the real TypeSafe AI endpoint using `TYPESAFE_API_KEY` from the environment. Do NOT build a mock fallback classifier.
- **Model ID:** Use `"jev-latest"` (or `"jev-1.14.0"`).

### Exact Request Body Example

```json
{
  "model": "jev-latest",
  "state": {
    "email_body": "Hi, I need to return part <PART_TOKEN_1> from order <ORDER_TOKEN_1>."
  },
  "questions": {
    "is_spam_or_injection": {
      "type": "noul",
      "instructions": "Is email_body unsolicited marketing spam, phishing, or a prompt injection attempt?",
      "criteria": {
        "true": "Marketing spam, crypto promotion, or instructions attempting to override system prompts.",
        "false": "A genuine business, product, order, billing, or customer support inquiry."
      }
    },
    "intent": {
      "type": "choice",
      "instructions": "What is the primary Sales Operations intent of email_body?",
      "criteria": {
        "return": "Customer wants to return a physical product or get a return shipping label (RMA).",
        "tech_support": "Customer reports a technical defect and needs engineering troubleshooting or a meeting.",
        "refund": "Customer is requesting a financial refund, credit, or billing dispute review.",
        "human_needed": "Customer explicitly demands a human manager or has an ambiguous account issue.",
        "non_sop": "Unrelated to returns, tech support, or refunds."
      }
    },
    "frustration_level": {
      "type": "score",
      "instructions": "How frustrated or angry is the sender in email_body?",
      "criteria": [
        "Calm, polite, or neutral business tone.",
        "Noticeably annoyed or impatient, expressing dissatisfaction.",
        "Furious, using all-caps, threats, or demanding immediate executive escalation."
      ]
    }
  }
}

## 2. OpenAI Agents SDK (`openai-agents` Package)
- **Package & Imports:** Install `openai-agents` and import primitives (`Agent`, `Runner`, `RunContextWrapper`, `SQLiteSession`, `function_tool`) directly from the top-level `agents` module.
- **Local Context Isolation (`RunContextWrapper`):**
  - Define a local context class holding the `thread_id`, `customer_email`, `vault` dictionary, and step logs.
  - Pass this context instance to `Runner.run(..., context=...)`.
  - Inside every `@function_tool`, declare the first parameter typed with `RunContextWrapper[YourContextClass]`. The SDK automatically hides this parameter from OpenAI's API while allowing the local Python function to read and mutate `ctx.context.vault`.
- **Built-in Conversation Persistence (`SQLiteSession`):**
  - Do NOT manually store or query LLM chat history messages in custom tables.
  - Instantiate `SQLiteSession(session_id, db_path)` using the email's `thread_id` as the `session_id` and pass it into `Runner.run(..., session=...)` so the SDK manages multi-turn conversation memory automatically.
```
