# Sales Research Copilot

Turns `(company, website, objective)` into a grounded, cited sales briefing via a
durable, self-correcting LangGraph workflow — with grounded follow-up chat.

See [`PRD.md`](./PRD.md) for the spec and [`IMPLEMENTATION-PLAN.md`](./IMPLEMENTATION-PLAN.md)
for the build order. _(This README is fleshed out in the final docs slice.)_

## Quickstart

```bash
cp .env.example .env        # fill provider keys later (slice 7); not needed to boot
docker compose up --build   # api :8000 · web :5173 · postgres :5432 · redis :6379
```

- Web shell: <http://localhost:5173> (live system-status view)
- Health: <http://localhost:8000/healthz> — reflects **live** Postgres + Redis checks
  (200 when both are up, 503 otherwise)

## Using it

1. Open <http://localhost:5173>, enter a company / website / objective, and start a run.
2. The detail page streams **live node-by-node progress** over SSE (plan questions,
   discovered sources, fact counts, draft) — reconnect or refresh mid-run resumes
   from the current snapshot, not a blank screen.
3. When the run finishes it renders the nine sections with inline citations, honest
   **Unknowns**, and a **Sources** list. The home page lists **recent runs**
   (most-recent-first), each linking back to its briefing.

Until slice 7 the LLM + search providers are deterministic mocks, so a run needs
no API keys and completes instantly.

## Local backend dev (without Docker)

Dependencies are managed with [uv](https://docs.astral.sh/uv/) (Python 3.11):

```bash
uv sync
uv run python -m app                  # serves :8000 (selector loop for psycopg on Windows)
DATABASE_URL=... REDIS_URL=... uv run pytest   # needs postgres + redis up
```

> Tests are DB-backed: `docker compose up -d postgres redis` first, then run pytest.

## Layout

```
backend/app/
  domain/      models, ResearchState, idempotent reducers, deterministic ids
  providers/   LLM + search seams (mock now, OpenAI/Tavily in slice 7)
  graph/       LangGraph nodes, assembly, Postgres checkpointer, background runner
  db/          SQL migrations + the single-transaction, replacing persist repository
  services/    session orchestration
  api/         FastAPI routes + schemas (create/detail, history, SSE stream, resume)
  infra/       Postgres + Redis adapters; Redis pub/sub progress fan-out
frontend/      Vite + React + TS + Tailwind v4 (create form, live progress, history, report)
```
