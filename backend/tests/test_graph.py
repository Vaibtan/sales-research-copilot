"""Graph integration test with deterministic mocks (slice 2 acceptance).

Asserts the full spine produces a complete, cited 9-section report, persists it
in one transaction, honestly reports the Risks gap as an Unknown, and writes a
checkpoint per super-step.
"""

from __future__ import annotations

import pytest

from app.db.repository import create_session, get_session_detail
from app.domain.identity import new_session_id
from app.domain.sections import CONTENT_SECTIONS, Section
from app.graph.build import build_graph, build_initial_state
from app.providers.mock import MockLLMClient, MockSearchProvider

pytestmark = pytest.mark.asyncio


async def _run(db, checkpointer):
    graph = build_graph(
        llm=MockLLMClient(),
        search=MockSearchProvider(),
        db=db,
        checkpointer=checkpointer.saver,
        max_extra_passes=1,
        quality_threshold=0.7,
    )
    session_id = new_session_id()
    await create_session(
        db,
        session_id=session_id,
        company="Acme",
        company_key="acme.com",
        website="https://acme.com",
        objective="Prep for a renewal call",
        status="planning",
    )
    config = {"configurable": {"thread_id": session_id}}
    final = await graph.ainvoke(build_initial_state(
        session_id=session_id,
        company="Acme",
        website="https://acme.com",
        objective="Prep for a renewal call",
        company_key="acme.com",
        max_extra_passes=1,
    ), config=config)
    return session_id, final, graph, config


async def test_full_report_produced_and_persisted(db, checkpointer):
    session_id, final, graph, config = await _run(db, checkpointer)

    # --- terminal state -----------------------------------------------------
    assert final["status"] == "done"
    assert final["report"] is not None

    # --- persisted detail ---------------------------------------------------
    detail = await get_session_detail(db, session_id)
    assert detail is not None
    assert detail.status == "done"
    assert detail.degraded is False
    report = detail.report
    assert report is not None

    rendered = {s.section for s in report.sections}
    # Mock search returns nothing for Risks → it must be an honest Unknown,
    # never fabricated. The other six content sections are present.
    assert Section.RISKS_CHALLENGES not in rendered
    assert rendered == set(CONTENT_SECTIONS) - {Section.RISKS_CHALLENGES}
    assert any("Risks & Challenges" in u for u in report.unknowns)

    # --- every claim is cited to a source that exists (Sources list) --------
    source_ids = {s.id for s in report.sources}
    assert source_ids
    for section in report.sections:
        assert section.items
        for item in section.items:
            assert item.source_ids, f"{section.section} has an uncited claim"
            assert all(sid in source_ids for sid in item.source_ids)


async def test_checkpoint_written_per_super_step(db, checkpointer):
    _, _, graph, config = await _run(db, checkpointer)

    history = [snap async for snap in graph.aget_state_history(config)]
    # 8 nodes → a checkpoint per super-step (plus the input checkpoint).
    assert len(history) >= 8

    current = await graph.aget_state(config)
    assert current.values["status"] == "done"
    assert current.next == ()  # graph reached END


async def test_persist_is_idempotent_on_reinvoke(db, checkpointer):
    """Re-running the same materialization upserts, not duplicates."""
    session_id, _, graph, config = await _run(db, checkpointer)
    detail_first = await get_session_detail(db, session_id)

    # Persist the same results again directly (deterministic ids → upsert).
    from app.db.repository import persist_results

    state = (await graph.aget_state(config)).values
    await persist_results(
        db,
        session_id=session_id,
        sources=state["sources"],
        facts=state["facts"],
        report=state["report"],
        status="done",
        degraded=False,
    )

    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM sources WHERE session_id = %s", (session_id,))
        source_count = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT count(*) FROM facts WHERE session_id = %s", (session_id,))
        fact_count = (await cur.fetchone())[0]

    assert source_count == len(detail_first.report.sources)
    assert fact_count > 0
