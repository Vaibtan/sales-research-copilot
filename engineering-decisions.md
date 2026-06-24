# Engineering Decisions

The decisions a senior reviewer would probe, the alternatives weighed, and what we
knowingly traded away. Rationale background: [`research.md`](./research.md).

## 3 major engineering decisions

### 1. LangGraph as the *single* control plane — raw `openai` SDK inside nodes, no second orchestrator

**Decision.** Use LangGraph for all orchestration, state, durability, routing, and
retries, and call the plain `openai` Responses API directly inside nodes for
structured outputs. No OpenAI Agents SDK, no LangChain in the hot path.

**Alternatives considered.** (a) OpenAI Agents SDK for the agent loop on top of
LangGraph; (b) LangChain wrappers (`init_chat_model`, chains) for model I/O;
(c) a hand-rolled orchestrator.

**Tradeoff.** The Agents SDK and LangChain are genuinely convenient, but each is a
*second* control plane — two memory models, two tracing systems, two retry
behaviors competing with LangGraph (which the brief mandates). Stacking them reads
as architectural indecision and costs control over token/cost/retry behavior. We
gave up out-of-the-box conveniences (tool loops, handoffs, `init_chat_model`) for
one explicit, debuggable control plane and direct ownership of every model call.
The cost: we write the node glue ourselves and own the structured-output parsing.

### 2. Durability as two cooperating layers: checkpoint **plus** a durable call ledger

**Decision.** Lean into recoverability as the headline feature. LangGraph
checkpoints per super-step (resume skips completed nodes), and *every* LLM /
embedding / search call is additionally wrapped in a durable `llm_calls` ledger
keyed by `(session_id, node, attempt, input_hash)` — reserve →
single-flight/lease-takeover (CAS) → provider-idempotency-key call → commit, with
only a committed result ever entering graph state.

**Alternatives considered.** (a) Checkpoint only — accept that a crash *mid-node*
re-runs the node and re-pays its calls on resume; (b) an in-memory single-flight
guard only (no cross-process durability); (c) rely solely on the provider's own
idempotency window.

**Tradeoff.** The ledger is real complexity (a state machine with leases,
heartbeats, CAS takeover, DB-side lease math). We accepted it because checkpoint-only
leaves a visible hole — the brief explicitly rejects "a single LLM call wrapped in
an API," and "resume re-pays the whole node" is exactly the unimpressive failure
mode. The honest guarantee we can defend: a committed call is never re-paid, and
the one in-flight call at crash time is de-duped by the provider idempotency key.
Residual exposure (a crash between provider-charge and ledger-commit *and* outside
the provider's idempotency window) is documented, not hidden.

### 3. Provider seams + deterministic mocks → the whole product runs and is tested offline

**Decision.** Every external dependency (LLM, search, embeddings, website fetch)
sits behind an interface with a deterministic mock; the real backends activate only
when both API keys are present. The test suite is hermetic (forces mocks, isolates
the cross-session table).

**Alternatives considered.** (a) Wire real providers directly and gate tests on live
keys / recorded cassettes (VCR); (b) integration-test against the live APIs.

**Tradeoff.** We invested in test doubles that are *real* doubles (the mock search
returns nothing for "risk" queries to exercise the honest-Unknowns path; the mock
embedding is a bag-of-words vector so retrieval is semantically meaningful offline).
The cost: the mocks must be kept faithful, and the live path (slice 7) is built but
its end-to-end behavior is verified by construction + injected fakes, not by a CI
run against OpenAI. The payoff: 105 deterministic tests, zero secrets/network/flake
in CI, and a product a reviewer can run instantly without keys.

## Top technical debt items

1. **The live provider path lacks an automated eval/golden-set.** The OpenAI/Tavily
   adapters are unit-tested via injected fakes, but groundedness/completeness on
   real companies is unmeasured. This is the documented fast-follow (PRD §0/§13).
2. **Prompt engineering is first-draft.** Node instructions are well-formed but
   untuned against live output; the analyze step-up model and token caps are
   placeholders pending a live cost/quality pass.
3. **Chat is single-turn-RAG, not conversational.** It retrieves top-k + last-N
   turns but doesn't do query rewriting or multi-hop; long sessions will degrade.
4. **The fan-out's per-question degrade is coarse.** A failed `gather_one` drops the
   question to an Unknown and marks the run degraded, but there's no per-question
   retry budget surfaced to the UI or partial-source-quality signal.
5. **Single-tenant, no auth.** Global sessions by design (PRD §3); multi-tenancy
   would touch every table (a `tenant_id` + row scoping) and the API.
6. **Observability is built but unverified against a live Langfuse.** Spans/exporter
   exist; the "trace appears in Langfuse" acceptance is a manual step.

## Biggest technical risk

**Grounding quality on the live model + search path.** Everything downstream — the
value prop ("grounded, cited, no fabrication"), the quality gate's thresholds, the
chat's "not in the report" discipline — depends on the real `gpt-5-nano`-class
model and Tavily producing clean, attributable facts. The architecture *contains*
the risk (citation-required prompts, the Unknowns contract, the quality loop, the
self-contained-citation chat), but the actual grounding rate is unmeasured until
the eval harness runs against live providers. A close second is **cost drift**: the
`<$0.01/run` budget is a target recomputed from live pricing, not a guarantee, and
the quality loop + fan-out can multiply token spend on hard companies.

## What I'd improve with 2 more weeks

1. **Eval/golden-set harness in CI** (week 1). A fixed set of companies scored for
   citation density, completeness (sections filled / 9), and hallucination, run on
   every change — turning "grounding quality" from a risk into a tracked metric.
2. **A real live run + cost/quality tuning** (week 1). Exercise the OpenAI/Tavily
   path end-to-end, confirm model ids/pricing, tune prompts + the analyze step-up,
   and record measured cost/run against the budget.
3. **Conversational chat** (week 2): query rewriting, multi-hop retrieval, and
   citation-aware follow-ups; surface the `stale` signal more richly.
4. **Cross-process durability hardening** (week 2): the ledger lease single-flights
   individual *calls* cross-process, but **resume / HITL graph re-entry is currently
   single-flighted only in-process** (an in-memory `_active` set) — two API instances
   could both re-enter one session. I'd add a DB-backed per-session **run lease**
   (owner + expiry) acquired before reading/deleting checkpoints so duplicate resumes
   409 across instances, plus a chaos test that kills the API process mid-node against
   the *real* provider and a background reaper for abandoned leases. (The companion
   SSRF DNS-rebinding TOCTOU — validate-vs-connect — wants connection IP-pinning in
   the same pass; both are noted in [`IMPLEMENTATION-PLAN.md`](./IMPLEMENTATION-PLAN.md).)
5. **Observability proof + dashboards** (week 2): stand up Langfuse, confirm traces
   land, and add per-node cost/latency dashboards + alerts on the error taxonomy.
