"""Slice 13 — parallel Send fan-out over research questions (PRD §6.1, §6.3).

`gather` dispatches one concurrent `gather_one` worker per question; the
`merge_by_id` reducer keeps concurrent Source discoveries idempotent. With many
questions returning an *overlapping* url, the shared source must appear exactly
once (no duplicate rows) while every question→source link is preserved.
"""

from __future__ import annotations

import pytest

from app.db.repository import create_session, get_session_detail
from app.domain.identity import new_session_id, source_id
from app.graph.build import build_graph, build_initial_state
from app.providers.base import SearchHit
from app.providers.mock import MockLLMClient

pytestmark = pytest.mark.asyncio


class OverlappingSearch:
    """Every question returns one SHARED url plus one unique url — so the fan-out
    exercises concurrent merge_by_id dedup on the shared source."""

    SHARED = "https://shared.example/common"

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]:
        if "risk" in query.lower():
            return []
        slug = "-".join(query.lower().split())[:40]
        return [
            SearchHit(
                url=self.SHARED, title="Shared source", snippet="shared evidence",
                content="shared content",
            ),
            SearchHit(
                url=f"https://uniq.example/{slug}", title=f"Unique {slug}",
                snippet="unique evidence", content="unique content",
            ),
        ]


async def test_concurrent_fanout_dedupes_overlapping_sources(db, checkpointer):
    graph = build_graph(
        llm=MockLLMClient(),
        search=OverlappingSearch(),
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
        objective="Prep",
        status="planning",
    )
    final = await graph.ainvoke(
        build_initial_state(
            session_id=session_id,
            company="Acme",
            website="https://acme.com",
            objective="Prep",
            company_key="acme.com",
            max_extra_passes=1,
        ),
        {"configurable": {"thread_id": session_id}},
    )
    assert final["status"] == "done"

    # The shared url, returned by every concurrent worker, deduped to ONE source.
    shared_id = source_id(session_id, OverlappingSearch.SHARED)
    in_state = [s for s in final["sources"] if s.id == shared_id]
    assert len(in_state) == 1, "merge_by_id must dedupe the shared source"

    # …yet multiple questions still link to it (question→source links preserved).
    linked = {a.question_id for a in final["assignments"] if a.source_id == shared_id}
    assert len(linked) >= 2

    # No duplicate source rows in the DB, and every source id is unique.
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM sources WHERE session_id=%s AND id=%s",
            (session_id, shared_id),
        )
        assert (await cur.fetchone())[0] == 1
        cur = await conn.execute(
            "SELECT count(*), count(DISTINCT id) FROM sources WHERE session_id=%s",
            (session_id,),
        )
        total, distinct = await cur.fetchone()
        assert total == distinct  # zero duplicate rows

    # The report is still correct: every claim cites a listed source.
    detail = await get_session_detail(db, session_id)
    report_ids = {s.id for s in detail.report.sources}
    for section in detail.report.sections:
        for item in section.items:
            assert all(sid in report_ids for sid in item.source_ids)
