# Backend image. Build context is the repo root (the uv project lives there),
# so paths below are repo-root-relative.
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Resolve dependencies first for layer caching — only re-runs when the lock changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

COPY backend/ ./backend/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
