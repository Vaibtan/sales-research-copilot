"""Slice 5 — idempotent persist & resume acceptance tests.

Two guarantees, proven over deterministic mocks + a real Postgres checkpointer:

* **Resume is convergent.** A run killed mid-flight resumes from the last good
  checkpoint without re-executing completed nodes (no re-paid work) and produces
  the same materialization with zero duplicate rows.
* **Persist is replacing, not just de-duplicating.** A regenerate that re-words a
  fact, drops a source, or shrinks the output leaves **no stale facts and no
  orphan rows** — `persist` deletes whatever is absent from the new output set
  inside the one transaction.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.db.repository import (
    create_session,
    get_session_detail,
    persist_results,
    set_session_status,
)
from app.domain.identity import new_session_id
from app.domain.models import (
    Fact,
    ReportItem,
    ReportSection,
    ResearchReport,
    Source,
)
from app.domain.sections import CONTENT_SECTIONS, FreshnessClass, Section
from app.graph.build import build_graph, build_initial_state
from app.graph.runner import GraphRunner, SessionBusyError
from app.infra.cache import Cache
from app.infra.events import ProgressBus
from app.providers.base import LLMTask
from app.providers.mock import MockLLMClient, MockSearchProvider

pytestmark = pytest.mark.asyncio


# --- builders ----------------------------------------------------------------


# Ids are *session-scoped* — in production `source.id`/`fact.id` are
# `hash(session_id, …)`, so they never collide across sessions. The test DB is
# long-lived, so the helpers scope their ids the same way (a bare "s1" would
# clash with a prior run's row under the global PK).


def _sid(session_id: str, label: str) -> str:
    return f"{session_id}-{label}"


def _source(session_id: str, label: str, url: str | None = None) -> Source:
    return Source(
        id=_sid(session_id, label),
        url=url or f"https://example.test/{session_id}/{label}",
        title=f"title-{label}",
        snippet="snippet",
        content_hash="h",
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _fact(
    session_id: str,
    label: str,
    question_id: str,
    source_label: str,
    *,
    attempt: int = 1,
    claim: str | None = None,
) -> Fact:
    text = claim or f"claim-{label}"
    return Fact(
        id=_sid(session_id, label),
        session_id=session_id,
        question_id=question_id,
        section=Section.COMPANY_OVERVIEW,
        freshness_class=FreshnessClass.DURABLE,
        claim=text,
        claim_norm=text,
        source_id=_sid(session_id, source_label),
        confidence=0.8,
        attempt=attempt,
    )


def _report(sources: list[Source], *, claim: str = "claim") -> ResearchReport:
    return ResearchReport(
        sections=[
            ReportSection(
                section=Section.COMPANY_OVERVIEW,
                items=[ReportItem(text=claim, source_ids=[s.id for s in sources])],
            )
        ],
        unknowns=[],
        sources=sources,
        quality_score=1.0,
    )


async def _report_row(db, session_id: str) -> tuple[int, object]:
    """The persisted report's (version, created_at) — for asserting that an
    idempotent re-persist leaves both untouched."""
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT version, created_at FROM reports WHERE session_id = %s",
            (session_id,),
        )
        row = await cur.fetchone()
    return row[0], row[1]


async def _new_session(db) -> str:
    session_id = new_session_id()
    await create_session(
        db,
        session_id=session_id,
        company="Acme",
        company_key="acme.com",
        website="https://acme.com",
        objective="Prep",
        status="planning",
    )
    return session_id


async def _counts(db, session_id: str) -> tuple[int, int]:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM sources WHERE session_id = %s", (session_id,)
        )
        sources = (await cur.fetchone())[0]
        cur = await conn.execute(
            "SELECT count(*) FROM facts WHERE session_id = %s", (session_id,)
        )
        facts = (await cur.fetchone())[0]
    return sources, facts


# --- resume (graph mechanics) ------------------------------------------------


async def test_kill_midrun_then_resume_completes_without_reexecuting(db, checkpointer):
    """Kill after `extract`, resume from the checkpoint: `plan` is not re-run
    (no re-paid work) and the finished report has zero duplicate rows."""
    llm = MockLLMClient()
    graph = build_graph(
        llm=llm,
        search=MockSearchProvider(),
        db=db,
        checkpointer=checkpointer.saver,
        max_extra_passes=1,
        quality_threshold=0.7,
    )
    session_id = await _new_session(db)
    config = {"configurable": {"thread_id": session_id}}
    initial = build_initial_state(
        session_id=session_id,
        company="Acme",
        website="https://acme.com",
        objective="Prep",
        company_key="acme.com",
        max_extra_passes=1,
    )

    # --- kill mid-run: run cleanly up to and including `extract`, then stop
    # (a committed checkpoint with `analyze` still pending — like a crash).
    await graph.ainvoke(initial, config, interrupt_after=["extract"])

    snapshot = await graph.aget_state(config)
    assert snapshot.next  # not at END — there is something left to resume
    assert llm.calls[LLMTask.PLAN] == 1

    # --- resume from the checkpoint (None input continues the same thread).
    final = await graph.ainvoke(None, config)
    assert final is not None and final["status"] == "done"
    assert final["report"] is not None

    # Completed nodes were not re-executed → plan was not re-paid.
    assert llm.calls[LLMTask.PLAN] == 1

    # The persisted materialization is complete and free of duplicates.
    detail = await get_session_detail(db, session_id)
    assert detail is not None and detail.status == "done"
    rendered = {s.section for s in detail.report.sections}
    assert rendered == set(CONTENT_SECTIONS) - {Section.RISKS_CHALLENGES}
    sources, facts = await _counts(db, session_id)
    assert sources == len(detail.report.sources)
    assert facts > 0


async def test_runner_resume_upgrades_unfinished_run(db, checkpointer, settings):
    """`GraphRunner.resume` re-enters a non-`done` run from its checkpoint, drives
    it to completion without re-executing `plan`, and is a no-op once finished."""
    llm = MockLLMClient()
    graph = build_graph(
        llm=llm,
        search=MockSearchProvider(),
        db=db,
        checkpointer=checkpointer.saver,
        max_extra_passes=1,
        quality_threshold=0.7,
    )
    cache = Cache(settings.redis_url)
    runner = GraphRunner(graph, db, ProgressBus(cache))
    try:
        session_id = await _new_session(db)
        config = {"configurable": {"thread_id": session_id}}
        initial = build_initial_state(
            session_id=session_id,
            company="Acme",
            website="https://acme.com",
            objective="Prep",
            company_key="acme.com",
            max_extra_passes=1,
        )
        await graph.ainvoke(initial, config, interrupt_after=["extract"])
        # A crash left the row marked failed; the checkpoint still has work left.
        await set_session_status(db, session_id=session_id, status="failed")

        assert await runner.resume(session_id) is True

        detail = None
        for _ in range(150):
            detail = await get_session_detail(db, session_id)
            if detail is not None and detail.status == "done":
                break
            await asyncio.sleep(0.1)
        assert detail is not None and detail.status == "done"
        assert llm.calls[LLMTask.PLAN] == 1  # plan not re-executed across resume

        # `done` is set by `persist` mid-run; wait for the executor to actually
        # release the session before asserting the no-op (an in-process resume of a
        # still-finishing run correctly reports busy, not no-op — see F4).
        for _ in range(50):
            if not runner.is_active(session_id):
                break
            await asyncio.sleep(0.05)
        assert not runner.is_active(session_id)

        # Resuming the now-finished run is a no-op (checkpoint is at END).
        assert await runner.resume(session_id) is False
    finally:
        await runner.shutdown()
        await cache.close()


async def test_concurrent_resume_single_flights(db, checkpointer, settings):
    """F4: two concurrent resumes of one partial run must not both drive the
    thread. The runner reserves the session atomically (before any await), so
    exactly one resume spawns an executor and the other raises SessionBusyError —
    no duplicate provider work, no checkpoint corruption."""
    llm = MockLLMClient()
    graph = build_graph(
        llm=llm,
        search=MockSearchProvider(),
        db=db,
        checkpointer=checkpointer.saver,
        max_extra_passes=1,
        quality_threshold=0.7,
    )
    cache = Cache(settings.redis_url)
    runner = GraphRunner(graph, db, ProgressBus(cache))
    try:
        session_id = await _new_session(db)
        config = {"configurable": {"thread_id": session_id}}
        initial = build_initial_state(
            session_id=session_id,
            company="Acme",
            website="https://acme.com",
            objective="Prep",
            company_key="acme.com",
            max_extra_passes=1,
        )
        await graph.ainvoke(initial, config, interrupt_after=["extract"])
        await set_session_status(db, session_id=session_id, status="failed")

        # Fire both resumes at once; the loser must be rejected, not double-driven.
        results = await asyncio.gather(
            runner.resume(session_id),
            runner.resume(session_id),
            return_exceptions=True,
        )
        spawned = [r for r in results if r is True]
        rejected = [r for r in results if isinstance(r, SessionBusyError)]
        assert len(spawned) == 1, results
        assert len(rejected) == 1, results

        # The single executor still drives the run to completion (plan not re-run).
        detail = None
        for _ in range(150):
            detail = await get_session_detail(db, session_id)
            if detail is not None and detail.status == "done":
                break
            await asyncio.sleep(0.1)
        assert detail is not None and detail.status == "done"
        assert llm.calls[LLMTask.PLAN] == 1
    finally:
        await runner.shutdown()
        await cache.close()


async def test_resume_replay_of_persist_does_not_bump_version(db, checkpointer):
    """F4's sibling on the persist side (F2), end-to-end: a crash after `persist`
    commits but before LangGraph checkpoints END leaves `persist` pending, so a
    resume re-runs it. Replaying from the pre-persist checkpoint with the identical
    materialization must NOT bump `version` (no false chat staleness, §8.1)."""
    llm = MockLLMClient()
    graph = build_graph(
        llm=llm,
        search=MockSearchProvider(),
        db=db,
        checkpointer=checkpointer.saver,
        max_extra_passes=1,
        quality_threshold=0.7,
    )
    session_id = await _new_session(db)
    config = {"configurable": {"thread_id": session_id}}
    initial = build_initial_state(
        session_id=session_id,
        company="Acme",
        website="https://acme.com",
        objective="Prep",
        company_key="acme.com",
        max_extra_passes=1,
    )

    # Full run → persist commits (version 1), checkpoint reaches END.
    final = await graph.ainvoke(initial, config)
    assert final["status"] == "done"
    version_before, created_before = await _report_row(db, session_id)
    assert version_before == 1

    # Rewind to the checkpoint committed just before `persist` ran and re-enter —
    # exactly the state a resume sees when persist committed but END did not.
    pre_persist = None
    async for snapshot in graph.aget_state_history(config):
        if snapshot.next == ("persist",):
            pre_persist = snapshot
            break
    assert pre_persist is not None, "expected a pre-persist checkpoint in history"

    replayed = await graph.ainvoke(None, pre_persist.config)
    assert replayed["status"] == "done"

    version_after, created_after = await _report_row(db, session_id)
    assert version_after == 1  # identical re-persist did not bump the version
    assert created_after == created_before


# --- persist replacement (convergent regenerate) -----------------------------


async def test_persist_bumps_version_only_on_content_change(db):
    """A regenerate that actually changes the served report bumps `version`; a
    follow-up persist of that same content does not bump again (Codex F2)."""
    session_id = await _new_session(db)
    s1 = _source(session_id, "s1")

    await persist_results(
        db, session_id=session_id, sources=[s1], facts=[],
        report=_report([s1], claim="first"), status="done", degraded=False,
    )
    assert (await _report_row(db, session_id))[0] == 1

    # Changed materialization → bump to 2.
    await persist_results(
        db, session_id=session_id, sources=[s1], facts=[],
        report=_report([s1], claim="reworded"), status="done", degraded=False,
    )
    assert (await _report_row(db, session_id))[0] == 2

    # Re-persisting that same content again is idempotent — stays at 2.
    await persist_results(
        db, session_id=session_id, sources=[s1], facts=[],
        report=_report([s1], claim="reworded"), status="done", degraded=False,
    )
    detail = await get_session_detail(db, session_id)
    assert detail is not None and detail.report_version == 2


async def test_identical_repersist_does_not_bump_version(db):
    """The F2 crash-replay at the unit level: `persist` re-running with the *same*
    materialization (as a resume does) must not bump `version` or move `created_at`
    — an identical re-persist is a true no-op, not a false staleness signal (§8.1)."""
    session_id = await _new_session(db)
    s1 = _source(session_id, "s1")
    report = _report([s1])

    await persist_results(
        db, session_id=session_id, sources=[s1], facts=[], report=report,
        status="done", degraded=False,
    )
    version_before, created_before = await _report_row(db, session_id)

    await persist_results(
        db, session_id=session_id, sources=[s1], facts=[], report=report,
        status="done", degraded=False,
    )
    version_after, created_after = await _report_row(db, session_id)

    assert version_before == 1
    assert version_after == 1  # not bumped — identical materialization
    assert created_after == created_before  # created_at untouched too


async def test_no_stale_facts_after_research_then_regenerate(db):
    """Re-researching a question replaces its facts: a reworded claim earns a new
    id, and the stale predecessor (same question, same source) is deleted."""
    session_id = await _new_session(db)
    s1 = _source(session_id, "s1")

    old = _fact(session_id, "fA", "q1", "s1", attempt=1, claim="old claim")
    await persist_results(
        db, session_id=session_id, sources=[s1], facts=[old], report=_report([s1]),
        status="done", degraded=False,
    )

    # Regenerate: same question + source, reworded claim → new fact id, no `old`.
    new = _fact(session_id, "fB", "q1", "s1", attempt=2, claim="updated claim")
    await persist_results(
        db, session_id=session_id, sources=[s1], facts=[new], report=_report([s1]),
        status="done", degraded=False,
    )

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM facts WHERE session_id = %s ORDER BY id", (session_id,)
        )
        ids = [row[0] for row in await cur.fetchall()]
    assert ids == [_sid(session_id, "fB")]  # stale `fA` gone, only the new fact


async def test_shrinking_regenerate_leaves_no_orphans(db):
    """Dropping a source on regenerate removes it and cascades to its facts —
    no orphan `sources` or `facts` rows survive the shrink."""
    session_id = await _new_session(db)
    s1, s2 = _source(session_id, "s1"), _source(session_id, "s2")
    fa = _fact(session_id, "fA", "q1", "s1")
    fb = _fact(session_id, "fB", "q2", "s2")

    await persist_results(
        db, session_id=session_id, sources=[s1, s2], facts=[fa, fb],
        report=_report([s1, s2]), status="done", degraded=False,
    )
    assert await _counts(db, session_id) == (2, 2)

    # Shrink: drop s2 entirely (and its fact).
    await persist_results(
        db, session_id=session_id, sources=[s1], facts=[fa],
        report=_report([s1]), status="done", degraded=False,
    )

    assert await _counts(db, session_id) == (1, 1)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM sources WHERE session_id = %s", (session_id,)
        )
        assert {r[0] for r in await cur.fetchall()} == {_sid(session_id, "s1")}
        cur = await conn.execute(
            "SELECT id FROM facts WHERE session_id = %s", (session_id,)
        )
        assert {r[0] for r in await cur.fetchall()} == {_sid(session_id, "fA")}
