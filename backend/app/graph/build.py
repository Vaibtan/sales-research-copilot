"""Graph assembly + initial-state construction.

The topology is the spine
``plan → recall → gather → extract → analyze → quality_gate → report → persist``
with one **cycle**: ``quality_gate`` routes back to ``gather`` (slice 4) for a
bounded, targeted re-research pass when the draft scores below threshold, or
forwards to ``report``. That branch is expressed as a ``Command(goto=...)`` from
the node itself (no static ``quality_gate → report`` edge). ``recall`` is still a
no-op; degraded routing (8) and the HITL interrupt (12) slot in without changing
this skeleton.
"""

from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy

from app.db.ledger import CallLedger
from app.domain.state import ResearchState
from app.graph.nodes import (
    make_analyze_node,
    make_await_approval_node,
    make_error_handler,
    make_extract_node,
    make_gather_node,
    make_gather_one_node,
    make_persist_node,
    make_plan_node,
    make_quality_node,
    make_recall_node,
    report_node,
)
from app.infra.database import Database
from app.providers.base import LLMClient, SearchProvider
from app.providers.embeddings import EmbeddingClient
from app.providers.errors import is_transient
from app.providers.ledger import LedgerEmbeddingClient, LedgerLLMClient


def build_graph(
    *,
    llm: LLMClient,
    search: SearchProvider,
    db: Database,
    checkpointer: AsyncPostgresSaver,
    max_extra_passes: int,
    quality_threshold: float,
    ledger: CallLedger | None = None,
    model_meta: dict | None = None,
    retry_max_attempts: int = 3,
    embeddings: EmbeddingClient | None = None,
    memory_ttl_durable_seconds: float = 14 * 86400,
    memory_ttl_volatile_seconds: float = 86400,
    memory_match_max_distance: float = 0.85,
    memory_revalidator=None,
    hitl_plan_approval: bool = False,
) -> CompiledStateGraph:
    # Route every LLM / embedding call through the durable ledger (§6.5) so a
    # mid-node kill re-pays nothing on resume. When no ledger is supplied (some unit
    # tests) the nodes call the provider directly.
    if ledger is not None:
        llm = LedgerLLMClient(llm, ledger)
        if embeddings is not None:
            embeddings = LedgerEmbeddingClient(embeddings, ledger)

    # Network-bound nodes retry transient failures (PRD §7, §11). The predicate
    # only matches transient provider/network errors; deterministic mocks never
    # raise these, so the policy is a no-op under tests. The ledger underneath
    # means a retried node never re-pays an already-committed call.
    net_retry = RetryPolicy(max_attempts=retry_max_attempts, retry_on=is_transient)

    builder = StateGraph(ResearchState)

    # plan failure → no error_handler → the run fails (no usable report, §9.1).
    plan_next = "awaiting_approval" if hitl_plan_approval else "recalling"
    builder.add_node(
        "plan", make_plan_node(llm, next_status=plan_next), retry_policy=net_retry
    )
    # HITL gate (slice 12): interrupt for human review/edit when enabled, else a
    # transparent pass-through. Always present so the topology is resume-stable.
    builder.add_node(
        "await_approval", make_await_approval_node(enabled=hitl_plan_approval)
    )
    builder.add_node(
        "recall",
        make_recall_node(
            db,
            embeddings,
            ttl_durable_seconds=memory_ttl_durable_seconds,
            ttl_volatile_seconds=memory_ttl_volatile_seconds,
            match_max_distance=memory_match_max_distance,
            revalidator=memory_revalidator,
        ),
    )
    # gather fans out (slice 13): the dispatcher (no network) Sends a parallel
    # gather_one worker per question; the worker is the network-bound, retried,
    # degradable unit. A failed worker routes to extract (its normal successor) so
    # the other questions' evidence still flows — that question becomes an Unknown.
    builder.add_node("gather", make_gather_node(max_extra_passes=max_extra_passes))
    # gather_one self-handles retry + degrade internally (a node-level error_handler
    # does not apply to Send-invoked workers), so no retry_policy/error_handler here.
    builder.add_node(
        "gather_one",
        make_gather_one_node(search, retry_max_attempts=retry_max_attempts),
    )
    # extract/analyze failures (after retries) degrade to an honest partial report
    # via the error_handler instead of aborting (§6.1).
    builder.add_node(
        "extract",
        make_extract_node(llm, max_extra_passes=max_extra_passes),
        retry_policy=net_retry,
        error_handler=make_error_handler("extract"),
    )
    builder.add_node(
        "analyze",
        make_analyze_node(llm, max_extra_passes=max_extra_passes),
        retry_policy=net_retry,
        error_handler=make_error_handler("analyze"),
    )
    builder.add_node(
        "quality_gate",
        make_quality_node(
            llm, quality_threshold=quality_threshold, max_extra_passes=max_extra_passes
        ),
    )
    builder.add_node("report", report_node)
    builder.add_node(
        "persist",
        make_persist_node(db, model_meta=model_meta, embeddings=embeddings),
    )

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "await_approval")
    builder.add_edge("await_approval", "recall")
    builder.add_edge("recall", "gather")
    # gather → {Send(gather_one) … | extract} is a Command(goto=...) from the
    # dispatcher; the fan-out workers rejoin at extract.
    builder.add_edge("gather_one", "extract")
    builder.add_edge("extract", "analyze")
    builder.add_edge("analyze", "quality_gate")
    # quality_gate → {gather (loop-back) | report} is a Command(goto=...) from the
    # node; the loop is bounded by `retries_left`, not by any static edge.
    builder.add_edge("report", "persist")
    builder.add_edge("persist", END)

    return builder.compile(checkpointer=checkpointer)


def build_initial_state(
    *,
    session_id: str,
    company: str,
    website: str | None,
    objective: str,
    company_key: str,
    max_extra_passes: int,
) -> ResearchState:
    return ResearchState(
        session_id=session_id,
        company=company,
        website=website,
        objective=objective,
        company_key=company_key,
        plan=None,
        sources=[],
        assignments=[],
        facts=[],
        draft=None,
        report=None,
        quality=None,
        retries_left=max_extra_passes,
        weak_questions=[],
        answered_questions=[],
        reused_from_memory=False,
        degraded=False,
        status="planning",
        errors=[],
    )
