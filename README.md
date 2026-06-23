# Sales Research Copilot

> *"Your sellers run the conversation. We do everything else."*

Turns `(company, website, objective)` into a grounded, cited 9-section sales
briefing via a **durable, self-correcting LangGraph workflow** — plus grounded
follow-up chat over the report. The standout production signals are the two things
most demos skip: **durable execution** (kill a run mid-flight, resume it without
re-paying tokens or duplicating data) and **layered memory** (per-run checkpoint
state · clean relational product data · pgvector for chat + cross-session warm-start).

- **Spec (the WHAT):** [`PRD.md`](./PRD.md) · **Rationale (the WHY):** [`research.md`](./research.md)
- **Architecture:** [`architecture.md`](./architecture.md) · **Decisions:** [`engineering-decisions.md`](./engineering-decisions.md) · **Product:** [`product-improvements.md`](./product-improvements.md)
- **Build log:** [`IMPLEMENTATION-PLAN.md`](./IMPLEMENTATION-PLAN.md) — 15 vertical slices with per-slice acceptance + verification.

---

## Quickstart

```bash
cp .env.example .env          # runs on mocks with no keys; add OPENAI/TAVILY for live mode
docker compose up --build     # web :5173 · api :8000 · postgres :5432 · redis :6379
```

- **App:** <http://localhost:5173>
- **Health:** <http://localhost:8000/healthz> — reflects **live** Postgres + Redis (200 up / 503 down)
- **API docs:** <http://localhost:8000/docs>

With no provider keys the LLM + search are **deterministic mocks**, so a full run
completes instantly and offline — ideal for trying the durability/HITL/chat flows.
Set `OPENAI_API_KEY` + `TAVILY_API_KEY` in `.env` to switch to the live providers
(model ids are verified against the API at boot; an unknown id fails fast).

## What you can do

1. **Create a run** (company / website / objective). The detail page streams
   **live node-by-node progress** over SSE — plan questions, discovered sources,
   fact counts, draft-ready. Refresh or reconnect mid-run and it rehydrates from
   the current snapshot, never a blank screen.
2. **Review the plan (HITL).** By default the run pauses after planning so you can
   edit / add / remove the research questions, then **Approve** to continue.
   (Set `HITL_PLAN_APPROVAL=false` to auto-approve.)
3. **Read the briefing.** Nine sections, every claim cited to a **Sources** list,
   unsupported items in honest **Unknowns** (no fabrication).
4. **Chat.** Ask follow-ups grounded in the report + sources; answers cite
   `source_id`s and decline ("not in the report") when out of scope.
5. **Resume / retry.** A `degraded` (partial) or `failed` run shows a **Resume**
   button that re-enters the graph to attempt an upgrade.
6. **History.** The home page lists prior runs, most-recent-first.

## The kill-and-resume demo (the marquee feature)

Recoverability is real, not cosmetic — proven by tests over a real Postgres
checkpointer (`backend/tests/test_resume.py`, `test_ledger.py`):

- LangGraph checkpoints **per super-step** (thread_id = session_id), so a resume
  skips completed nodes — **no re-executed work**.
- Every LLM / embedding call is wrapped in a **durable ledger** (`llm_calls`,
  PRD §6.5): reserve → single-flight / lease-takeover → call-with-provider-
  idempotency-key → commit. A committed call is **never re-paid**; concurrent
  resumes single-flight on one provider call; a crash mid-call is de-duped by the
  provider idempotency key. → *a mid-node kill re-pays no tokens and leaves no
  duplicate rows.*
- `persist` is a single transaction that **replaces** the full session
  materialization (deletes orphans, supersedes re-researched facts), so a
  regenerate/resume is **convergent**, not just duplicate-free.

To see it end-to-end with the mocks, the resume tests simulate a crash with
`interrupt_after=["extract"]` then resume and assert `plan` is not re-paid and
row counts match the report.

## Tests

DB-backed (Postgres + Redis). The suite is hermetic — it forces the mock providers
and isolates the cross-session memory table, so no secrets or network are needed.

```bash
docker compose up -d postgres redis
uv run pytest backend/tests          # 98 tests
```

## Local dev (without Docker)

Python 3.11, managed by [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run python -m app                 # api :8000 (sets the Windows selector loop for psycopg)
# frontend:
cd frontend && npm install && npm run dev   # :5173
```

## Observability (optional)

```bash
docker compose --profile obs up      # adds self-hosted Langfuse (+ clickhouse/minio)
```

Set `OTEL_ENABLED=true` + `LANGFUSE_HOST`/keys and the app exports OTEL spans
(per node + per LLM call, with token/latency) to Langfuse. Off by default — core
dev has no dependency on it. Every workflow log line is structured JSON carrying
`session_id` + `node`.

## Layout

```
backend/app/
  domain/        models, ResearchState + idempotent reducers, deterministic ids, chunking, sections
  providers/     LLM + search + embedding seams: deterministic mocks, OpenAI/Tavily, the ledger wrappers, SSRF fetch
  graph/         LangGraph nodes, assembly (build.py), Postgres checkpointer, background runner, chat graph
  db/            SQL migrations, the durable call ledger, the single-transaction replacing persist repository
  services/      session + chat orchestration
  api/           FastAPI routes/schemas (create/detail, history, SSE stream, resume, approve-plan, chat, messages) + error taxonomy
  infra/         Postgres + Redis adapters; Redis pub/sub progress fan-out
  observability/ OTEL tracing → Langfuse
frontend/src/    Vite + React + TS + Tailwind — create form, live progress, plan review, report + citations, chat, history
```

## API (FastAPI)

| Method | Path | Purpose |
| :-- | :-- | :-- |
| POST | `/sessions` | Create a session; start the graph |
| GET | `/sessions` · `/sessions/{id}` | History · detail + report |
| GET | `/sessions/{id}/stream` | SSE live progress |
| GET | `/sessions/{id}/plan` · POST `/approve-plan` | HITL: proposed questions · approve (edited) |
| POST | `/sessions/{id}/resume` | Resume a degraded/failed/interrupted run |
| POST | `/sessions/{id}/chat` · GET `/messages` | Grounded chat (SSE) · history |
| GET | `/healthz` | Live db + redis readiness |
