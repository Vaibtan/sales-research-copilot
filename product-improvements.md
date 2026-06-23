# Product & Business Thinking

Sales Research Copilot — turning pre-meeting research from 30–60 min of manual
hunting into a 1–2 min grounded, cited briefing.

## 1. Five weaknesses in the current design

1. **Grounding quality is unmeasured.** No eval/golden-set, so "cited and accurate"
   is asserted, not proven — the entire value prop rests on an untracked metric.
2. **No CRM/workflow integration.** A briefing that lives in our UI, not in
   Salesforce/HubSpot/Gmail where reps actually work, will see low repeat usage.
3. **Shallow chat.** Single-turn RAG with no query rewriting or multi-hop — it
   answers "what are their products?" well but not "compare their pricing to X."
4. **Single-tenant, no auth.** Can't be sold to a team as-is; no seats, no sharing,
   no governance — and sales managers (the buyers) need exactly those.
5. **Source breadth is thin.** Tavily + site fetch covers the public web but misses
   the high-value sources reps care about: LinkedIn, news/funding feeds, 10-Ks,
   the rep's own past notes/emails.
6. **No freshness/recency signal in the UI.** A briefing doesn't tell the rep how
   old its facts are or which sections are stale — risky for time-sensitive
   "Business Signals."

## 2. Top 3 improvements to build next

1. **Eval harness + grounding score** — make quality a tracked, regression-gated
   number. Everything else is guesswork without it.
2. **CRM/email integration** (Salesforce/HubSpot/Gmail) — push the briefing where
   the work happens; this is what converts a demo into daily usage and willingness
   to pay.
3. **Richer, recency-aware sources** (LinkedIn, funding/news, filings) with visible
   freshness — directly raises the differentiated value (Business Signals, timing).

## 3. Who buys, who uses, why they'd pay

- **User:** SDR / AE — preps for a specific meeting; walks in with discovery
  questions + an outreach angle in 2 minutes instead of 45.
- **Buyer:** Sales manager / RevOps — wants *consistent, high-quality* prep across
  the team, faster rep ramp, and auditable, governable tooling.
- **Why pay:** it converts unstructured prep time into a repeatable, cited asset —
  time saved maps directly to revenue activity (more meetings, better-prepped reps,
  shorter ramp). Pricing: seat-based for reps + usage for runs; managers buy seats,
  RevOps governs.

## 4. Success metrics

- **Product:** report completeness (sections filled / 9), **citation density** (%
  claims cited), unknowns ratio, time-to-report, chat groundedness (% answers with
  valid citations).
- **Eng/ops:** run success rate, **resume success rate**, p50/p95 latency,
  **cost/run**, retry rate, degraded rate.
- **Business:** prep-time saved/meeting, runs/user/week, repeat-research rate,
  briefing→meeting attach rate, seat retention.

## 5. Four-week AI roadmap

- **Wk 1 — Measure:** eval/golden-set harness (citation density, completeness,
  hallucination) in CI; live model/pricing confirmation + a real cost/run number.
- **Wk 2 — Tune:** prompt + token-cap tuning per node, analyze step-up model
  decision, quality-gate threshold calibration against the eval set.
- **Wk 3 — Ground deeper:** richer sources (news/funding/LinkedIn) behind the
  `SearchProvider` seam; recency/freshness surfaced per fact and section.
- **Wk 4 — Converse:** query rewriting + multi-hop retrieval for chat; stronger
  stale-citation UX; first CRM push (read-only briefing export).

## 6. Biggest cost, scaling, and reliability risks

- **Cost:** token spend drift — the `<$0.01/run` budget is a target, not a
  guarantee; the quality loop + fan-out multiply spend on hard companies. *Mitigate:*
  per-node caps, bounded loop, the cross-session cache + ledger (no re-pays), and a
  per-tenant budget guard.
- **Scaling:** the background runner is in-process (one executor per session in one
  API instance); horizontal scale needs the durable lease to become the
  cross-instance single-flight (the ledger already lays the groundwork), plus a
  proper job queue. Search/LLM rate limits are the external ceiling.
- **Reliability:** dependence on third-party search/LLM availability and grounding
  quality. *Mitigate:* the degraded path (never a 500), retries, the provider
  abstraction (swap vendors), and the resume/ledger durability story.

## 7. A feature I'd remove (and why)

**Always-on HITL plan-approval as the default.** It's a great capability and demos
the `interrupt` primitive, but forcing every run to pause for approval adds friction
that most reps won't want for routine prep. Keep it as an *opt-in* power-user mode
(it already is config-gated) and default to auto-approve, surfacing "edit plan" as a
one-click option — preserve the capability, remove the default friction.

## 8. A feature I'd add (and why)

**CRM/email integration with one-click briefing push** (and "research this
account" triggered from the CRM). It's the single biggest driver of repeat usage
and willingness to pay: a briefing inside the rep's existing workflow gets used
every day; one in a separate tab gets used once. It also unlocks the buyer's
real job — *consistent team-wide prep* — and creates a defensible workflow hook.

## 9. First 90-day roadmap

- **Days 0–30 — Trustworthy:** eval harness + grounding score; live provider
  tuning + real cost numbers; chaos/durability test against the live provider.
  Goal: a quality number we'd stake the pitch on.
- **Days 31–60 — Sticky:** CRM/email integration (push + trigger); richer,
  recency-aware sources; conversational chat. Goal: daily-active usage.
- **Days 61–90 — Sellable:** auth + multi-tenancy (seats, sharing, RBAC); manager
  dashboards (team usage, prep coverage); usage-based billing + budget guards.
  Goal: a team can buy and govern it.

## 10. If I owned it, the first thing I'd change

**Build the eval harness and put a grounding score on the dashboard — day one.**
The product's entire promise is "grounded and cited, no fabrication," yet today
that's an architectural intention, not a measured fact. Without a number, we can't
tune prompts, set the quality threshold honestly, catch regressions, or credibly
sell trust. Everything else (sources, chat, CRM) compounds *on top of* trustworthy
grounding; measuring it first is what turns a strong demo into a defensible product.
