# PRD — AI Sales Research Copilot

> *"Your sellers run the conversation. We do everything else."*
> A production-minded research copilot that turns `(company, website, objective)` into a
> grounded, cited sales briefing via a durable, self-correcting LangGraph workflow — with
> follow-up chat over the report.

**Owner:** vaibtan · **Status:** approved scope (rev 2 — passed adversarial review, no open findings) · **Date:** 2026-06-18
**Companion docs:** [`research.md`](./research.md) (architecture exploration & rationale)

---

## 0. Locked decisions (from grilling)

| Decision | Choice |
| :--- | :--- |
| Orchestrator | **LangGraph only** (single control plane). Raw `openai` SDK (Responses API) inside nodes. **No** OpenAI Agents SDK, **no** LangChain in the hot path. |
| Search/data source | **Tavily** behind a swappable `SearchProvider` interface (+ `httpx` site fetch). |
| Models | **`gpt-5-nano`-class default**, tiered & config-driven per node; model IDs are config placeholders verified against the live API at boot (fail-fast on invalid id). `text-embedding-3-small` for vectors. |
| Memory | **3 layers**: LangGraph Postgres checkpoint (run state) · relational Postgres (product data) · pgvector (chat RAG + cross-session memory). |
| MVP extensions | **HITL plan-approval gate · cross-session research memory · parallel fan-out (`Send`)** — on top of the baseline self-correcting graph. **All required** by the MVP acceptance gate (§13); phases are build order, not optional tiers. |
| Frontend | **Clean & functional**: Vite + React + TS + Tailwind; live SSE progress; report w/ citations; chat; loading/error/degraded states. |
| Auth | **None** (single-user, global sessions). |
| Observability | **Langfuse self-hosted as an optional Docker `profile`**; OTEL spans per node + LLM call. |
| Eval harness | **Deferred** to post-MVP (documented fast-follow). |

---

## 1. Problem & vision

Sellers (SDRs/AEs) waste 30–60 min before every meeting hunting across a company's site, news,
and LinkedIn to assemble context — and the output is inconsistent and rarely cited. **The copilot
compresses that into a 1–2 minute automated research run** that produces a structured, *grounded*
briefing (every claim traceable to a source) plus a chat to drill in. The differentiator vs. a
"ChatGPT, research Acme" prompt is **grounding, structure, durability, and memory**: it cites
sources, it survives failures, and it remembers prior research.

## 2. Users & value

| Persona | Job-to-be-done | Why they'd pay |
| :--- | :--- | :--- |
| **SDR / AE (primary user)** | Prep for a specific meeting fast | Saves 30–60 min/meeting; walks in with discovery questions + outreach angle |
| **Sales manager (buyer)** | Consistent, high-quality prep across the team | Standardized briefings, ramp new reps faster, pipeline quality |
| **RevOps (admin)** | Tooling that plugs into the workflow | Auditable, governable, integrates with CRM |

**Who buys vs. who uses:** sales managers / RevOps buy (seat-based or usage-based); SDRs/AEs use.
They pay because it converts unstructured prep time into a repeatable, cited asset — time saved is
directly attributable to revenue activity.

## 3. Goals / non-goals

**Goals (MVP)**
- One-shot: `(company, website, objective)` → all 9 report sections, each with inline citations + honest Unknowns.
- A LangGraph workflow that genuinely uses: multiple nodes, shared state, conditional routing, intermediate outputs, **failure handling**, **recoverability** (checkpoint + resume).
- Durable, observable, cost-controlled, dockerized; clean API contracts; grounded follow-up chat.

**Non-goals (MVP)**
- Auth / multi-tenant RBAC. CRM write-back. PDF export. Mobile-native. Team collaboration. Fine-tuning. The eval harness (deferred).

## 4. Core user flow

1. Create a research session (`company`, `website`, `objective`).
2. Workflow runs; UI shows **live node-by-node progress** (SSE).
3. **(HITL)** After planning, the user can review/edit the research questions, then approve to continue.
4. Structured report renders with citations + Unknowns + Sources.
5. Follow-up **chat** answers grounded in the report + sources (with citations).
6. Everything persists; **session history**; a killed/failed run is **resumable**.

## 5. The report (required output contract)

Nine sections, all citation-backed where claims are made; unsupported items go to **Unknowns** (no fabrication):
`Company Overview · Products & Services · Target Customers · Business Signals · Risks & Challenges ·
Suggested Discovery Questions · Suggested Outreach Strategy · Unknowns · Sources`.

