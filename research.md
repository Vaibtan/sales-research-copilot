# Research & Rationale — AI Sales Research Copilot

> The **WHY** behind the build: what the evaluators are really testing, the options we weighed,
> and the reasoning behind each decision. **The canonical spec (the WHAT) lives in
> [`PRD.md`](./PRD.md)** — graph topology, `ResearchState`, API contract, schema, build plan.
> Where a topic's spec belongs to the PRD, this doc *links* rather than repeats it.

**Status:** research complete, decisions locked (grilling done). **Date:** 2026-06-18.
**Companion:** [`PRD.md`](./PRD.md) — build against that; this doc explains why it looks the way it does.

---

## 1. What the evaluators are actually testing

The brief lists 7 axes (frontend, backend, LangGraph, AI eng, production readiness, product, business).
Reading between the lines, a senior reviewer/CTO is looking for evidence of judgment, not feature count:

| Stated axis | What a senior reviewer is *really* checking |
| :--- | :--- |
| LangGraph workflow design | Did you use it as a **durable state machine** (checkpoints, resume, routing, retries) or as a glorified `for` loop? "A single LLM call wrapped in an API is not acceptable." |
| AI engineering | Structured outputs, grounding/citations, hallucination control, eval/quality gates — not prompt spaghetti. |
| Production readiness | Failure handling, recoverability, observability, config hygiene, idempotency, cost control. |
| Backend | Streaming, persistence boundaries, clean API contracts, error taxonomy. |
| Product/business | Do you understand *who* (sellers/SDRs/AEs) and *why they'd pay*, and reason about cost/scale/reliability. |

**Design thesis (drives everything in the PRD):** the most impressive thing we can show is a
workflow that *survives failure* and *remembers* — i.e. it leans hard into the two LangGraph
features most demos skip: **durable execution (checkpoint + resume)** and **layered memory
(short-term thread state + long-term semantic store)**. The PRD's graph and memory design exist to
make those two things shine.

---

## 2. Stack rationale — LangGraph only, raw OpenAI SDK, no extra frameworks

The brief mandates **React + FastAPI + LangGraph**. The open questions were how OpenAI fits in and
how much framework to take on. Decision:

```
LangGraph            → orchestration, state, durability, routing, retries   (MANDATORY, embraced)
OpenAI Python SDK    → raw model calls via the Responses API + structured   (NOT the Agents SDK)
  (`openai` pkg)        outputs, called directly inside nodes
NO LangChain core    → we do not need its abstractions in the hot path
NO OpenAI Agents SDK → it is a SECOND orchestrator; stacking it on LangGraph is redundant
```

### Why not the OpenAI Agents SDK?

The Agents SDK (`openai-agents`, v0.7) is genuinely good — `Agent(output_type=PydanticModel)`,
`@function_tool`, handoffs, sessions, tracing. **But it is itself an agent-loop orchestrator.**
LangGraph is mandatory and is *also* an orchestrator. Running both means two competing control
planes, two memory models, two tracing systems — more surface area, less control. For this
assignment that reads as architectural indecision. We use the plain `openai` SDK (Responses API)
for model I/O *inside* LangGraph nodes, keeping LangGraph as the single control plane and getting
structured outputs via the Responses API's JSON-schema / Pydantic parsing. Maximum control, minimal
deps, no double-orchestration. (The Agents SDK stays documented as the rejected alternative for
`engineering-decisions.md`.)

### Why no LangChain in the hot path?

LangGraph runs standalone; we only need `langgraph` + `langgraph-checkpoint-postgres`
(+ `langgraph-store-postgres` if we use the native store). Avoiding LangChain's model wrappers
keeps our LLM calls explicit, debuggable, and cheap to reason about. (`init_chat_model` is
convenient but hides token/cost/retry behavior we want to own.)

---

## 3. Model & cost rationale

**Indicative** OpenAI pricing (per 1M tokens, mid-2026) — treat these as *placeholders, not ground
truth*. Model IDs and prices **must be re-verified against the live API/pricing page at build time**;
the app loads them from config and **fails fast** on an unknown model id. The table fixes the *shape*
of the strategy (a cheap nano-class default + a step-up tier), not the exact numbers.

| Model (class) | ~Input | ~Output | Role |
| :--- | ---: | ---: | :--- |
| `gpt-5-nano` (cheapest) | ~$0.05 | ~$0.40 | **default** — extraction, classification, per-source fact pulls, chat |
| nano step-up *(if available)* | — | — | section synthesis if nano quality is thin |
| `gpt-5` (mid) | ~$1.25 | ~$10.00 | reserved — only final report synthesis if needed |
| flagship | — | — | not used (overkill) |
| `text-embedding-3-small` | ~$0.02 | — | pgvector embeddings (chat RAG + memory) |

**Why tiered & config-driven:** a single `MODELS` config maps each *node role* → model id, default to
the cheapest nano-class model, per-node overrides. Cost becomes a first-class config concern (a
production signal), not hardcoded — and because IDs/prices live in config, drift in OpenAI's lineup is
a config change, not a code change. The run budget (PRD §6.4) is a *target recomputed from live
pricing*, not a guarantee. → Applied config: PRD [§0](./PRD.md), [§6.4](./PRD.md).

