# Implementation Plan — AI Sales Research Copilot

> Build-order checklist derived from [`PRD.md`](./PRD.md) (the canonical WHAT) using tracer-bullet
> vertical slices. **The PRD is the source of truth** — this doc sequences the work and gives
> per-slice acceptance criteria; it deliberately does **not** restate the spec. Each `§` reference
> points at the PRD section that defines the behavior.

**Owner:** vaibtan · **Date:** 2026-06-18 · **Status:** approved breakdown

## How to read this

- Each numbered slice is a **thin vertical cut through all layers** (graph + API + DB + UI + tests),
  demoable/verifiable on its own. Prefer finishing a slice end-to-end over building one layer wide.
- **Type:** `AFK` = fully specified, an agent can implement & merge without human interaction ·
  `HITL` = needs a human gate (a decision, secrets, or review) before/within the slice.
- **Acceptance criteria** are the checklist. A slice is done when every box is ticked *and* its demo runs.
- Slices are numbered in **dependency order** — build top-to-bottom; `Blocked by` calls out hard prerequisites.
- This plan covers **only the MVP acceptance gate (PRD §13)**. Explicitly-deferred work is in
  [Fast-follow backlog](#fast-follow-backlog-post-mvp--outside-the-gate) at the bottom.

## Overview

| # | Slice | Phase | Type | Blocked by |
| :-- | :--- | :--- | :--- | :--- |
| 1 | Infra & app skeleton | P0 | AFK | — |
| 2 | Mocked baseline graph + session create/detail | P0 | AFK | 1 |
| 3 | Live SSE progress + session history | P0 | AFK | 2 |
| 4 | Self-correcting quality loop (conditional routing) | P0 | AFK | 2 |
| 5 | Idempotent persist & resume | P0 | AFK | 2 (rel. 4) |
| 6 | Durable LLM-call ledger (token idempotency) | P0 | AFK | 2 (rel. 5) |
| 7 | Real LLM + Tavily providers | P0 | **HITL** | 2, 6 |
| 8 | Graceful degraded path & error taxonomy | P0 | AFK | 2 (rel. 4, 6, 7) |
| 9 | pgvector chunking & embeddings on persist | P1 | AFK | 5, 6 |
| 10 | Grounded follow-up chat | P1 | AFK | 9 |
| 11 | Cross-session research memory (recall made real) | P1 | AFK | 9 (rel. 7) |
| 12 | HITL plan-approval (interrupt → edit → resume) | P2 | AFK | 3 |
| 13 | Parallel `Send` fan-out | P2 | AFK | 5 |
| 14 | Observability: Langfuse profile + structured logs | P3 | AFK | 2 |
| 15 | Docs deliverables + responsive/polish pass | P3 | **HITL** | ~all |

**Dependency notes.** Slice 2 is the spine — almost everything hangs off it. Build durability
(4→5→6) before going live on real APIs (7) so kill/resume is proven against deterministic mocks
first (per the handoff: *mocked first*). The semantic layer (9) gates both chat (10) and real
recall (11). HITL plan-approval (12) only changes "auto-approved" → "human-approved" — recall (11)
stays positioned after `plan` from the start so approved `question_id`s always exist before binding.

---

## 1. Infra & app skeleton

**Phase:** P0 · **Type:** AFK

### What to build

The thinnest end-to-end skeleton that proves the whole stack is wired and boots with one command.
Bring up the Docker topology (Postgres on a pgvector image + Redis), a `pydantic-settings` config
layer reading a documented `.env.example`, a FastAPI app exposing `/healthz` that actually checks
DB + Redis connectivity, and a Vite + React + TS + Tailwind shell that calls `/healthz` and shows
health status. No domain logic yet — this de-risks Docker, config, and the FE↔API seam early.

PRD refs: §12 (stack/infra), §11 (config: `pydantic-settings`, `.env.example`, no secrets in repo).

### Acceptance criteria

- [ ] `docker compose up` starts `api`, `web`, `postgres` (pgvector image), `redis` cleanly.
- [ ] `GET /healthz` returns readiness reflecting **live** DB + Redis checks (not a static 200).
- [ ] The web shell loads and renders API health (healthy / unhealthy) from `/healthz`.
- [ ] Config is loaded via `pydantic-settings`; `.env.example` lists every required key with empty values; no secrets are committed.
- [ ] `.gitignore` excludes `.env`; the repo has `backend/` and `frontend/` (or equivalent) structure.
- [ ] Structured JSON logging is initialized (scaffold; full per-node fields land in slice 14).

### Blocked by

- None — can start immediately.

---

## 2. Mocked baseline graph + session create/detail

**Phase:** P0 · **Type:** AFK

### What to build

The product spine, end-to-end, with **mocked** LLM and `SearchProvider` so the graph, checkpointing,
and persistence are exercised before any real API. Implement the linear LangGraph path
`plan → gather → extract → analyze → report → persist` over the shared `ResearchState` with its
idempotent reducers, a `PostgresSaver` checkpointer (thread_id = session_id), relational persistence
of the served entities, the create/detail API, and a React page that creates a session and renders
the resulting 9-section report. The quality-gate **loop-back** and durability hardening come in later
slices; here the gate node may exist as a straight-through pass.

PRD refs: §6.1 topology, §6.2 `ResearchState` + reducers (`merge_by_id`, `supersede_by_question`),
§5 report contract, §7 layer-2 product data, §9 (`POST /sessions`, `GET /sessions/{id}`), §10 data model.

### Acceptance criteria

- [ ] `POST /sessions {company, website, objective}` creates a session row and starts the graph; returns `session_id`.
- [ ] On create, `company_key` is computed and stored (eTLD+1 of `website`, fallback normalized company name) — needed by recall (#11), §16.
- [ ] The graph runs all baseline nodes over `ResearchState`; `SearchProvider` and the LLM client are behind interfaces and **mocked** (deterministic fixtures).
- [ ] Reducers behave per spec: `merge_by_id` dedupes `sources` by deterministic id; `supersede_by_question` is in place for `facts`.
- [ ] `PostgresSaver` writes a checkpoint per super-step (`PostgresSaver.setup()` run at boot/migration).
- [ ] `persist` writes `sessions`, `reports` (1/session), `sources`, `facts` with their deterministic ids + unique constraints.
- [ ] `GET /sessions/{id}` returns the session + report when ready: all 9 sections, each claim carrying `source_id`(s), unsupported items in **Unknowns** (no fabrication), and a **Sources** list.
- [ ] React: a create form (`company`, `website`, `objective`) and a detail page rendering the 9 sections with inline citations, Unknowns, and Sources.
- [ ] Graph integration test with deterministic mocks asserts a full report is produced and persisted.

### Blocked by

- #1 (Infra & app skeleton).

---

## 3. Live SSE progress + session history

**Phase:** P0 · **Type:** AFK

### What to build

Make the run observable to the user in real time, and let them see prior runs. Add an SSE endpoint
that streams node-by-node status as the graph advances, a session-history list endpoint, and the
corresponding React: a live progress view (node states as they transition) plus a history list with
loading states.

PRD refs: §9 (`GET /sessions/{id}/stream`, `GET /sessions`), §4 (live node-by-node progress),
Technical FE reqs (workflow progress UI, session history, loading states).

### Acceptance criteria

- [ ] `GET /sessions/{id}/stream` emits SSE events as nodes transition (`status` from §6.2: planning → gathering → analyzing → grading → reporting → done).
- [ ] The stream surfaces **intermediate outputs live** (plan questions, discovered sources, fact counts, draft), not just status — satisfying the §6.3 "intermediate outputs" requirement.
- [ ] The stream emits a final `{event:"end", status}` and then closes on any terminal status.
- [ ] `GET /sessions` returns session history (list) ordered most-recent-first.
- [ ] React shows live node-by-node progress driven by the SSE stream, with a loading state while the run is in flight.
- [ ] React history list links to each session detail page.
- [ ] Reconnecting to the stream mid-run (or after refresh) shows current progress, not a blank/stuck UI.

### Blocked by

- #2 (Mocked baseline graph + session create/detail).

---

## 4. Self-correcting quality loop (conditional routing)

**Phase:** P0 · **Type:** AFK

### What to build

The conditional-routing heart of the graph: a `quality_gate` node that grades the draft (LLM-judge +
deterministic checks) and either proceeds to `report` or loops back to `gather` for a **targeted**
re-research pass on weak questions. This is where `supersede_by_question` earns its keep — a
re-researched question replaces its whole prior fact set rather than accreting duplicates.

PRD refs: §6.1 (`quality_gate` routing), §6.2 (`quality`, `retries_left`, `weak_questions`),
§16 (τ=0.7, ≤1 extra pass), §6.3 (conditional routing requirement).

### Acceptance criteria

- [ ] `quality_gate` produces a `QualityVerdict(score, gaps[], weak_questions[])`.
- [ ] Routing: `score < τ AND retries_left > 0` → set `weak_questions`, `goto gather`; else → `report`.
- [ ] Threshold τ and max extra passes are **config-driven** (defaults τ=0.7, ≤1 extra pass); the loop is bounded (cannot spin).
- [ ] On loop-back, `gather` re-researches only `weak_questions`; `supersede_by_question` replaces the entire fact set for each re-researched `question_id` (no stale predecessors, `attempt` recorded).
- [ ] Retry budget spent → proceeds gracefully to `report` (no error).
- [ ] Test (deterministic mock): a low-scoring first draft triggers exactly one targeted re-research pass and the superseded facts are replaced, not duplicated.

### Blocked by

- #2 (Mocked baseline graph + session create/detail).

---

## 5. Idempotent persist & resume

**Phase:** P0 · **Type:** AFK

### What to build

Make persistence **convergent under resume/regenerate**, not merely duplicate-free, and expose the
resume entrypoint. `persist` runs as a single transaction that (a) upserts rows on deterministic ids +
unique constraints and (b) **replaces the full session materialization** — deletes `sources` no longer
present (cascading to their `facts` and `source_chunks`), replaces `report_chunks` — so a shrinking
regenerate leaves no orphans. Add `POST /sessions/{id}/resume` to re-enter the graph from the last good
checkpoint.

PRD refs: §10 (idempotency & replacement rules, FK cascades), §11 (idempotency & consistency),
§9.1 (resume lifecycle), §6.1 (`persist`).

### Acceptance criteria

- [ ] Every row uses a deterministic id from its natural key, backed by the `UNIQUE` constraints in §10.
- [ ] `persist` is a **single transaction** (relational rows together) — partial failure rolls back.
- [ ] Full-materialization replacement: `sources` absent from the new output are deleted, cascading to their `facts` and `source_chunks`; `report_chunks` for the session are replaced.
- [ ] `persist` bumps `reports.version` on regenerate (so chat staleness in #10 is detectable, §8.1).
- [ ] `POST /sessions/{id}/resume` re-enters the graph from the last good checkpoint to **attempt an upgrade** (`degraded`/`failed` → `done`, §9.1); resuming a `done` run is a no-op.
- [ ] Test: kill mid-run, then resume → **completed nodes are not re-executed** and there are **zero duplicate rows**.
- [ ] Test: re-research a question → **no stale facts** remain for that `question_id`.
- [ ] Test: a shrinking regenerate (drops a source / fewer sections) → **no orphan** `sources`, `facts`, `source_chunks`, or `report_chunks`.

### Blocked by

- #2 (Mocked baseline graph + session create/detail). Related: #4 (supersede-on-loop-back is the main re-research case these tests exercise).

---

## 6. Durable LLM-call ledger (token idempotency)

**Phase:** P0 · **Type:** AFK

### What to build

Guarantee that a mid-node kill re-pays no tokens and that concurrent resumes / double-submits don't
double-charge. Wrap every LLM / embedding / search call in a **durable ledger** keyed by
`(session_id, node, attempt, input_hash)` implementing reserve → (single-flight | lease takeover via
CAS) → call-with-provider-idempotency-key → commit, backed by durable storage so it survives a crash.

PRD refs: §6.5 (full protocol), §10 (`llm_calls` schema: status, idempotency_key, locked_by,
lease_expires_at), §11 (reliability).

### Acceptance criteria

- [ ] `llm_calls` ledger exists with `status ∈ {in_progress, done, failed}`, `idempotency_key`, `locked_by`, `lease_expires_at`, PK `(session_id, node, attempt, input_hash)`.
- [ ] **Reserve:** atomic insert of an `in_progress` row with owner token + lease; a duplicate key with a *valid* lease single-flights (waits); a `done` row returns the cached response with **no provider call**.
- [ ] **Takeover:** an `in_progress` row whose lease has **expired** is taken over by a waiter via compare-and-swap (new owner + renewed lease); the active owner heartbeats to extend its lease in-flight — **no deadlock**.
- [ ] **Provider idempotency key** = the ledger key, so a re-sent request after a crash/takeover is deduplicated by the provider within its window.
- [ ] **Commit:** response stored atomically (`status → done`); permanent failure sets `failed` so the key can be retried.
- [ ] Test `crash-after-reserve-before-call`: asserts lease takeover and no deadlock.
- [ ] Test `kill-before-commit`: asserts no double-charge (provider idempotency key honored).
- [ ] Test `concurrent-resume`: asserts single-flight (only one provider call for the same key).

### Blocked by

- #2 (Mocked baseline graph + session create/detail). Related: #5 (together these are the marquee kill-and-resume demo).

---

## 7. Real LLM + Tavily providers

**Phase:** P0 · **Type:** **HITL**

### What to build

Swap the mocks for production providers behind the existing interfaces: the `openai` SDK (Responses
API) for structured outputs per node, Tavily search + `httpx` site fetch behind `SearchProvider`,
boot-time model-id verification (fail-fast on an unknown id), an SSRF guard on website fetch, and
`RetryPolicy(max_attempts=3, retry_on=transient)` on the network-bound nodes. Recompute the
`<$0.01/run` budget from **live** pricing rather than asserting it.

> **HITL gate:** requires real `OPENAI_API_KEY` + `TAVILY_API_KEY` and a human decision on the live
> `gpt-5-*` model ids / pricing and the analyze-node step-up model (PRD §16 open item — earlier web
> pricing data is indicative only, not confirmed).

PRD refs: §0 (providers), §6.4 (budget), §15 (nano step-up for analyze), §11 (SSRF guard), §16 (model-id/pricing confirmation).

### Acceptance criteria

- [ ] Live `gpt-5-*` model ids confirmed against the API and recorded in config; boot **fails fast** on an unknown id.
- [ ] Per-node model + token caps are config-driven; analyze can step up to a stronger model via config only.
- [ ] LLM nodes use the `openai` Responses API for structured outputs; the call path goes through the slice-6 ledger (no double-charge on resume).
- [ ] The real embedding client (`text-embedding-3-small`) is wired behind its interface and id-verified at boot alongside the chat/report models (first exercised by #9).
- [ ] `SearchProvider` is backed by Tavily + `httpx` fetch; the abstraction stays swappable (mock still usable in tests).
- [ ] SSRF guard blocks private/internal IP ranges on website fetch.
- [ ] Transient failures retried per `RetryPolicy(max_attempts=3, retry_on=transient)`.
- [ ] A real run for a real company produces a cited 9-section report; measured cost/run is logged and compared to the recomputed budget.

### Blocked by

- #2 (Mocked baseline graph + session create/detail).
- #6 (Durable LLM-call ledger — #7's real call path must route through it). *Build order: durability before going live.*

---

## 8. Graceful degraded path & error taxonomy

**Phase:** P0 · **Type:** AFK

### What to build

Guarantee the system **never returns a 500** for a partial failure — it degrades honestly. Add an
`error_handler` that converts a `NodeError` on gather/extract/analyze into
`Command(goto="report", update={degraded: True})`, surface a `degraded` terminal status consistently
across graph/API/UI, map the error taxonomy to HTTP, and render a degraded banner with partial
sections instead of an error screen.

PRD refs: §6.1 (`error_handler`), §9 (error taxonomy → HTTP), §9.1 (run lifecycle & degraded contract),
§6.2 (`degraded`, `errors`), FE error/degraded states.

### Acceptance criteria

- [ ] `error_handler` on gather/extract/analyze routes a `NodeError` to `report` with `degraded=True` (honest partial report, never a 500).
- [ ] Terminal statuses `done | degraded | failed` are represented in both the graph `status` enum (§6.2) and the `sessions.status` column, and all three are resumable via `/resume`.
- [ ] SSE emits the final `{event:"end", status}` for terminal `degraded`/`failed` too.
- [ ] Error taxonomy → HTTP: transient → 503 + retry-after; validation → 422; degraded → 200 + `degraded:true`; not-found → 404.
- [ ] `GET /sessions/{id}` returns the partial report + `status` + `degraded`; the UI renders partial sections under a **degraded banner**, plus generic loading/error states.
- [ ] Test: an injected node failure yields a degraded terminal run (resumable) and a non-500 API response.

### Blocked by

- #2 (Mocked baseline graph + session create/detail). Related: #4, #6, #7.

---

## 9. pgvector chunking & embeddings on persist

**Phase:** P1 · **Type:** AFK

### What to build

Lay the semantic layer that chat and cross-session memory build on. Within the persist transaction,
chunk the report and sources and write embeddings (`text-embedding-3-small`) into the pgvector tables,
keeping the single-transaction guarantee (relational + vector together, or roll back).

PRD refs: §7 (layer 3 semantic), §10 (`report_chunks`, `source_chunks`), §11 (single-transaction persist).

### Acceptance criteria

- [ ] `report_chunks` and `source_chunks` tables exist with `vector(1536)` columns and the §10 unique constraints.
- [ ] Embedding generation goes through the slice-6 ledger (idempotent, not re-paid on resume).
- [ ] Chunking + embedding writes happen **inside the persist transaction** — a failure rolls back relational *and* vector writes together.
- [ ] After a successful run, chunks exist with embeddings for the report and each source.
- [ ] The shrink/replace rules from slice 5 hold for chunks (no orphan `report_chunks`/`source_chunks`).

### Blocked by

- #5 (Idempotent persist & resume — the single-transaction persist this extends).
- #6 (Durable LLM-call ledger — embeddings are generated through it).

---

## 10. Grounded follow-up chat

**Phase:** P1 · **Type:** AFK

### What to build

A grounded chat over the report + sources, as its own small `retrieve → answer` LangGraph (so it
inherits checkpointing + streaming). Retrieve top-k pgvector chunks scoped to the session plus the
last N turns; answer with the cheap model and **require `source_id` citations**; out-of-context
questions answer "not in the report." Persist messages with **self-contained citation snapshots** and
a `report_version` + `stale` flag so chat survives report regeneration.

PRD refs: §8 (chat loop), §8.1 (citation durability, stale signal), §9 (`/chat`, `/messages`),
§10 (`messages` schema).

### Acceptance criteria

- [ ] `retrieve → answer` chat graph retrieves top-k chunks scoped to `session_id` + last N turns; active retrieval uses only **live** sources.
- [ ] `POST /sessions/{id}/chat` streams the answer token-by-token; `GET /sessions/{id}/messages` returns history.
- [ ] Answers **must cite `source_id`s**; out-of-scope questions return "not in the report" (no fabrication).
- [ ] Each stored citation snapshots its source (`source_id` + `url` + `title` + `snippet`) at answer time, so `GET /messages` resolves even if that source is later dropped by a regenerate.
- [ ] Each message records the `report_version` it was grounded on; messages grounded on an older version are flagged `stale=true`.
- [ ] React chat UI: streamed answers, rendered citations, and a "report was updated — earlier answers may reference changed sources" notice for stale messages.

### Blocked by

- #9 (pgvector chunking & embeddings on persist).

---

## 11. Cross-session research memory (recall made real)

**Phase:** P1 · **Type:** AFK

### What to build

Make the `recall` node real (it has been a no-op since slice 2). Maintain a **self-contained**
`research_memory` store (its own provenance + `content_hash` + `fetched_at` + persisted `section` and
`freshness_class`; **no** foreign `source_id`/`question_id`). On a run, for each approved question,
match memory by `company_key`; if fresh (age < TTL by `freshness_class`) **remap** it into the live
session as a new session-scoped source + a question-owned fact; else **re-fetch** the url and
revalidate by `content_hash`. `persist` upserts `research_memory` for future warm-starts. Recall stays
positioned **after** `plan`.

PRD refs: §7.1 (full recall/freshness algorithm + invariant), §6.1 (`recall`), §10 (`research_memory`
schema), §16 (`company_key` = eTLD+1 fallback name; TTL 14d durable / 0–1d volatile).

### Acceptance criteria

- [ ] `research_memory` is self-contained per §10 (provenance + `content_hash` + `fetched_at` + `section` + `freshness_class`; unique on `(company_key, content_hash, fact_norm)`); **no** `source_id`/`question_id`.
- [ ] `company_key` = registrable domain (eTLD+1) of `website`, falling back to normalized company name.
- [ ] Fresh reuse (`now - fetched_at < TTL`) **remaps** memory into the session as a new `sources` row (new session-scoped `source_id`) + a fact bound to the matched **approved** `question_id`; memory facts matching no approved question are dropped.
- [ ] Stale reuse re-fetches the url: unchanged `content_hash` → refresh `last_validated_at` & reuse; changed → discard the stale fact and let `gather` re-derive.
- [ ] **Invariant test:** every claim in the report cites a `sources` row that exists *in this session* (recall never makes a report cite another session's row).
- [ ] `freshness_class` is stored (not inferred at read time); **acceptance test asserts the split** — volatile facts (Business Signals / Risks / news) are re-fetched while durable facts (Overview / Products) warm-start.
- [ ] `persist` upserts `research_memory` so a second run on the same company warm-starts.

### Blocked by

- #9 (pgvector chunking & embeddings on persist). Related: #7 (re-fetch/revalidate uses the real fetch path).

---

## 12. HITL plan-approval (interrupt → edit → resume)

**Phase:** P2 · **Type:** AFK

### What to build

Insert a human approval gate between `plan` and `recall`: the graph `interrupt()`s after planning,
the UI shows the proposed research questions for review/edit, and approval resumes the run with the
(optionally edited) questions. This flips "auto-approved" → "human-approved"; because `recall` was
already positioned after `plan`, approved `question_id`s exist before any binding.

PRD refs: §6.1 (`interrupt`), §6.3 (HITL), §4 step 3, §9 (`/approve-plan`), §13 P2,
§6.2 (`awaiting_approval` status).

### Acceptance criteria

- [ ] The graph `interrupt()`s after `plan` (between `plan` and `recall`); status becomes `awaiting_approval`.
- [ ] `POST /sessions/{id}/approve-plan` resumes with `Command(resume=edited_questions)`; questions retain stable `question_id`s.
- [ ] React: a plan-review screen lists the proposed questions, allows edit/add/remove, and an Approve action that continues the run (progress UI resumes via SSE).
- [ ] Acceptance: after approval, `recall` binds reused facts **only** to current-plan `question_id`s.
- [ ] Demo: start a run, pause at planning, edit a question, approve, and watch the run complete with the edited plan.

### Blocked by

- #3 (Live SSE progress + session history — the run must visibly pause and resume). Builds on #2.

---

## 13. Parallel `Send` fan-out

**Phase:** P2 · **Type:** AFK

### What to build

Replace the sequential `gather` with a `Send` fan-out over the research questions **not** already
answered by recall, gathering them in parallel. The `merge_by_id` reducer must keep concurrent merges
idempotent (dedupe `Source` rows by url while preserving the question→source links).

PRD refs: §6.1 (`Send` fan-out, merge_by_id), §6.3 (parallelism), §13 P2.

### Acceptance criteria

- [ ] `gather` fans out via `Send` over questions not answered by `recall`; runs concurrently.
- [ ] `merge_by_id` dedupes concurrent `Source` discoveries by url with **no duplicate** `sources` rows and preserved question→source links.
- [ ] Per-question `RetryPolicy(max_attempts=3, retry_on=transient)` still applies inside the fan-out.
- [ ] Test: concurrent gather over overlapping sources yields zero duplicate `sources` rows.
- [ ] Demo: a multi-question plan gathers in parallel (observably faster than sequential), report unchanged in correctness.

### Blocked by

- #5 (Idempotent persist & resume — relies on `merge_by_id` idempotency). Builds on #2.

---

## 14. Observability: Langfuse profile + structured logs

**Phase:** P3 · **Type:** AFK

### What to build

Make runs traceable and debuggable. Add Langfuse self-hosted behind an optional Docker `--profile obs`
(never blocking core dev), emit OTEL spans per node + per LLM call (token/cost/latency), and finalize
structured JSON logs carrying `session_id`/`node` and the error taxonomy.

PRD refs: §0 (observability), §11 (observability), §12 (`--profile obs` topology).

### Acceptance criteria

- [ ] `docker compose --profile obs up` adds `langfuse` (+ its `clickhouse`/`minio`) without affecting the default `docker compose up`.
- [ ] Each node and each LLM call emits an OTEL span with token/cost/latency attributes.
- [ ] A completed run appears in Langfuse as a trace with per-node spans and token/cost data.
- [ ] Structured JSON logs include `session_id` and `node` on every workflow log line, and tag errors by the §9 taxonomy.
- [ ] Core dev/tests run with the obs profile **off** (no hard dependency on Langfuse).

### Blocked by

- #2 (Mocked baseline graph + session create/detail).

---

## 15. Docs deliverables + responsive/polish pass

**Phase:** P3 · **Type:** **HITL**

### What to build

Produce the required submission documents and a final UI pass. Distill the existing artifacts into the
deliverables: `architecture.md` (from PRD), `engineering-decisions.md` (from `research.md`), a fresh
`README.md`, and `product-improvements.md` answering all 10 TASK prompts. Do a responsive-design pass
across the FE and finalize loading/error/degraded states.

> **HITL gate:** this is the portfolio-facing narrative for senior-eng / CTO readers — the user owns
> voice and emphasis and should review before it's considered done.

PRD refs: handoff doc convention (PRD→`architecture.md`, research.md→`engineering-decisions.md`);
TASK.md (`product-improvements.md` 10 questions; `engineering-decisions.md` 6 points; required docs);
FE responsive-design requirement.

### Acceptance criteria

- [ ] `README.md`: what it is, setup (`docker compose up`, `.env` keys), run instructions, and the kill/resume demo steps.
- [ ] `architecture.md`: system/graph architecture distilled from the PRD (not a copy — the reader-facing WHAT).
- [ ] `engineering-decisions.md`: 3 major decisions, alternatives, tradeoffs, top tech-debt items, biggest technical risk, and the "2 extra weeks" answer (TASK §engineering-decisions).
- [ ] `product-improvements.md` (≤2 pages): answers all 10 TASK product/business questions (≥5 weaknesses, top-3 next, buyer/user/why-pay, metrics, 4-week roadmap, risks, remove/add features, 90-day roadmap, first change).
- [ ] FE responsive across breakpoints; loading/error/degraded states verified end-to-end.
- [ ] No stale docs: deferred work matches the [Fast-follow backlog](#fast-follow-backlog-post-mvp--outside-the-gate).

### Blocked by

- Substantively all prior slices (docs must reflect the built system). Drafting can begin in parallel; final review is the human gate.

---

## Fast-follow backlog (post-MVP — outside the gate)

Explicitly **deferred** in PRD §13 / §0. Tracked here so they're not forgotten, but **not** part of the
MVP acceptance gate — do not start these until the 15 slices above are done.

- [ ] **Eval / golden-set harness in CI** — the documented fast-follow (PRD §0, §13). Regression-grade
      groundedness/completeness scoring on a fixed company set.
- [ ] **PDF export** of the briefing.
- [ ] **CRM write-back / push** integration.
- [ ] **Auth / multi-tenancy (RBAC)** — currently single-user, no auth by design (PRD §3 non-goals).

---

*Methodology: tracer-bullet vertical slices (one thin end-to-end path per slice), generated from
`PRD.md` via the `to-issues` skill. Update this file as slices complete — check the boxes and note any
scope changes back into the PRD if the contract itself changes.*
