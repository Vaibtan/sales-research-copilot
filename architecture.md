# Architecture — Sales Research Copilot

How the system is put together and why it holds up under failure. This is the
reader-facing distillation; the canonical spec is [`PRD.md`](./PRD.md) and the
build log with per-slice verification is [`IMPLEMENTATION-PLAN.md`](./IMPLEMENTATION-PLAN.md).

## 1. System shape

```
┌─────────────┐   REST + SSE    ┌──────────────────────────────────────────────┐
│  React SPA  │ ───────────────▶│                 FastAPI                        │
│ (Vite/TS/   │ ◀─────────────── │  routes → services → graph runner / chat svc  │
│  Tailwind)  │   stream/chat    └───────┬───────────────┬──────────────┬───────┘
└─────────────┘                          │               │              │
                                  LangGraph graph   relational repo   Redis pub/sub
                                          │               │              │
                          ┌───────────────┴──────┐   ┌────┴─────┐   ┌────┴────┐
                          │ Postgres (pgvector)   │   │ Postgres │   │  Redis  │
                          │ • LangGraph checkpoint│   │ • product│   │ • SSE   │
                          │ • llm_calls ledger    │   │   tables │   │   fan-out│
                          │ • pgvector chunks/mem │   │          │   │ • snapshot│
                          └───────────────────────┘   └──────────┘   └─────────┘
                          OpenAI (Responses) · Tavily + httpx  ← swappable provider seams
```

The backend is layered: **routes** (HTTP/SSE contracts) → **services** (session +
chat orchestration) → **graph** (the LangGraph workflow + a background runner) →
**db / providers / infra** (persistence, model/search seams, Redis). Everything
external sits behind an interface with a deterministic mock, so the whole product
runs and is fully tested offline.

## 2. The workflow (the heart)

A single LangGraph state machine over a shared `ResearchState`, checkpointed per
super-step (`thread_id = session_id`):

```
START → plan → [await_approval] → recall → gather ──fan-out──▶ gather_one ×N
                  (HITL interrupt)                 │                  │
                                                   └──(all recalled)──┤
                                                                      ▼
                                                                   extract → analyze → quality_gate
                                                                                            │
                              ┌──── score<τ ∧ retries>0 ∧ weak≠∅ : loop back to gather ◀────┤
                              ▼                                                              │
                           report ──────────────────────────────────────────────  else ────┘
                              ▼
                           persist → END
   error_handler on extract/analyze (and self-handled in gather_one):
     NodeError → degraded partial report (never a 500)
```

It exercises every LangGraph primitive the brief asks for, used as a durable state
machine rather than a `for` loop:

