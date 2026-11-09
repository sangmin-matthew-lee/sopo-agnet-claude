import json

import httpx
import pytest

from jev_router import (
    JEV_URL,
    JevResult,
    PIILeakError,
    build_request,
    classify,
    parse_response,
    route,
)


def jev_payload(spam=0.02, intent="return", conf=0.95, score=0.1):
    return {
        "model": "jev-latest",
        "answers": {
            "is_spam_or_injection": {"type": "noul", "noul": spam},
            "intent": {"type": "choice", "choice": intent, "confidence": conf,
                       "probabilities": {intent: conf}},
            "frustration_level": {"type": "score", "score": score, "confidence": 0.8,
                                  "probabilities": {"0": 0.9, "1": 0.1, "2": 0.0}},
        },
    }


def result(**kw) -> JevResult:
    return parse_response(jev_payload(**kw))


# ----------------------------------------------------------------- routing

def test_path_c_return_agent():
    d = route(result(intent="return", conf=0.92, score=0.2))
    assert d.path == "C_RETURN_AGENT" and d.thread_status is None


@pytest.mark.parametrize("kw", [dict(spam=0.75), dict(spam=0.99, intent="return"), dict(intent="non_sop")])
def test_path_a_block(kw):
    d = route(result(**kw))
    assert d.path == "A_BLOCK" and d.thread_status == "BLOCKED_SPAM"


def test_block_takes_priority_over_escalation():
    d = route(result(spam=0.9, score=2.0, intent="human_needed"))
    assert d.path == "A_BLOCK"


@pytest.mark.parametrize("kw", [
    dict(score=1.5),                     # furious
    dict(score=1.9, intent="return"),     # furious even with return intent
    dict(intent="human_needed"),
    dict(intent="return", conf=0.59),     # low confidence
    dict(intent="refund", conf=0.9),      # no automated handler
    dict(intent="tech_support", conf=0.9),
])
def test_path_b_escalate(kw):
    d = route(result(**kw))
    assert d.path == "B_ESCALATE" and d.thread_status == "ESCALATED_TO_HUMAN"


def test_boundaries():
    assert route(result(spam=0.7499)).path == "C_RETURN_AGENT"
    assert route(result(score=1.4999)).path == "C_RETURN_AGENT"
    assert route(result(conf=0.60)).path == "C_RETURN_AGENT"


# ------------------------------------------------------------ jev client

def test_request_body_shape():
    body = build_request("return <PART_TOKEN_1>")
    assert body["state"] == {"email_body": "return <PART_TOKEN_1>"}
    assert body["questions"]["is_spam_or_injection"]["type"] == "noul"
    assert body["questions"]["intent"]["type"] == "choice"
    assert set(body["questions"]["intent"]["criteria"]) == {"return", "tech_support", "refund", "human_needed", "non_sop"}
    assert body["questions"]["frustration_level"]["type"] == "score"
    assert len(body["questions"]["frustration_level"]["criteria"]) == 3


async def test_classify_calls_live_endpoint_with_masked_text():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=jev_payload(intent="return", conf=0.88, score=0.3))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await classify("return <PART_TOKEN_1> from <ORDER_TOKEN_1>", api_key="k", client=client)

    assert seen["url"] == JEV_URL
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["state"]["email_body"] == "return <PART_TOKEN_1> from <ORDER_TOKEN_1>"
    assert r.source == "jev" and r.intent == "return" and r.intent_confidence == 0.88
    assert r.frustration_level == 0.3


async def test_classify_refuses_raw_pii():
    with pytest.raises(PIILeakError):
        await classify("return AB-1234 from ORD-9988", api_key="k")


async def test_no_api_key_fails_closed_to_human(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    r = await classify("return <PART_TOKEN_1>")
    assert r.source == "fallback_no_api_key"
    assert route(r).path == "B_ESCALATE"


async def test_api_error_fails_closed_to_human():
    transport = httpx.MockTransport(lambda req: httpx.Response(500, json={"error": "boom"}))
    async with httpx.AsyncClient(transport=transport) as client:
        r = await classify("hello", api_key="k", client=client)
    assert r.source == "fallback_api_error"
    assert route(r).path == "B_ESCALATE"


async def test_malformed_response_fails_closed():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"answers": {}}))
    async with httpx.AsyncClient(transport=transport) as client:
        r = await classify("hello", api_key="k", client=client)
    assert r.source == "fallback_api_error"


# ------------------------------------------------- multi-turn continuation

def test_reply_on_awaiting_thread_continues_even_if_non_sop():
    d = route(result(intent="non_sop", conf=0.99), awaiting_customer_info=True)
    assert d.path == "C_RETURN_AGENT"
    d = route(result(intent="return", conf=0.4), awaiting_customer_info=True)
    assert d.path == "C_RETURN_AGENT"


def test_non_sop_without_awaiting_thread_still_blocks():
    assert route(result(intent="non_sop", conf=0.99), awaiting_customer_info=False).path == "A_BLOCK"


@pytest.mark.parametrize("kw,path", [
    (dict(spam=0.9, intent="non_sop"), "A_BLOCK"),         # spam in a thread is still blocked
    (dict(intent="return", score=1.8), "B_ESCALATE"),        # angry reply still escalates
    (dict(intent="human_needed"), "B_ESCALATE"),
    (dict(intent="refund", conf=0.9), "B_ESCALATE"),
])
def test_awaiting_thread_keeps_safety_rules(kw, path):
    assert route(result(**kw), awaiting_customer_info=True).path == path


def test_awaiting_thread_with_jev_unavailable_still_fails_closed():
    from jev_router import fail_closed
    assert route(fail_closed("fallback_no_api_key"), awaiting_customer_info=True).path == "B_ESCALATE"


def test_request_includes_thread_context_only_when_given():
    from jev_router import AWAITING_INFO_CONTEXT
    plain = build_request("it's <ORDER_TOKEN_1>")
    assert "thread_context" not in plain["state"]
    ctxd = build_request("it's <ORDER_TOKEN_1>", thread_context=AWAITING_INFO_CONTEXT)
    assert ctxd["state"]["thread_context"] == AWAITING_INFO_CONTEXT
    assert "thread_context" in ctxd["questions"]["intent"]["instructions"]
    assert "thread_context" not in plain["questions"]["intent"]["instructions"]  # shared dict not mutated


async def test_thread_context_with_raw_pii_refused():
    with pytest.raises(PIILeakError):
        await classify("hi", api_key="k", thread_context="order ORD-9988")
