"""Live-streaming pipeline (process_email_stream) and the SSE endpoint."""

import asyncio
import json

import db
import pipeline
from masker import contains_raw_pii
from tests.test_api import APPROVE_SCRIPT, CODE, H, client, jev, stub  # noqa: F401 (fixtures)


async def collect(**kw):
    args = dict(sender_email="john@acmecorp.com", thread_id="th-s", message_id="m-s",
                body="Hi, I'd like to return ab - 1234 from ord/9988.")
    args.update(kw)
    return [e async for e in pipeline.process_email_stream(**args)]


def reassemble_tool_args(events):
    """item_id -> (tool name, concatenated argument deltas)."""
    tools, args = {}, {}
    for e in events:
        if e["type"] == "agent_delta" and e["kind"] == "tool_start":
            tools[e["item_id"]] = e["tool"]
        elif e["type"] == "agent_delta" and e["kind"] == "tool_args":
            args[e["item_id"]] = args.get(e["item_id"], "") + e["delta"]
    return [(tools[i], json.loads(args[i])) for i in tools]


async def test_happy_path_streams_in_pipeline_order(client, stub):
    stub["scripts"] = [APPROVE_SCRIPT]
    events = await collect()
    types = [e["type"] for e in events]
    titles = [e.get("title") for e in events]

    assert types[-1] == "done" and events[-1]["result"]["status"] == "RETURN_LABEL_ISSUED"
    assert titles[0] == "Idempotency check passed"
    # Spinners are announced before the slow calls they cover.
    assert titles.index("Calling Jev System 1…") < titles.index("Jev System 1 classification")
    assert titles.index("Return Agent (gpt-4o-mini) thinking…") < titles.index("Return Agent finished")
    # Agent output arrives as many small deltas, between agent start and finish.
    first_delta = types.index("agent_delta")
    assert titles.index("Return Agent (gpt-4o-mini) thinking…") < first_delta < titles.index("Return Agent finished")
    assert types.count("agent_delta") > 10


async def test_streamed_tool_args_match_what_the_model_sent(client, stub):
    stub["scripts"] = [APPROVE_SCRIPT]
    events = await collect()
    assert reassemble_tool_args(events) == [(s[1], s[2]) for s in APPROVE_SCRIPT if s[0] == "tool"]
    text = "".join(e["delta"] for e in events if e["type"] == "agent_delta" and e["kind"] == "text")
    assert text == "done"


async def test_local_tool_steps_stream_live(client, stub):
    stub["scripts"] = [APPROVE_SCRIPT]
    events = await collect()
    logs = [e["step"] for e in events if e["type"] == "agent_log"]
    assert logs[:2] == ["agent_input", "tool_call"]
    for expected in ("local_unmask", "eligibility", "label", "dispatch", "agent_final"):
        assert expected in logs


async def test_done_trace_equals_streamed_steps(client, stub):
    stub["scripts"] = [APPROVE_SCRIPT]
    events = await collect()
    steps = [{k: v for k, v in e.items() if k != "type"} for e in events if e["type"] == "step"]
    assert events[-1]["result"]["trace"] == steps


async def test_nothing_model_facing_contains_raw_pii(client, stub):
    stub["scripts"] = [APPROVE_SCRIPT]
    events = await collect()
    for e in events:
        if e["type"] == "agent_delta":
            assert not contains_raw_pii(e.get("delta", "")), e
        if e["type"] == "agent_log" and not e.get("local_only"):
            assert not contains_raw_pii(json.dumps(e)), e


async def test_spam_streams_without_agent(client, stub):
    stub["jev"] = jev(spam=0.98, intent="non_sop")
    events = await collect(body="crypto 1000x!!!")
    assert events[-1]["result"]["status"] == "BLOCKED_SPAM"
    assert not any(e["type"] in ("agent_delta", "agent_log") for e in events)
    assert not any(e.get("phase") == "phase3" for e in events)


async def test_duplicate_streams_done(client, stub):
    stub["jev"] = jev(spam=0.98)
    await collect()
    events = await collect()
    assert events[-1]["result"]["status"] == "DUPLICATE_IGNORED"


async def test_pipeline_finishes_if_client_disconnects(client, stub):
    stub["scripts"] = [APPROVE_SCRIPT]
    gen = pipeline.process_email_stream("john@acmecorp.com", "th-dc", "m-dc", "Return AB-1234 from ORD-9988")
    await gen.__anext__()          # receive one event...
    await gen.aclose()             # ...then the browser goes away
    for _ in range(200):
        if not pipeline._BACKGROUND:
            break
        await asyncio.sleep(0.01)
    assert db.load_thread("th-dc")["thread_status"] == "RETURN_LABEL_ISSUED"
    assert db.find_order("ORD-9988", "AB-1234")["return_label"] == "UPS-RMA-ORD-9988"


async def test_pipeline_exception_becomes_error_event(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(pipeline, "process_email", boom)
    events = await collect()
    assert events == [{"type": "error", "error": "RuntimeError: db down"}]


# ------------------------------------------------------------- HTTP / SSE

def sse_events(client, body, headers=H, **kw):
    payload = {"sender_email": "john@acmecorp.com", "thread_id": "th-h", "message_id": "m-h", "body": body, **kw}
    with client.stream("POST", "/api/simulate-email/stream", headers=headers, json=payload) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


def test_stream_endpoint_requires_passcode(client):
    r = client.post("/api/simulate-email/stream", json={
        "sender_email": "john@acmecorp.com", "thread_id": "t", "message_id": "m", "body": "x"})
    assert r.status_code == 401


def test_stream_endpoint_happy_path(client, stub):
    stub["scripts"] = [APPROVE_SCRIPT]
    events = sse_events(client, "Return ab - 1234 from ord/9988")
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["status"] == "RETURN_LABEL_ISSUED"
    assert any(e["type"] == "agent_delta" for e in events)


def test_stream_endpoint_spam(client, stub):
    stub["jev"] = jev(spam=0.99, intent="non_sop")
    events = sse_events(client, "crypto!!!")
    assert events[-1]["result"]["status"] == "BLOCKED_SPAM"