| Primitive | Where |
| :-- | :-- |
| Multiple meaningful nodes | plan · await_approval · recall · gather/gather_one · extract · analyze · quality_gate · report · persist |
| Shared state + reducers | `ResearchState` with idempotent reducers — `merge_by_id` (sources), `supersede_by_question` / `supersede_assignments_by_question` (facts/links, attempt-aware), `keep_degraded` |
| Conditional routing | `quality_gate` returns `Command(goto="gather" | "report")` — atomic route **and** state update |
| Intermediate outputs | plan / sources / facts / draft streamed live per super-step |
| Failure handling | per-node `RetryPolicy(retry_on=transient)` + `error_handler` → degraded; `gather_one` self-handles (workers aren't covered by node error handlers) |
| Recoverability | Postgres checkpoint + resume; durable `llm_calls` ledger so resume re-pays no tokens |
| HITL | `interrupt()` after plan; resume with `Command(resume=edited_questions)` |
| Parallelism | `Send` fan-out over research questions |
| Memory | cross-session `recall` + pgvector chat RAG |

## 3. Durable execution — why a kill is safe

Two layers cooperate so a mid-run kill costs nothing:

1. **Checkpoint per super-step.** LangGraph's `AsyncPostgresSaver` persists state
   between nodes, so a resume continues from the last good checkpoint — completed
   nodes never re-execute.
2. **The `llm_calls` ledger** (PRD §6.5) closes the *within-node* gap. Every LLM /
   embedding / search call is keyed by `(session_id, node, attempt, input_hash)`
   and runs: **reserve** (insert `in_progress` with an owner + lease) →
   **single-flight** (a duplicate with a valid lease waits; a `done` row returns
   the cached response) → **lease-takeover** (a crashed owner's expired lease is
   taken via compare-and-swap) → **call with the ledger key as the provider
   idempotency key** → **commit**. Only a committed result enters graph state — a
   non-owner that lost a takeover (on either the commit *or* the exception path)
   re-resolves from the authoritative row instead of returning its own value, so a
   resume always replays what the run actually saw. Lease math is evaluated DB-side
   to dodge clock skew; each step is its own committed statement so cross-connection
   waiters observe it. (Search has no provider idempotency header, so the ledger's
   committed-result short-circuit — not provider-side dedup — is its protection.)

The honest guarantee (PRD §6.5): a committed call is never re-paid, and the single
in-flight call at crash time is de-duped by the provider idempotency key.

`persist` is the third pillar: one transaction that **replaces** the whole session
materialization — prune-then-upsert on deterministic ids, cascade-deleting dropped
sources (→ their facts/chunks) and superseding re-researched facts — so a
regenerate/resume is *convergent*, not merely duplicate-free. The report version
bumps only when the served content actually changes (a content hash), so a
resume-replay doesn't falsely stale the chat.

## 4. Memory — three layers, three jobs

| Layer | Store | Holds | Job |
| :-- | :-- | :-- | :-- |
| 1. Run state | LangGraph `PostgresSaver` (thread = session) | `ResearchState` per super-step | durability + resume; internal |
| 2. Product data | Postgres tables | sessions, reports, sources, facts, messages | the clean entities the API serves; `persist` is the seam |
| 3. Semantic | pgvector | report_chunks, source_chunks, research_memory | chat RAG + cross-session warm-start |

`research_memory` is **self-contained** (its own provenance + `content_hash` +
`fetched_at` + persisted `section`/`freshness_class`; no foreign `source_id`). On a
run, `recall` matches it to the approved plan by section + embedding; **fresh**
facts are remapped into the session as new session-scoped sources (so a report can
only ever cite a *this-session* row), **stale** ones are re-validated by
`content_hash` or re-derived. Volatile sections (Business Signals/Risks) re-fetch;
durable ones (Overview/Products) warm-start.

## 5. Request & streaming flow

- `POST /sessions` creates the row and **kicks off the run in a background runner**
  (a tracked asyncio task that is the single executor for a thread_id), returning
  immediately. The runner `astream`s the graph and publishes a `ProgressEvent`
  snapshot per super-step to **Redis pub/sub**.
- `GET /sessions/{id}/stream` **subscribes** to that fan-out (it never drives the
  graph itself) and forwards SSE; on connect it replays the latest snapshot
  (cached in Redis with a TTL, falling back to the DB row) so a reconnect shows
  current progress. A terminal status emits `event: end`.
- Resume / approve-plan re-enter via the same runner with monotonic step numbering
  and a non-terminal "resumed" snapshot, so a reconnecting client never replays a
  stale terminal `end`.
- Chat is its own small `retrieve → answer` graph; the answer streams token-by-token
  over SSE. Citations are **snapshotted** at answer time so history resolves even
  after a source is dropped; messages carry the `report_version` they were grounded
  on and are flagged `stale` when the report is regenerated.

## 6. Reliability & cost

- **Never a 500 for a partial failure:** `error_handler` (extract/analyze) and the
  self-handling `gather_one` degrade to an honest partial report; the error
  taxonomy maps to HTTP (transient → 503 + Retry-After, validation → 422, degraded
  → 200 + `degraded:true`, not-found → 404).
- **SSRF guard** on website fetch (blocks private/loopback/link-local/reserved IPs,
  incl. cloud metadata).
- **Cost control:** per-node model + token caps in config; a bounded quality loop
  (τ, ≤1 extra pass); the cross-session cache; the ledger preventing re-pays.
- **Config:** `pydantic-settings` (12-factor), `.env.example`, no secrets in repo.
- **Observability:** OTEL spans per node + LLM call → Langfuse (opt-in profile);
  structured JSON logs with `session_id`/`node`/taxonomy.

## 7. Tech stack

`api`: FastAPI · LangGraph (+ postgres checkpointer) · `openai` (Responses API) ·
`tavily-python` · `httpx` · psycopg3 · redis · `pydantic-settings` · `structlog` ·
OpenTelemetry. `web`: Vite + React + TS + Tailwind. Infra: Postgres (pgvector
image, all three Postgres roles) · Redis · Langfuse (obs profile).