---

## 4. Search tooling — the tradeoff behind choosing Tavily

Given "no third-party unless necessary" + available OpenAI credits, we weighed:

| Option | How | Pros | Cons |
| :--- | :--- | :--- | :--- |
| **A. OpenAI built-in web search** | Responses API `web_search` tool | Stays in OpenAI ecosystem (uses credits), no extra vendor, less code | Per-call cost, less deterministic, coarser source control |
| **B. Tavily/Exa search API** | LLM-native search vendor | Best retrieval quality + clean, citation-ready snippets | Adds a dependency + key + cost |
| **C. DIY: site fetch + raw search** | `httpx` + Bing/SerpAPI/DDG | Max control, cheap, transparent | Most code, scraping fragility, we maintain it |

**Decided → B (Tavily).** For a research product whose entire value is *grounding*, retrieval
quality wins; Tavily's `include_raw_content=True` gives clean, citable passages. We hide it behind a
swappable `SearchProvider` interface so A and C remain drop-in alternatives and so a **deterministic
mock provider** can be injected in tests/CI. The one extra dep + `TAVILY_API_KEY` is worth it. →
Spec & interface placement: PRD [§12](./PRD.md).

---

## 5. Memory & context — why the architecture is split the way it is

The PRD specs three memory layers ([PRD §7](./PRD.md)). The non-obvious *why*:

- **Why separate Layer 1 (LangGraph checkpoint) from Layer 2 (our relational tables)?** The
  checkpoint is optimized for *resume*, not for queries like "list my last 10 sessions" or "render
  report sections." Serving the UI directly from checkpoint blobs would couple the API to LangGraph
  internals and break the moment the state schema changes. The `persist` node is the deliberate
  seam: graph state in, clean rows out.
- **Why pgvector for Layer 3 instead of LangGraph's native `Store`?** The native `Store` (semantic
  index over Postgres) is viable and we cite it as the framework-native option, but for the chat RAG
  path we want full control over chunking, hybrid search, and citation joins — so we own those
  tables. (We can still demo the native `Store` for cross-session memory to prove we know the
  primitive.) A deliberate "control vs. convenience" tradeoff.
- **Why minimal per-node context (never dump full state):** small, typed context windows keep token
  cost low (nano) and cut hallucination — the "AI engineering maturity" signal.

→ Canonical layer table, stores, and the context-assembly rule: PRD [§7](./PRD.md).

---

## 6. Cross-cutting rationale (streaming & observability)

- **SSE over WebSocket for progress:** workflow progress is one-directional (server→client); SSE is
  simpler, auto-reconnects, and survives proxies. WebSocket is reserved only if we later need
  bidirectional control. A nice durability flourish falls out for free: because state is
  checkpointed, a browser refresh can **rehydrate** live progress from the latest checkpoint. →
  Endpoint spec: PRD [§9](./PRD.md).
- **Langfuse as an optional Docker `profile`, not default-on:** modern Langfuse self-host pulls in
  **ClickHouse + Redis + S3/MinIO** — heavy. Making it a `--profile obs` opt-in means core dev is
  never blocked on it, while we still get per-node + per-LLM-call OTEL spans when we want them. →
  Topology: PRD [§11–12](./PRD.md).

---

## 7. Decisions log

All decisions are locked in the canonical table at **PRD [§0](./PRD.md)**. The reasoning for each
lives above: stack/Agents-SDK (§2), models (§3), Tavily (§4), memory layering (§5),
streaming/observability (§6). Scope sequencing (P0→P3, depth over breadth) is at PRD [§13](./PRD.md);
deferred items (eval harness, auth, CRM, export) at PRD [§13 fast-follow](./PRD.md).

---

## Appendix — LangGraph primitives we verified against current docs (1.x)

Reference snippets confirming the API surface the PRD relies on (so the spec isn't hand-waving):

```python
# Durable execution (recoverability)
from langgraph.checkpoint.postgres import PostgresSaver
with PostgresSaver.from_conn_string(DB_URI) as cp:
    cp.setup()
    graph = builder.compile(checkpointer=cp)          # thread_id = session_id

# Long-term semantic memory (framework-native option we considered for Layer 3)
from langgraph.store.postgres import PostgresStore     # index=embeddings → semantic search

# Per-node resilience
from langgraph.types import RetryPolicy, Command
builder.add_node("gather", gather, retry_policy=RetryPolicy(max_attempts=3))
# error_handler(state, NodeError) -> Command(goto="report", update={"degraded": True})  # langgraph >= 1.2

# Conditional routing (self-correction loop)
builder.add_conditional_edges("quality_gate", route_on_quality)

# HITL plan-approval
from langgraph.types import interrupt   # pause after `plan`; resume with Command(resume=edited_questions)

# Parallel fan-out over research questions
from langgraph.types import Send        # gather → Send("research", q) per question

# Streaming progress → SSE
async for ev in graph.astream(inputs, config, stream_mode="updates"):
    ...   # forward node deltas to the SSE endpoint
```