Stored as structured JSON so the shape can evolve without migrations; each claim references `source_id`s.

---

## 6. The LangGraph workflow (the heart of the product)

### 6.1 Graph topology

```
START
  │
  ▼
plan ─────────────  objective (+ optional cheap memory digest: "prior research exists?") → ResearchPlan:
  │                  questions (each with a stable question_id) mapped to the 9 sections
  ▼
[interrupt] ──────  HITL: user reviews/edits research questions, then approves  → resume(Command)
  │
  ▼
recall ───────────  cross-session memory — runs AFTER approval so each reused fact owns an approved
  │                  question_id: per approved question, match memory by company_key; if age < TTL →
  │                  remap its self-contained source into THIS session (new source_id) + seed a
  │                  question-owned fact; else re-fetch & revalidate by content_hash. Mark answered Qs. (§7.1)
  ▼
gather ───────────  Send-fan-out over questions NOT answered by recall → Tavily + httpx; emits
  │ (parallel)      (question_id, Source) discoveries — merge_by_id dedupes Source rows by url while
  │                 keeping the question→source links; RetryPolicy(max_attempts=3, retry_on=transient)
  ▼
extract ──────────  per (question_id, source) → Fact[] (question_id, claim, source_id, confidence)  [parallel, nano]
  │
  ▼
analyze ──────────  map-reduce Facts → DraftReport (9 sections + inline citation refs)
  │
  ▼
quality_gate ─────  LLM-judge + deterministic checks → QualityVerdict(score, gaps[], weak_questions[])
  │
  ├─ score < τ AND retries_left > 0 ──▶ set weak_questions; goto gather  (targeted re-research loop)
  │
  ▼ (score ok OR retry budget spent → graceful)
report ───────────  assemble final ResearchReport (all 9 sections + Sources)
  │
  ▼
persist ──────────  relational write (sessions/reports/sources) + embed chunks → pgvector;
  │                  upsert research_memory for future recall
  ▼
END

error_handler (on gather/extract/analyze, langgraph ≥1.2):
  NodeError → Command(goto="report", update={degraded: True})  → honest partial report, never a 500
  → terminal status = "degraded" (resumable; SSE/API contract in §9.1)
```

### 6.2 Shared state (`ResearchState`)

```python
class ResearchState(TypedDict):
    session_id: str
    company: str; website: str | None; objective: str
    plan: ResearchPlan | None
    sources: Annotated[list[Source], merge_by_id]            # dedupe-merge by deterministic id (idempotent)
    facts:   Annotated[list[Fact], supersede_by_question]    # facts own a question_id; loop-back replaces
                                                             #   the WHOLE fact set for each re-researched question
    draft:   DraftReport | None
    quality: QualityVerdict | None
    retries_left: int
    weak_questions: list[str]                                 # question_ids to re-research
    reused_from_memory: bool
    degraded: bool
    status: Literal["planning","awaiting_approval","recalling","gathering",
                    "analyzing","grading","reporting","done","degraded","failed"]
    errors: Annotated[list[NodeErrorRecord], add]
```

### 6.3 LangGraph primitives → assignment requirements (traceability)

| Brief requirement | How we satisfy it |
| :--- | :--- |
| Multiple meaningful nodes | recall, plan, gather, extract, analyze, quality_gate, report, persist |
| Shared graph state | `ResearchState` with idempotent reducers — `merge_by_id` (sources) + `supersede_by_question` (facts) — for safe parallel **and** loop-back merges |
| Conditional routing | `quality_gate` → loop-back-to-gather vs. proceed; recall marks answered questions so `gather` skips them |
| Intermediate outputs | plan, sources, facts, draft surfaced live via streaming |
| Failure handling | per-node `RetryPolicy`; `error_handler` → degraded partial report |
| Recoverability | `PostgresSaver` checkpoint per super-step; resume by re-invoking with same `thread_id` |
| (beyond brief, in MVP gate) HITL | `interrupt()` after plan; resume with `Command(resume=edited_questions)` |
| (beyond brief, in MVP gate) Parallelism | `Send` fan-out over research questions |
| (beyond brief, in MVP gate) Memory | cross-session recall + chat RAG over pgvector |

### 6.4 Cost & latency budget (per run, nano)

Target: **< $0.01/run** at nano-class pricing, **< 90s** typical (parallel gather). The budget is a
*target recomputed from live pricing at build time*, not a guarantee. Retry loop bounded to ≤1 extra
pass. Token caps per node enforced in config.

