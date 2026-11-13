# Cloud Run image for SOPO Agent. Deploy with --max-instances=1 (SQLite lives in the instance).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PORT=8080 \
    SOP_DB_PATH=/tmp/sop_demo.db

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py pipeline.py ./
COPY core ./core
COPY sop_agents ./sop_agents
COPY live ./live
COPY static ./static

RUN useradd --create-home appuser
USER appuser

EXPOSE 8080
# --proxy-headers: trust Cloud Run's proxy for https URLs (e.g. dashboard links when PUBLIC_BASE_URL is unset).
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
