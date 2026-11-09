# sopo-agnet-claude
Agent for Sales Operation(SOP) Dept. Built using Claude Code

SOP Return Orchestrator: FastAPI + SQLite + TypeSafe Jev (System 1 gatekeeper) + OpenAI Agents SDK
(System 2 Return Agent), with a 3-column live sandbox UI. Order IDs, part numbers and labels are
masked into tokens locally and never sent to any AI provider. See `CLAUDE.md` for the full spec.

## Run locally

```bash
uv sync
export DEMO_ACCESS_CODE=demo TYPESAFE_API_KEY=... OPENAI_API_KEY=...
uv run uvicorn main:app --reload --port 8080
open "http://localhost:8080/?code=demo"
```

If `TYPESAFE_API_KEY` or `OPENAI_API_KEY` is missing (or either API errors), the pipeline **fails closed**:
messages are escalated to the SOP human inbox instead of being auto-processed.

## Test

```bash
uv run pytest
```

Tests run fully offline: Jev is stubbed via `httpx.MockTransport`/monkeypatch, and the Return Agent
runs through the real `Runner` + `SQLiteSession` with a scripted fake model (`tests/fake_model.py`).

## Deploy (Cloud Run)

```bash
gcloud run deploy sop-orchestrator --source . --port 8080 \
  --set-env-vars DEMO_ACCESS_CODE=...,TYPESAFE_API_KEY=...,OPENAI_API_KEY=...
```

SQLite lives at `/tmp/sop_demo.db` in the container (ephemeral, per instance) and the rate limiter
is in-memory — fine for a demo; pin `--max-instances 1` for consistent state.

## Layout

| File | Role |
|---|---|
| `db.py` | Schema, seed data, reset, query helpers |
| `masker.py` | Phase 1: preprocessing + "Catch Messy, Store Clean" token vault |
| `jev_router.py` | Phase 2: Jev client + deterministic Path A/B/C router |
| `agent.py` | Phase 3/4: Return Agent, anti-spoofing, eligibility, label idempotency, de-masking dispatch |
| `pipeline.py` | Orchestrates the 4 phases and builds the X-Ray trace |
| `main.py` | FastAPI endpoints, passcode check, rate limiting |
| `static/index.html` | 3-column sandbox UI |