### 6.5 Durable LLM-call cache (token idempotency on resume)

LangGraph checkpoints *between* super-steps, so a crash mid-node would, on resume, re-run that node.
Completed calls must not be re-paid, and concurrent resumes / double-submits must not double-charge.
Every LLM / embedding / search call is therefore wrapped in a **durable call ledger** keyed by
`(session_id, node, attempt, input_hash)`:

1. **Reserve** — atomically insert an `in_progress` row for the key with an **owner token (`locked_by`)
   and a lease (`lease_expires_at`)**. A duplicate key **single-flights**: if the row is `in_progress`
   **and the lease is valid**, wait for it; if `done`, return the cached response.
2. **Take over abandoned reservations** — if the row is `in_progress` but its **lease has expired** (the
   owner crashed after reserving), a waiter atomically **compare-and-swaps** ownership (new `locked_by`,
   renewed lease) and proceeds — so no one deadlocks waiting on a call that no longer exists. The active
   owner heartbeats to extend its lease while the call is in flight.
3. **Call with a provider idempotency key** = the same key, so a *re-sent* request (after a crash or a
   takeover) is **deduplicated by the provider and not re-charged** (within the provider's window).
4. **Commit** — atomically store the response (`status → done`); on permanent failure set `failed` so the
   key can be retried. A later call with the same key **hits → returns the stored response, no provider call**.

The ledger is **durable** (Postgres `llm_calls`, or Redis with AOF) so it survives the crash.

**Honest guarantee (deliberately not an absolute):** any call already committed to the ledger is
**never re-paid**, and the at-most-one in-flight call at crash time is protected by the provider
idempotency key — so a mid-node kill re-pays nothing in practice. The only residual exposure is a crash
landing *between* provider-charge and ledger-commit **and** outside the provider's idempotency window.
Tests: **crash-after-reserve-before-call** (asserts lease takeover, no deadlock), **kill-before-commit**
(asserts no double-charge via the idempotency key), and **concurrent-resume** (asserts single-flight).

---

## 7. Memory & context architecture (3 layers)

| Layer | Store | Holds | Purpose |
| :--- | :--- | :--- | :--- |
| **1. Run state** | LangGraph `PostgresSaver` (thread_id = session_id) | `ResearchState` per super-step | Durability + resume (recoverability). Internal, not served to UI. |
| **2. Product data** | Our Postgres tables | sessions, reports, sources, facts, messages | The clean entities the API serves. `persist` node is the seam. |
| **3. Semantic** | pgvector tables | report_chunks, source_chunks, research_memory | Chat RAG (grounded answers) + cross-session warm-start. `research_memory` is **self-contained** (carries source provenance + `content_hash` + `fetched_at`, **no foreign `source_id`**); reuse is TTL-gated and remaps sources into the live session — see §7.1. |

**Context assembly rule:** never dump full state into a prompt. Each node gets a minimal typed
context (extract = one source; analyze = facts grouped per section; chat = top-k chunks + last N turns).

### 7.1 Cross-session memory & freshness (grounding safety)

`research_memory` is **self-contained**: each row carries its own source provenance (`url`, `title`,
`snippet`, `content_hash`, `fetched_at`) plus a persisted **`section`** and **`freshness_class`**
(`durable` | `volatile`) discriminator — and it does **not** reference a session-scoped `source_id`,
so `recall` can never make a report cite another session's row. On reuse, per candidate:

1. Look up memory by `company_key`; read its persisted `freshness_class` → resolve `TTL` from config
   (`volatile` default 0–1d, `durable` default 14d). No read-time reclassification.
2. If `now - fetched_at < TTL` → **remap** it into the current session as a fresh `sources` row
   (new session-scoped `source_id`) and seed a **fact owned by the matched approved `question_id`**.
3. Else → **re-fetch** the `url`: if `content_hash` is unchanged, refresh `last_validated_at` and
   reuse; if changed, discard the stale fact and let `gather` re-derive it.
4. **Invariant:** every claim that reaches the report cites a `sources` row that exists *in this
   session*. Memory accelerates and cheapens research; it never bypasses grounding.

`recall` runs **after** plan-approval precisely so each reused fact can be bound to an approved
`question_id`. `research_memory` itself stores **no** `question_id` (it is question-agnostic across
sessions); the binding is done at recall time by semantic match to the current plan, and memory facts
that match no approved question are dropped. This keeps recalled facts inside the same
`supersede_by_question` lifecycle as freshly-gathered ones (so the quality loop can supersede them too).

Because `freshness_class` is **stored on each fact/memory row** (not inferred at read time), volatile
facts (Business Signals, Risks, news) are effectively always re-fetched while durable facts (Overview,
Products) warm-start. An acceptance test asserts exactly this split. (Resolves Finding #2 + recall/question_id.)

---

## 8. Follow-up chat (grounded, cheap loop)

Small 2-node graph `retrieve → answer` (so it inherits checkpointing + streaming):
- retrieve top-k pgvector chunks (report + sources) scoped to `session_id` + last N turns
- answer with `gpt-5-nano`, **must cite `source_id`s**; out-of-context → "not in the report"
- streamed token-by-token to the UI.

### 8.1 Citation durability across resume/regenerate

Chat can outlive a report version (a degraded run is chatted against, then resumed/regenerated). Two
rules keep the grounded-chat contract intact:
- **Self-contained citations.** Each stored citation snapshots its source (`source_id` + `url` +
  `title` + `snippet`) at answer time, so `GET /messages` always resolves — even if that source was
  later dropped by a shrinking regenerate (§10). Active RAG retrieval still uses only live sources, so
  *new* answers never pull stale passages.
- **Staleness signal.** `persist` bumps `reports.version`; each message records the `report_version`
  it was grounded on, and any message grounded on an older version is flagged `stale=true`. The chat
  UI shows a "report was updated — earlier answers may reference changed sources" notice rather than
  silently serving dangling context.

---

## 9. API contract (FastAPI)

| Method | Path | Purpose |
| :--- | :--- | :--- |
| POST | `/sessions` | Create session `{company, website, objective}`; start graph; return `session_id` |
| GET | `/sessions` | Session history (list) |
| GET | `/sessions/{id}` | Session detail + report (if ready) |
| GET | `/sessions/{id}/stream` | **SSE** live workflow progress (node updates) |
| POST | `/sessions/{id}/approve-plan` | Resume from HITL interrupt with (optionally edited) questions |
| POST | `/sessions/{id}/resume` | Resume a failed/interrupted run (recoverability) |
| POST | `/sessions/{id}/chat` | Grounded chat (streamed); persists messages |
| GET | `/sessions/{id}/messages` | Chat history |
| GET | `/healthz` | Liveness/readiness (db, redis) |

Error taxonomy → HTTP: transient (503 + retry-after), validation (422), degraded (200 + `degraded:true`), not-found (404).

### 9.1 Run lifecycle & degraded contract

Terminal statuses: **`done`** (full report), **`degraded`** (partial — some nodes failed after
retries; honest Unknowns + `degraded:true`), **`failed`** (no usable report). All three are terminal
and **resumable** via `POST /sessions/{id}/resume`, which re-enters the graph from the last good
checkpoint to attempt an upgrade (`degraded`/`failed` → `done`); resuming a `done` run is a no-op.
- **SSE** emits a final `{event:"end", status}` and then closes on any terminal status.
- **`GET /sessions/{id}`** returns the (possibly partial) report + `status` + `degraded`; the UI
  renders partial sections under a degraded banner, not an error screen.
- The `status` enum (§6.2) and `sessions.status` column both include `degraded`, so the terminal state
  is unambiguous across graph, API, and UI. (Resolves adversarial finding: degraded contract.)

## 10. Data model (Layer 2 + 3)

```
sessions(id, company, company_key, website, objective, status, degraded, created_at, updated_at)
reports(id, session_id UNIQUE, version, sections_jsonb, quality_score, model_meta_jsonb, created_at)  -- 1/session, upsert; version bumps on regenerate
sources(id, session_id, url, title, snippet, content_hash, fetched_at,
        UNIQUE(session_id, url))                          -- id = hash(session_id, normalized_url)
facts(id, session_id, question_id, section, freshness_class, claim, claim_norm,
        source_id REFERENCES sources(id) ON DELETE CASCADE, confidence, attempt,
        UNIQUE(session_id, question_id, source_id, claim_norm))  -- id = hash(session_id, question_id, source_id, claim_norm)
messages(id, session_id, role, content, citations_jsonb, report_version, stale, created_at)
        -- citations_jsonb = self-contained snapshots (source_id + url/title/snippet at answer time) so
        -- they resolve even if a source is later dropped; `stale` set on report regenerate (§8.1)
report_chunks(id, session_id, section, chunk_ix, text, embedding vector(1536),
        UNIQUE(session_id, section, chunk_ix))
source_chunks(id, session_id, source_id REFERENCES sources(id) ON DELETE CASCADE, chunk_ix, text, embedding vector(1536),
        UNIQUE(source_id, chunk_ix))
research_memory(id, company_key, section, freshness_class, url, title, snippet, content_hash,
        fact, fact_norm, embedding vector(1536), fetched_at, last_validated_at, created_at,
        UNIQUE(company_key, content_hash, fact_norm))      -- self-contained; NO source_id/question_id (bound at recall, §7.1); x-session
llm_calls(session_id, node, attempt, input_hash, status, idempotency_key, locked_by, lease_expires_at,
        response_jsonb, created_at, PRIMARY KEY(session_id, node, attempt, input_hash))
        -- durable call ledger; status in (in_progress,done,failed); lease+owner enable takeover of abandoned rows; §6.5
-- LangGraph checkpoint tables created by PostgresSaver.setup()
```

**Idempotency & consistency:** every row uses a **deterministic id** derived from its natural key
(e.g. `source.id = hash(session_id, normalized_url)`, `fact.id = hash(session_id, question_id,
source_id, claim_norm)`) backed by the `UNIQUE` constraints above. Beyond de-duplication, two
**replacement** rules keep re-research/resume *convergent* (not merely duplicate-free):

- **Facts are superseded per question.** The `supersede_by_question` reducer (and `persist`) drop
  *all* prior facts owned by a re-researched `question_id` before inserting the new attempt's facts —
  so a reworded or corrected claim cannot leave its stale predecessor behind. `attempt` records which
  gather pass produced each fact.
- **The whole materialization set is replaced.** In one transaction `persist` computes the current
  session source set and **deletes any `sources` rows no longer present** — cascading to their
  `source_chunks` **and `facts`** (`facts.source_id` is FK `ON DELETE CASCADE`) — deletes `report_chunks`
  for the session, then re-inserts the current sources/facts/chunks. So if a regenerate or resume
  **drops a source entirely** or **shrinks** output, no orphan `sources`, `facts`, `source_chunks`, or
  `report_chunks` remain for chat or analysis to retrieve.

`persist` runs as a **single transaction** (relational + vector together, or rolls back). Verified by
resume/retry tests asserting zero duplicates, **no stale facts after re-research, and no orphan chunks
after a shrinking regenerate** (§11). (Resolves Finding #3 + chunk-staleness.)

---

## 11. Non-functional requirements

- **Reliability:** retries on transient errors; graceful degradation; kill-and-resume demonstrated with
  a durable LLM-call ledger + provider idempotency keys (§6.5) so resume re-pays no tokens for committed
  calls and the in-flight call isn't double-charged.
- **Idempotency & consistency:** deterministic ids + unique constraints on sources/facts/reports/
  chunks/memory; `persist` is a single transaction that upserts rows and **replaces the full session
  materialization** — sources not present in the new output are deleted, **cascading to their `facts`
  and `source_chunks`**, and `report_chunks` are replaced (so a source removed *entirely* leaves
  nothing behind); facts are also **superseded per `question_id`** on loop-back; reducers are
  `merge_by_id` (sources) / `supersede_by_question` (facts). Tests assert **zero duplicate rows, no
  stale or orphan facts after re-research or a dropped source, no orphan sources/chunks after shrink,
  and volatile facts re-fetched while durable facts warm-start**.
- **Observability:** Langfuse traces (per-node spans, token/cost, latency); structured JSON logs with `session_id`/`node`; error taxonomy.
- **Cost control:** per-node model + token caps in config; bounded retry loop; cross-session cache.
- **Config:** `pydantic-settings` (12-factor), `.env.example`, no secrets in repo.
- **Security (light):** input validation, SSRF guard on website fetch (block internal IPs), output is read-only.
- **Testing:** unit (nodes with mocked `SearchProvider` + LLM), graph integration test (deterministic mocks), API contract tests.

## 12. Tech stack & infra

`api`: FastAPI + LangGraph + `openai` + `tavily-python` + `httpx` + `pydantic-settings` + `structlog`.
`web`: Vite + React + TS + Tailwind.
Infra (Docker): `postgres` (pgvector image, all 3 Postgres roles), `redis`, `langfuse` (profile).

```
docker compose up            # api, web, postgres, redis
docker compose --profile obs up   # + langfuse (clickhouse/minio)
```

## 13. MVP acceptance gate & build order

**MVP acceptance gate — all must pass; none are optional (this is the single definition of "done"):**
- ✅ `(company, website, objective)` → all 9 report sections, every claim cited, honest Unknowns.
- ✅ Postgres checkpoint with a **demonstrated kill-and-resume**: completed nodes are never re-executed;
  any call already committed to the durable ledger is never re-paid and the in-flight call is
  deduplicated via a provider idempotency key (§6.5) — so a mid-node kill re-pays no tokens in practice
  and leaves no duplicate rows.
- ✅ Grounded follow-up **chat** with valid citations; out-of-scope → "not in the report."
- ✅ **Cross-session memory** warm-start with TTL revalidation; every reused claim re-anchored to a
  current-session source (§7.1).
- ✅ **HITL plan-approval** (interrupt → edit questions → resume).
- ✅ **Parallel `Send` fan-out** over research questions.
- ✅ Self-correcting quality loop; graceful **degraded** path (never a 500); **idempotent** persist.
- ✅ Sessions create/history/detail; loading/error/degraded UI states; responsive.
- ✅ Dockerized; config-driven models; structured logs; Langfuse traces (profile).

The phases below are **build order, not scope tiers** — every phase is required by the gate above;
they are sequenced so each increment is independently demoable (depth-first delivery, not descoping).

- **P0 — Baseline graph:** plan → recall(**stub/no-op, positioned after `plan`**) → gather(Tavily,
  sequential) → extract → analyze → quality_gate(loop) → report → persist. Checkpoint + **resume**;
  idempotent writes. Sessions API + SSE progress. *Demo: full cited report + kill/resume.*
- **P1 — Memory & chat:** pgvector chunks; grounded chat loop; cross-session `recall` made **real**,
  binding facts to the plan's (auto-approved) `question_id`s with TTL revalidation (§7.1). recall stays
  *after* `plan`, so approved question_ids always exist before any binding. *Demo: cited follow-ups;
  re-run same company → warm-start.*
- **P2 — HITL + parallelism:** insert `interrupt()` plan-approval **between `plan` and `recall`** +
  `approve-plan` API/UI; `Send` fan-out. Now "approved" = human-approved. Acceptance: recall may bind
  only to current-plan question_ids. *Demo: edit the plan; parallel gather.*
- **P3 — Polish & ops:** error/degraded states, Langfuse profile, config hardening, idempotency/
  resume tests, README/architecture/engineering-decisions/product-improvements docs.

**Fast-follow (explicitly post-MVP, outside the gate):** eval/golden-set harness in CI; PDF export;
CRM push; auth/multi-tenancy.

## 14. Success metrics

- **Product:** report completeness (sections filled / 9), **citation density** (% claims cited),
  unknowns ratio, time-to-report, chat groundedness (answers with valid citations).
- **Eng/ops:** run success rate, resume success rate, p50/p95 latency, **cost/run**, retry rate.
- **Business (narrative):** prep-time saved/meeting, runs/user/week, repeat-research rate.

## 15. Risks & mitigations

| Risk | Mitigation |
| :--- | :--- |
| Scope (3 extensions + baseline) | Phases P0→P3 are build *order*, not optional tiers — all required by the MVP gate (§13), sequenced so each step is independently demoable. Risk managed by depth-first delivery, not by cutting scope. |
| Hallucination / weak grounding | Fact→source provenance, citation-required prompts, Unknowns contract, quality gate |
| Tavily/web flakiness & cost | `SearchProvider` abstraction + retries + cache + degraded path; mock in tests |
| Langfuse self-host weight | Optional `profile`; core dev never blocked on it |
| nano quality on synthesis | Config-driven step-up to a stronger model (a nano step-up class, or `gpt-5`) for the analyze node only |
| SSRF via user website | Block private/internal IP ranges on fetch |

## 16. Open (minor) items — defaults set, confirm during build

- **Model ids/pricing:** treated as config placeholders; the app verifies each id against the live
  API at boot and **fails fast** on an unknown id. The `<$0.01/run` budget is recomputed from live
  pricing, not asserted. (Resolves adversarial Finding #4.)
- **Retry-loop threshold τ** and max extra passes — start τ=0.7, ≤1 extra pass (config).
- **`company_key`** for cross-session recall — default: registrable domain (eTLD+1) of `website`,
  fallback to normalized company name.
- **Memory freshness TTL** — default 14d global, per-section override for volatile sections (§7.1).
