"""Slice 7 — Codex F1 regression: loop-back replaces stale evidence.

The deterministic mock search hides F1 (same urls each pass → same ids → dedupe).
With *changing* sources — which real Tavily produces — a quality loop-back must:
  * re-research the weak question over the *fresh* hits only (attempt 2), and
  * leave **no** stale facts and **no** now-uncited source from attempt 1.

This drives the graph with a search provider that returns different urls on each
pass for the same query, plus a judge that flags one section weak on pass 1.
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.db.repository import create_session, get_session_detail
from app.domain.identity import new_session_id
from app.domain.sections import Section
from app.graph.build import build_graph, build_initial_state
from app.providers.base import SearchHit
from app.providers.mock import MockLLMClient

pytestmark = pytest.mark.asyncio

_THRESHOLD = 0.7
_MAX_EXTRA_PASSES = 1


class ShiftingSearch:
    """Returns *different* urls on each call for the same query (the web changing
    between research passes); nothing for risk queries (honest-gap, like the mock)."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    async def search(
        self, query: str, *, max_results: int = 5, call=None
    ) -> list[SearchHit]:
        if "risk" in query.lower():
            return []
        self.calls[query] += 1
        pass_n = self.calls[query]
        slug = "-".join(query.lower().split())[:40]
        return [
            SearchHit(
                url=f"https://shift.example/{slug}/pass{pass_n}/{i}",
                title=f"{query[:30]} p{pass_n} #{i}",
                snippet=f"evidence pass {pass_n} #{i}",
                content=f"content pass {pass_n} #{i}",
            )
            for i in range(1, 3)
        ]


async def _run(db, checkpointer, llm, search):
    graph = build_graph(
        llm=llm,
        search=search,
        db=db,
        checkpointer=checkpointer.saver,
        max_extra_passes=_MAX_EXTRA_PASSES,
        quality_threshold=_THRESHOLD,
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
    config = {"configurable": {"thread_id": session_id}}
    final = await graph.ainvoke(
        build_initial_state(
            session_id=session_id,
            company="Acme",
            website="https://acme.com",
            objective="Prep",
            company_key="acme.com",
            max_extra_passes=_MAX_EXTRA_PASSES,
        ),
        config=config,
    )
    return session_id, final


async def test_loopback_replaces_stale_sources_and_facts(db, checkpointer):
    weak = Section.COMPANY_OVERVIEW
    llm = MockLLMClient(quality_scores=[0.5, 1.0], weak_sections=[weak])
    search = ShiftingSearch()

    session_id, final = await _run(db, checkpointer, llm, search)
    assert final["status"] == "done"

    weak_q = next(q.id for q in final["plan"].questions if q.section == weak)
    weak_facts = [f for f in final["facts"] if f.question_id == weak_q]
    assert weak_facts and all(f.attempt == 2 for f in weak_facts)

    # The weak question's facts now cite ONLY pass-2 sources — no stale pass-1.
    weak_source_ids = {f.source_id for f in weak_facts}
    sources_by_id = {s.id: s for s in final["report"].sources}
    weak_urls = [sources_by_id[sid].url for sid in weak_source_ids if sid in sources_by_id]
    assert weak_urls, "weak question lost its sources"
    assert all("pass2" in u for u in weak_urls)
    assert not any("pass1" in u for u in weak_urls)

    # No orphan in the served report: every listed source backs a surviving fact.
    cited = {f.source_id for f in final["facts"]}
    assert {s.id for s in final["report"].sources} <= cited

    # And the DB materialization matches — no stale source/fact rows linger.
    detail = await get_session_detail(db, session_id)
    assert detail is not None
    db_source_ids = {s.id for s in detail.report.sources}
    assert db_source_ids == {s.id for s in final["report"].sources}

    async with db.pool.connection() as conn:
        pass1_overview = await conn.execute(
            "SELECT count(*) FROM facts f JOIN sources s ON s.id = f.source_id"
            " WHERE f.session_id=%s AND f.question_id=%s AND s.url LIKE %s",
            (session_id, weak_q, "%pass1%"),
        )
        assert (await pass1_overview.fetchone())[0] == 0  # no weak-q pass-1 facts

        # Every source row is cited by a fact (no orphan rows after the shrink).
        orphans = await conn.execute(
            "SELECT count(*) FROM sources s WHERE s.session_id=%s"
            " AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.source_id = s.id)",
            (session_id,),
        )
        assert (await orphans.fetchone())[0] == 0
