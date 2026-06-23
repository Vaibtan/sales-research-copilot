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

- [x] `docker compose up` starts `api`, `web`, `postgres` (pgvector image), `redis` cleanly.
- [x] `GET /healthz` returns readiness reflecting **live** DB + Redis checks (not a static 200). *(verified: killing redis → 503 `redis:false`; restoring → 200)*
- [x] The web shell loads and renders API health (healthy / unhealthy) from `/healthz`.
- [x] Config is loaded via `pydantic-settings`; `.env.example` lists every required key with empty values; no secrets are committed. *(secret provider keys empty; non-secret DSNs carry dev defaults)*
- [x] `.gitignore` excludes `.env`; the repo has `backend/` and `frontend/` (or equivalent) structure.
- [x] Structured JSON logging is initialized (scaffold; full per-node fields land in slice 14).

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

- [x] `POST /sessions {company, website, objective}` creates a session row and starts the graph; returns `session_id`.
- [x] On create, `company_key` is computed and stored (eTLD+1 of `website`, fallback normalized company name) — needed by recall (#11), §16. *(tldextract bundled PSL; verified `stripe.com`/`acme.com`/`bbc.co.uk`)*
- [x] The graph runs all baseline nodes over `ResearchState`; `SearchProvider` and the LLM client are behind interfaces and **mocked** (deterministic fixtures).
- [x] Reducers behave per spec: `merge_by_id` dedupes `sources` by deterministic id; `supersede_by_question` is in place for `facts` (attempt-aware; unit-tested).
- [x] `PostgresSaver` writes a checkpoint per super-step (`AsyncPostgresSaver.setup()` run at boot; test asserts ≥8 checkpoints).
- [x] `persist` writes `sessions`, `reports` (1/session), `sources`, `facts` with their deterministic ids + unique constraints (single transaction; idempotent upserts).
- [x] `GET /sessions/{id}` returns the session + report when ready: all 9 sections, each claim carrying `source_id`(s), unsupported items in **Unknowns** (Risks gap, no fabrication), and a **Sources** list.
- [x] React: a create form (`company`, `website`, `objective`) and a detail page rendering the 9 sections with inline citations, Unknowns, and Sources (verified in-browser).
- [x] Graph integration test with deterministic mocks asserts a full report is produced and persisted. *(17 backend tests pass)*

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

- [x] `GET /sessions/{id}/stream` emits SSE events as nodes transition (`status` from §6.2: planning → gathering → analyzing → grading → reporting → done). *(runner streams `astream(stream_mode="values")` → publishes a `ProgressEvent` per super-step to Redis pub/sub; SSE endpoint subscribes. Verified via `test_sse_stream` + curl: statuses advance through the lifecycle.)*
- [x] The stream surfaces **intermediate outputs live** (plan questions, discovered sources, fact counts, draft), not just status — satisfying the §6.3 "intermediate outputs" requirement. *(each event carries plan question texts, discovered source titles/urls, `facts_count`, `draft_ready`; asserted in tests + seen in-browser.)*
- [x] The stream emits a final `{event:"end", status}` and then closes on any terminal status. *(SSE layer synthesizes `event: end` from a terminal `ProgressEvent.terminal`; generator returns/closes.)*
- [x] `GET /sessions` returns session history (list) ordered most-recent-first. *(`list_sessions` ORDER BY created_at DESC; `test_history_lists_most_recent_first`.)*
- [x] React shows live node-by-node progress driven by the SSE stream, with a loading state while the run is in flight. *(`useLiveSession` EventSource → `ProgressView` phase pipeline + live stat row; verified in-browser.)*
- [x] React history list links to each session detail page. *(`SessionHistory` on the home page; each row links to `/sessions/:id` — verified in-browser.)*
- [x] Reconnecting to the stream mid-run (or after refresh) shows current progress, not a blank/stuck UI. *(snapshot-on-connect from Redis `session:{id}:latest` set-with-TTL, falling back to the DB row; `test_reconnect_after_done_replays_snapshot` + in-browser on a non-terminal session shows the live pipeline, not blank.)*

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

- [x] `quality_gate` produces a `QualityVerdict(score, gaps[], weak_questions[])`. *(`make_quality_node` calls the judge with the approved questions in `QualityContext` so `weak_questions` come back as `question_id`s; verdict stored on `state["quality"]` and surfaced as `report.quality_score`.)*
- [x] Routing: `score < τ AND retries_left > 0` → set `weak_questions`, `goto gather`; else → `report`. *(node returns `Command[Literal["gather","report"]]` — no static `quality_gate → report` edge. Adds a defensive `AND weak_questions non-empty` guard: a sub-threshold score with no named weak question reports rather than accidentally re-researching everything — `test_low_score_without_weak_questions_does_not_loop`.)*
- [x] Threshold τ and max extra passes are **config-driven** (defaults τ=0.7, ≤1 extra pass); the loop is bounded (cannot spin). *(`settings.quality_threshold` / `settings.max_extra_passes` → `build_graph` → node; bound is `retries_left` not `recursion_limit` — `test_retry_budget_exhausted_proceeds_to_report`.)*
- [x] On loop-back, `gather` re-researches only `weak_questions`; `supersede_by_question` replaces the entire fact set for each re-researched `question_id` (no stale predecessors, `attempt` recorded). *(gather/extract already filter by `weak_questions`; quality decrements `retries_left` so the re-extract runs at `attempt=2`. Re-research yields a distinct claim → distinct fact id, so supersede visibly drops the attempt-1 fact rather than collapsing to a no-op — `test_low_score_triggers_exactly_one_targeted_repass` asserts the weak question is attempt-2-only while peers stay attempt-1.)*
- [x] Retry budget spent → proceeds gracefully to `report` (no error). *(`test_retry_budget_exhausted_proceeds_to_report`: scores `[0.5, 0.5]`, one extra pass, terminal `done` not `failed`.)*
- [x] Test (deterministic mock): a low-scoring first draft triggers exactly one targeted re-research pass and the superseded facts are replaced, not duplicated. *(`test_quality_loop.py` — 4 tests; full backend suite **25 pass** = prior 21 + 4.)*

> **Carry-forwards (not blocking; the deterministic mock does not exhibit them):**
> (a) On loop-back the weak question's old `assignments`/`sources` are *unioned*, not pruned — harmless here because the mock re-search returns the same urls (same ids → `merge_by_id`/`merge_assignments` dedupe), and `supersede_by_question` keeps the served *facts* correct. With *changing* real sources (slice 7) a superseded source could linger uncited in the in-state `sources` list; slice 5's full-materialization replacement (orphan deletion) is the persist-layer backstop. **This is Codex finding F1** — now an explicit slice-7 acceptance item (question-scoped assignment supersede + derive sources from surviving citations + the empty/different-re-pass regression); deferred to #7 per the 2026-06-22 decision since real changing sources are what trigger it.
> (b) The loop bound is `retries_left`; LangGraph's default `recursion_limit=25` is a far-off backstop that comfortably covers `max_extra_passes ≤ ~4`. Raise it in the runner config if that default ever grows.
> (c) In-app demo gap: the default mock always scores 1.0, so the loop can't be triggered in the running app until slice 7's real LLM can score low naturally (or a dev-only mock-score setting is added). Acceptance is the passing tests. *(User decision 2026-06-22: backend-only, no dev hook.)*

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

- [x] Every row uses a deterministic id from its natural key, backed by the `UNIQUE` constraints in §10. *(unchanged from slice 2 — `source.id=hash(session_id,url)`, `fact.id=hash(session_id,question_id,source_id,claim_norm)`, `report` unique on `session_id`. The replacement logic leans on these being session-scoped.)*
- [x] `persist` is a **single transaction** (relational rows together) — partial failure rolls back. *(all writes inside one `conn.transaction()`; prune + upsert commit together or roll back.)*
- [x] Full-materialization replacement: `sources` absent from the new output are deleted, cascading to their `facts` and `source_chunks`; `report_chunks` for the session are replaced. *(`DELETE … WHERE id <> ALL(new_ids)` for sources (cascades to facts/source_chunks) and facts (stale predecessors on surviving sources), then upsert; empty new set clears the table. `source_chunks`/`report_chunks` tables land in slice 9 — the same prune extends there.)*
- [x] `persist` bumps `reports.version` on regenerate (so chat staleness in #10 is detectable, §8.1). *(**Content-conditional** since the Codex-F2 fix: `ON CONFLICT (session_id) DO UPDATE SET version = CASE WHEN content_hash IS DISTINCT FROM EXCLUDED.content_hash THEN version+1 ELSE version END` — `reports.content_hash` (migration `0002`) fingerprints the served report (`report.model_dump()`; excludes `model_meta`/raw facts). A real regenerate bumps; an idempotent resume-replay of identical material does **not** (no false staleness). `test_persist_bumps_version_only_on_content_change`, `test_identical_repersist_does_not_bump_version`, `test_resume_replay_of_persist_does_not_bump_version`.)*
- [x] `POST /sessions/{id}/resume` re-enters the graph from the last good checkpoint to **attempt an upgrade** (`degraded`/`failed` → `done`, §9.1); resuming a `done` run is a no-op. *(`GraphRunner.resume` re-streams `astream(None)`; no-op when the checkpoint is at `END`. Service: 404 unknown · `done` short-circuits to no-op (even during the final-checkpoint flush window) · 409 if the run is still actively executing in-process · else re-enter. `test_resume_done_session_is_noop`, `test_resume_unknown_session_returns_404`, `test_runner_resume_upgrades_unfinished_run`.)*
- [x] Test: kill mid-run, then resume → **completed nodes are not re-executed** and there are **zero duplicate rows**. *(`test_kill_midrun_then_resume_completes_without_reexecuting`: `interrupt_after=["extract"]` commits a partial checkpoint, resume completes it, `llm.calls[PLAN] == 1` proves `plan` wasn't re-paid, row counts equal the report's source/fact sets.)*
- [x] Test: re-research a question → **no stale facts** remain for that `question_id`. *(`test_no_stale_facts_after_research_then_regenerate`: a reworded claim earns a new id; the predecessor on the surviving source is deleted.)*
- [x] Test: a shrinking regenerate (drops a source / fewer sections) → **no orphan** `sources`, `facts`, `source_chunks`, or `report_chunks`. *(`test_shrinking_regenerate_leaves_no_orphans` covers sources+facts (cascade); chunk tables don't exist until slice 9, which re-asserts the chunk half.)* **Backend suite now 32 pass** (prior 25 + 7).

> **Carry-forwards / notes:**
> (a) **Chunk replacement is deferred to slice 9** — `source_chunks`/`report_chunks` tables don't exist yet; the source-delete already cascades to `source_chunks` (FK) once they do, and `report_chunks` get an explicit replace then.
> (b) **Resume's busy-guard is in-process only.** The Codex-F2/F4 pass hardened it: the check-and-reserve is now **atomic** — `GraphRunner.resume` adds the session to `_active` *synchronously before any `await`* and raises `SessionBusyError` on a duplicate (the old `service.is_active()`-then-`await resume()` TOCTOU is gone; `test_concurrent_resume_single_flights`). This still only single-flights within one API instance; cross-process / cross-instance single-flight needs the slice-6 durable lease (`llm_calls.locked_by`/`lease_expires_at`).
> (c) **Graceful drain on shutdown.** `runner.shutdown()` now waits a grace window for in-flight runs to checkpoint cleanly before cancelling, so the checkpointer pool isn't yanked out mid-`aput` (was surfacing as `PoolClosed` at test teardown). Stragglers past the window are still cancelled (resumable, since LangGraph checkpoints per super-step).
> (d) **No FE this slice** (consistent with slice 4): a Resume button + degraded banner pair naturally with slice 8's degraded path, which is also what first produces a resumable in-app state over the mocks (the mock run always succeeds).
> (e) **Test gotcha (not a code bug):** the long-lived dev DB means tests must use *session-scoped* row ids — a bare `"s1"` collides with a prior run's row under the global PK, and `ON CONFLICT … DO UPDATE` (which doesn't rewrite `session_id`) then leaves the new session empty. Production ids are `hash(session_id, …)`, so this only bites hand-written fixtures.

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

- [x] `llm_calls` ledger exists with `status ∈ {in_progress, done, failed}`, `idempotency_key`, `locked_by`, `lease_expires_at`, PK `(session_id, node, attempt, input_hash)`. *(migration `0003_llm_calls.sql`; `CHECK` on status; no FK on session_id so the ledger is keyed independently of the relational materialization.)*
- [x] **Reserve:** atomic insert of an `in_progress` row with owner token + lease; a duplicate key with a *valid* lease single-flights (waits); a `done` row returns the cached response with **no provider call**. *(`CallLedger._reserve` = `INSERT … ON CONFLICT DO NOTHING RETURNING`; valid-lease waiters poll; `done` short-circuits — `test_done_call_returns_cache_without_reinvoking_provider`.)*
- [x] **Takeover:** an `in_progress` row whose lease has **expired** is taken over by a waiter via compare-and-swap (new owner + renewed lease); the active owner heartbeats to extend its lease in-flight — **no deadlock**. *(`_take_expired` CAS on `locked_by IS NOT DISTINCT FROM <observed>` + `lease_expires_at <= now()`; `_heartbeat` extends the lease while producing; `test_crash_after_reserve_before_call_takes_over_without_deadlock` via `asyncio.wait_for`.)*
- [x] **Provider idempotency key** = the ledger key, so a re-sent request after a crash/takeover is deduplicated by the provider within its window. *(`LedgerKey.idempotency_key` = digest of the 4-tuple; the real OpenAI client passes it through — slice 7.)*
- [x] **Commit:** response stored atomically (`status → done`); permanent failure sets `failed` so the key can be retried. *(`_commit` guarded on `locked_by=owner` (a taken-over owner can't overwrite); `_fail` on exception; `test_failed_row_is_retryable`.)*
- [x] Test `crash-after-reserve-before-call`: asserts lease takeover and no deadlock.
- [x] Test `kill-before-commit`: asserts no double-charge (provider idempotency key honored). *(`_FakeProvider` charges once per idempotency key; takeover re-sends the same key → `charges == 1`.)*
- [x] Test `concurrent-resume`: asserts single-flight (only one provider call for the same key). *(`test_concurrent_resume_single_flights`: 3 concurrent `run`s → 1 provider call.)*

> **Carry-forwards / notes:** lease math is evaluated **DB-side** (`now()`) to dodge app/DB clock skew; each ledger step is its own committed statement (not one long txn) so cross-connection waiters observe transitions immediately. The graph routes **LLM** calls through the ledger now (`build_graph(ledger=…)` wraps the client in `LedgerLLMClient`; nodes pass `CallSite(session_id, node, attempt)`); **search** (slice 7) and **embedding** (slice 9) calls route through the same ledger as those providers land. Backend suite **41 pass** (prior 35 + 6).

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

- [x] Live `gpt-5-*` model ids confirmed against the API and recorded in config; boot **fails fast** on an unknown id. *(`verify_models` retrieves each configured id; `ModelVerificationError` on 404; called in lifespan when keys present, gated by `verify_models_on_boot`. `test_verify_models_*`.)*
- [x] Per-node model + token caps are config-driven; analyze can step up to a stronger model via config only. *(`LLMRouting.from_settings`; `model_analyze` defaults to `gpt-5` while others are `gpt-5-nano`; per-task `max_output_tokens_*`.)*
- [x] LLM nodes use the `openai` Responses API for structured outputs; the call path goes through the slice-6 ledger (no double-charge on resume). *(`OpenAILLMClient.parse` → `responses.parse(text_format=…)` → `.output_parsed`; wrapped by `LedgerLLMClient` in `build_graph`. `test_openai_parse_*`.)*
- [x] The real embedding client (`text-embedding-3-small`) is wired behind its interface and id-verified at boot alongside the chat/report models (first exercised by #9). *(`EmbeddingClient` protocol + `OpenAIEmbeddingClient` + `MockEmbeddingClient`; embedding id added to the boot-verify set; on `app.state.embeddings`.)*
- [x] `SearchProvider` is backed by Tavily + `httpx` fetch; the abstraction stays swappable (mock still usable in tests). *(`TavilySearchProvider` maps results → `SearchHit`, best-effort SSRF-guarded `httpx` content enrichment; mock remains the test/default backend. `test_tavily_*`.)*
- [x] SSRF guard blocks private/internal IP ranges on website fetch. *(`fetch.assert_public_url` resolves the host and rejects private/loopback/link-local/reserved/multicast — incl. `169.254.169.254` metadata; `test_ssrf_guard_*`.)*
- [x] Transient failures retried per `RetryPolicy(max_attempts=3, retry_on=transient)`. *(`build_graph` sets `RetryPolicy(max_attempts=retry_max_attempts, retry_on=is_transient)` on plan/gather/extract/analyze; `is_transient` covers httpx/openai connection/timeout/429/5xx — `test_is_transient_classification`.)*
- [ ] A real run for a real company produces a cited 9-section report; measured cost/run is logged and compared to the recomputed budget. *(**HITL — requires live `OPENAI_API_KEY`+`TAVILY_API_KEY`** per the build decision: the full live path + per-call token-usage logging (`llm.usage`) are built, but a live run/cost-measurement is a manual step, not run in CI. Mock stays default.)*
- [x] **Codex F1 — loop-back evidence replacement (deferred here from slice 4).** Real, *changing* sources first exercise this gap: on a quality loop-back `gather` re-emits only the weak question's hits, but `sources`/`assignments` are append/dedupe-merged (`merge_by_id`/`merge_assignments`), so `extract` re-runs attempt-2 over the weak question's *stale* attempt-1 sources too and they survive into report/persist — violating the §6.2/§10 per-question replacement contract. **Fix:** give `QuestionSource` an `attempt`/batch marker and add a question-scoped *supersede* reducer for `assignments` (mirror `supersede_by_question`, keyed by `question_id`), so a re-research drops the question's older assignments; then derive the final `sources` from surviving fact citations (drop sources no longer cited). The deterministic mock hides this (re-search returns identical urls → same ids → dedupe is a no-op). **Regression:** make the re-pass return *empty/different* hits for the weak question and assert the old facts **and** their now-uncited sources are absent. *(Was slice-4 carry-forward (a); deferred to #7 per 2026-06-22 decision because real changing sources are what actually trigger it.)* **DONE:** `QuestionSource.attempt` + `supersede_assignments_by_question` reducer (mirrors `supersede_by_question`); `gather` stamps the attempt; `report_node` derives the served Sources from surviving fact citations and `persist` writes that cited set (so the orphan is pruned). Regression `test_loopback_evidence.py::test_loopback_replaces_stale_sources_and_facts` drives a `ShiftingSearch` (different urls per pass) + weak-on-pass-1 judge → asserts weak-question facts cite only pass-2 sources, the pass-1 source is gone from the report **and** DB, and no orphan source rows remain.

> **Carry-forwards / notes:** the real backends are **built but mock stays default** — live mode activates only when both `OPENAI_API_KEY`+`TAVILY_API_KEY` are set (decision 2026-06-23). The suite is **hermetic** (conftest force-empties the provider keys) so it never makes a live call; the real adapters are unit-tested via injected SDK-shaped fakes (no network). The cost/budget acceptance box stays open pending a manual live run. Backend suite **65 pass** (prior 41 + 24).

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

- [x] `error_handler` on gather/extract/analyze routes a `NodeError` to `report` with `degraded=True` (honest partial report, never a 500). *(`make_error_handler` (langgraph 1.2.6 `error_handler=`) → `Command(goto="report", update={degraded, errors})`; `report_node` assembles an honest partial from surviving facts via `_degraded_draft` when no draft exists. `test_node_failure_degrades_to_partial_report`.)*
- [x] Terminal statuses `done | degraded | failed` are represented in both the graph `status` enum (§6.2) and the `sessions.status` column, and all three are resumable via `/resume`. *(enum unchanged; `sessions.status` is TEXT; runner sets `failed` on an uncaught node error; all three route through `service.resume` → `runner.resume`.)*
- [x] **Codex F3 — degraded/failed-at-END are actually upgradable (deferred here from slice 5).** *(**DONE:** the no-op gate moved to persisted status `done`; `service.resume` builds a fresh initial state and `runner.resume(fresh_state=…)` re-invokes from START for a degraded/failed run at END (empirically confirmed LangGraph restarts a completed thread on fresh input; the ledger means successful calls aren't re-paid). `test_degraded_at_end_resume_upgrades_to_done` drives degrade → release → resume → done.)* Today `GraphRunner.resume` returns `False` (no-op) whenever the checkpoint has no `next`, and the service only special-cases `done`. Once this slice's `error_handler` drives a degraded partial report all the way to `report → persist → END`, that terminal run sits at `next == ()` with status `degraded`, so `/resume` wrongly reports `resumed:false` — contradicting §9.1 (degraded/failed are resumable to attempt an upgrade). **Fix:** gate the resume no-op on the *persisted status* (`done` → no-op) rather than on `next == ()`; for a `degraded`/`failed`-at-END run, either leave a resumable target before fallback reporting (don't checkpoint straight to END) **or** start a fresh upgrade attempt from persisted session state (re-invoke from a rebuilt initial state on the thread; re-pays tokens until the slice-6 ledger lands). **Tests:** degraded-at-END → `/resume` schedules work (not a silent no-op); failed-at-END likewise. *(Was implicit in slice-5's resume contract; deferred to #8 per 2026-06-22 decision because the degraded path that produces such states is built here.)*
- [x] **Codex F5 — resume must not replay a stale terminal SSE snapshot (deferred here from the 2026-06-23 re-review).** *(**DONE:** `runner.resume` sets a non-terminal DB status **and** publishes a non-terminal "resumed" `ProgressEvent` (set-then-publish overwrites the cached terminal snapshot) before returning, and starts the resumed run's `step` **above** the previous latest (monotonic across attempts). `stream_session` re-checks the fresh DB status before honoring a cached *terminal* snapshot. `test_resume_publishes_nonterminal_resumed_snapshot` (deterministic: first event after resume is non-terminal) + `test_resume_does_not_replay_stale_terminal_end` (reconnect ends on `done`, not stale `degraded`).)* This is the *streaming* half of F3 and only triggers once a run can fail/degrade (which lands in this slice). Two coupled defects in `stream_session` (`backend/app/api/routes/sessions.py`): (1) on reconnect it prefers the cached `bus.latest(session_id)` and ends the stream when that snapshot is `terminal`, *without* re-checking the fresh DB status — so after a `failed`/`degraded` snapshot is cached, a `/resume` that flips the status non-terminal and spawns work still serves the old terminal snapshot + `event:end` to a reconnecting client; (2) the resumed `_run` restarts `step = 0`, and the SSE loop drops `event.step <= last_step`, so resumed progress is hidden behind the prior run's higher step. **Fix:** on resume, synchronously invalidate/replace the cached latest snapshot with a non-terminal "resumed" snapshot before `GraphRunner.resume` returns; make progress ordering monotonic across resume attempts (a `run_attempt`/epoch on `ProgressEvent`, or initialize `step` above the previous latest); and have the stream ignore a cached terminal snapshot when the fresh DB status is non-terminal. **Regression:** create a failed run with a cached terminal snapshot, call `/resume`, immediately reconnect to `/stream`, assert no stale `end` is emitted while the runner is active. *(F2/F4 confirmed closed and F1/F3 deferrals accepted by the same re-review; verdict needs-attention solely on this finding.)*
- [x] SSE emits the final `{event:"end", status}` for terminal `degraded`/`failed` too. *(degraded reaches END normally → the final streamed state is terminal → SSE `end`; a `failed` crash publishes a terminal `failed` event from `runner._run`. `test_resume_does_not_replay_stale_terminal_end` sees `end:degraded` then `end:done`.)*
- [x] Error taxonomy → HTTP: transient → 503 + retry-after; validation → 422; degraded → 200 + `degraded:true`; not-found → 404. *(`app/api/errors.py` maps psycopg/redis transients → 503 + `Retry-After`, `ProviderError` → 502, `SsrfError` → 400; validation 422 (pydantic) + not-found 404 (HTTPException) unchanged. `test_transient_db_error_maps_to_503`, `test_degraded_run_returns_200_with_flag`.)*
- [x] `GET /sessions/{id}` returns the partial report + `status` + `degraded`; the UI renders partial sections under a **degraded banner**, plus generic loading/error states. *(detail already carries status+degraded+report; FE degraded banner + a **Resume button** (degraded *and* failed states) that calls `/resume` then re-attaches the live stream via `useLiveSession.reload()`. tsc + vite build clean.)*
- [x] Test: an injected node failure yields a degraded terminal run (resumable) and a non-500 API response. *(`test_node_failure_degrades_to_partial_report` + `test_degraded_run_returns_200_with_flag`.)*

> **Carry-forwards / notes:** FE resume/degraded verified by tsc + production build; a consolidated in-browser pass lands in slice 15. Backend suite **72 pass** (prior 65 + 7). F3/F5 close the last deferred Codex findings — all of F1–F5 now resolved.

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

- [x] `report_chunks` and `source_chunks` tables exist with `vector(1536)` columns and the §10 unique constraints. *(migration `0004_chunks.sql`; HNSW cosine indexes; FK cascades to sessions/sources.)*
- [x] Embedding generation goes through the slice-6 ledger (idempotent, not re-paid on resume). *(`LedgerEmbeddingClient` wraps the embedding client in `build_graph`; persist embeds via `CallSite(session_id,"embed",1)` → `llm_calls` rows. `test_run_persists_chunks_with_embeddings_via_ledger` asserts a `done` `node='embed'` row.)*
- [x] Chunking + embedding writes happen **inside the persist transaction** — a failure rolls back relational *and* vector writes together. *(`persist_results` writes chunk rows in the same `conn.transaction()` as sessions/reports/sources/facts; embeddings are generated before the txn (ledger-cached) and the vectors written within it.)*
- [x] After a successful run, chunks exist with embeddings for the report and each source. *(`vector_dims(embedding) == 1536` for report chunks; one+ `source_chunks` per served source.)*
- [x] The shrink/replace rules from slice 5 hold for chunks (no orphan `report_chunks`/`source_chunks`). *(prune-then-upsert by deterministic id extends to both chunk tables; a dropped source cascades its `source_chunks`. `test_shrinking_regenerate_leaves_no_orphan_chunks`, `test_dropping_source_cascades_to_its_chunks`.)*

> **Notes:** vectors are stored via pgvector's text input (`%s::vector`) — no extra driver adapter/dep. Embeddings stored in the ledger as JSONB (size tradeoff accepted for MVP). Resolves the slice-5 chunk carry-forward (a). Backend suite **79 pass** (prior 72 + 7).

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

- [x] `retrieve → answer` chat graph retrieves top-k chunks scoped to `session_id` + last N turns; active retrieval uses only **live** sources. *(`graph/chat.py` 2-node LangGraph; `retrieve_chunks` cosine `<=>` over `report_chunks` ∪ `source_chunks` scoped to the session (live via FK cascade); last 8 turns in `ChatContext`.)*
- [x] `POST /sessions/{id}/chat` streams the answer token-by-token; `GET /sessions/{id}/messages` returns history. *(`ChatService.answer_stream` yields `token` SSE frames then a `done` frame; `get_messages` returns the ordered history. `test_chat_in_scope_answer_is_cited`, `test_messages_history_records_user_and_assistant`.)*
- [x] Answers **must cite `source_id`s**; out-of-scope questions return "not in the report" (no fabrication). *(mock chat answers only from passages sharing a content word and *requires* a citable source; else declines. `test_chat_out_of_scope_declines`. Real CHAT prompt enforces the same.)*
- [x] Each stored citation snapshots its source (`source_id` + `url` + `title` + `snippet`) at answer time, so `GET /messages` resolves even if that source is later dropped by a regenerate. *(`resolve_citations` snapshots into `citations_jsonb`; `test_citation_snapshot_survives_source_drop_and_marks_stale` drops every source then asserts the snapshot still resolves.)*
- [x] Each message records the `report_version` it was grounded on; messages grounded on an older version are flagged `stale=true`. *(`persist_results` flips `stale` on messages below the current `reports.version` when it bumps; same test asserts `stale=true` after a regenerate.)*
- [x] React chat UI: streamed answers, rendered citations, and a "report was updated — earlier answers may reference changed sources" notice for stale messages. *(`ChatPanel`: POST-SSE via `fetch` stream reader (EventSource is GET-only), optimistic bubbles, citation chips, per-message stale notice. tsc + vite build clean.)*

> **Notes:** chat uses the **raw** providers (not ledgered — it isn't a resumable run). The mock embedding is a deterministic **bag-of-words** vector (hashing trick) so retrieval is semantically meaningful offline (a "products" question surfaces the products chunk). Backend suite **84 pass** (prior 79 + 5).

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

- [x] `research_memory` is self-contained per §10 (provenance + `content_hash` + `fetched_at` + `section` + `freshness_class`; unique on `(company_key, content_hash, fact_norm)`); **no** `source_id`/`question_id`. *(migration `0006_research_memory.sql`; HNSW cosine index; `id = digest(company_key, content_hash, fact_norm)`.)*
- [x] `company_key` = registrable domain (eTLD+1) of `website`, falling back to normalized company name. *(unchanged from slice 2 — `domain/company.py`.)*
- [x] Fresh reuse (`now - fetched_at < TTL`) **remaps** memory into the session as a new `sources` row (new session-scoped `source_id`) + a fact bound to the matched **approved** `question_id`; memory facts matching no approved question are dropped. *(`make_recall_node`: per approved question, `match_memory` (section-scoped + nearest embedding ≤ distance) → remap fresh; no match → drop. `test_fresh_memory_warm_starts_and_invariant_holds`.)*
- [x] Stale reuse re-fetches the url: unchanged `content_hash` → refresh `last_validated_at` & reuse; changed → discard the stale fact and let `gather` re-derive. *(stale → `revalidator(url)` (real fetch path; `None` for mock) → `content_hash` match → `touch_memory_validated` + remap, else `continue` so `gather` re-derives.)*
- [x] **Invariant test:** every claim in the report cites a `sources` row that exists *in this session* (recall never makes a report cite another session's row). *(remapped source id = `source_id(session_id, url)`; the invariant test asserts every report source id equals the session-scoped digest.)*
- [x] `freshness_class` is stored (not inferred at read time); **acceptance test asserts the split** — volatile facts (Business Signals / Risks / news) are re-fetched while durable facts (Overview / Products) warm-start. *(`test_freshness_split_volatile_refetched_durable_warmstarted`: durable Overview (fresh) → memory url; volatile Business Signals (2d old, TTL 1d, no revalidator) → re-derived via gather url.)*
- [x] `persist` upserts `research_memory` so a second run on the same company warm-starts. *(persist embeds each fact claim (ledgered) → `MemoryRow` upsert in the persist txn; `test_persist_upserts_memory_for_future_warmstart`: cold run → memory rows → second run `reused_from_memory=True`.)*

> **Notes:** binding is **section-scoped semantic match** (nearest embedding *within the question's section*) — a refinement of §7.1 that prevents a Products fact from ever binding to the Risks question (which would fabricate a section that must stay an honest Unknown). `gather` skips recall-`answered_questions` on the first pass (§6.1). Test isolation: an autouse fixture truncates the cross-session `research_memory` per test (the long-lived-DB gotcha, now for the company-keyed table). Backend suite **87 pass** (prior 84 + 3).

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

- [x] The graph `interrupt()`s after `plan` (between `plan` and `recall`); status becomes `awaiting_approval`. *(new `await_approval` node calls `interrupt(proposed)`; `plan` sets status `awaiting_approval` when the gate is on; the runner persists the paused phase after `astream` returns. Always-present pass-through when off, so the topology is resume-stable. `test_run_pauses_for_approval_and_exposes_plan`.)*
- [x] `POST /sessions/{id}/approve-plan` resumes with `Command(resume=edited_questions)`; questions retain stable `question_id`s. *(`runner.approve_plan` re-enters with `Command(resume=…)`; `_apply_plan_edits` keeps a kept question's id and mints fresh ids for added ones — `test_apply_plan_edits_keeps_stable_ids_and_mints_new`.)*
- [x] React: a plan-review screen lists the proposed questions, allows edit/add/remove, and an Approve action that continues the run (progress UI resumes via SSE). *(`PlanReview` (GET `/plan` → editable rows + section selects + add/remove) → `approvePlan` → `useLiveSession.reload()` re-attaches the stream. tsc + vite build clean.)*
- [x] Acceptance: after approval, `recall` binds reused facts **only** to current-plan `question_id`s. *(recall runs *after* `await_approval`, iterating `state["plan"].questions` — the approved set — so a remapped fact can only own an approved id; section-scoped match further guards it.)*
- [x] Demo: start a run, pause at planning, edit a question, approve, and watch the run complete with the edited plan. *(`test_approve_edited_plan_drives_the_run`: dropping the Products question → that section is an honest Unknown in the final report, proving the edited plan drove the run.)*

> **Notes:** `hitl_plan_approval` is config-driven (default **on** in the app for the demo; `.env` toggles it). The general test suite forces it **off** (conftest) so P0/P1 flows stay straight-through; `test_hitl.py` exercises the on-path. `/approve-plan` is single-flight (reuses the `SessionBusyError` guard) and 409s if the session isn't awaiting approval. Backend suite **91 pass** (prior 87 + 4).

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

- [x] `gather` fans out via `Send` over questions not answered by `recall`; runs concurrently. *(`gather` is now a dispatcher returning `Command(goto=[Send("gather_one", payload) …])` (or straight to `extract` when all recalled); `gather_one` is the per-question worker. Loop-back fans out only the weak questions.)*
- [x] `merge_by_id` dedupes concurrent `Source` discoveries by url with **no duplicate** `sources` rows and preserved question→source links. *(`test_concurrent_fanout_dedupes_overlapping_sources`: a shared url returned by every worker → one source row, ≥2 question→source links, `count == count(distinct)`.)*
- [x] Per-question `RetryPolicy(max_attempts=3, retry_on=transient)` still applies inside the fan-out. *(a node-level `error_handler`/`retry_policy` doesn't apply to `Send` workers, so `gather_one` **self-handles**: an internal `is_transient` retry loop, then graceful degrade (no sources + `degraded=True`) — the other questions' evidence still flows.)*
- [x] Test: concurrent gather over overlapping sources yields zero duplicate `sources` rows. *(as above.)*
- [x] Demo: a multi-question plan gathers in parallel (observably faster than sequential), report unchanged in correctness. *(parallelism is structural via `Send`; report correctness asserted unchanged in the fan-out test + the full suite.)*

> **Notes:** `degraded` became `Annotated[bool, keep_degraded]` (OR-merge) so concurrent failing workers don't collide on a single-value channel. The **F3 fresh-upgrade now deletes the completed thread's checkpoint** before re-invoking (`checkpointer.adelete_thread`) — a same-thread re-invoke otherwise *merges* reducer channels (sources/facts/errors/degraded) with the old run, so the upgrade could never shed the prior degraded state (verified empirically). Workers write only the reducer-merged channels (not `status`, a single-value channel). Backend suite **92 pass** (prior 91 + 1).

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

- [x] `docker compose --profile obs up` adds `langfuse` (+ its `clickhouse`/`minio`) without affecting the default `docker compose up`. *(langfuse-web/worker + langfuse-postgres + clickhouse + minio under `profiles: ["obs"]`; verified `docker compose config --services` = {api,postgres,redis,web} default vs +5 obs services with the profile.)*
- [x] Each node and each LLM call emits an OTEL span with token/cost/latency attributes. *(`_instrument` wraps every node in `span("node.{name}")`; `LedgerLLMClient`/`LedgerEmbeddingClient` open `llm.{task}`/`embed` spans; `OpenAILLMClient` annotates the span with `llm.input_tokens`/`output_tokens`. Spans capture latency inherently; nodes also log `duration_ms`.)*
- [ ] A completed run appears in Langfuse as a trace with per-node spans and token/cost data. *(**HITL/manual** — requires the obs profile + Langfuse running + keys, which CI doesn't run. The OTLP→Langfuse exporter + span instrumentation are built; a no-op exporter in tests means this is verified by inspection, not CI.)*
- [x] Structured JSON logs include `session_id` and `node` on every workflow log line, and tag errors by the §9 taxonomy. *(`_instrument` binds `session_id`+`node` via structlog contextvars (merged into every line inside the node); `api/errors.py` tags each handler `taxonomy=transient|provider|validation`. `test_instrument_binds_contextvars_for_inner_logs`, `test_instrument_emits_node_log_with_session_and_duration`.)*
- [x] Core dev/tests run with the obs profile **off** (no hard dependency on Langfuse). *(`otel_enabled` defaults False → `configure_tracing` leaves the no-op OTEL provider; spans/attrs are safe no-ops. `test_tracing_disabled_without_config`, `test_span_and_attrs_are_safe_without_provider`.)*

> **Notes:** instrumented with the OTEL **API** everywhere so spans are cheap no-ops until `configure_tracing` installs the OTLP→Langfuse provider (lazy SDK import). Langfuse v3 ingests OTLP directly (`/api/public/otel/v1/traces`), so no langfuse SDK dep — just `opentelemetry-{api,sdk,exporter-otlp-proto-http}`. Langfuse reuses the shared redis (db 1) with its own postgres/clickhouse/minio. Backend suite **98 pass** (prior 92 + 6).

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
