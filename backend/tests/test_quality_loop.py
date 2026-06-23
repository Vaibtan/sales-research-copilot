"""Slice 4 — self-correcting quality loop (conditional routing) acceptance tests.

These exercise the `quality_gate → gather` loop-back end-to-end over deterministic
mocks: a low first-pass score triggers exactly one *targeted* re-research pass on
the weak question, `supersede_by_question` replaces that question's whole fact set
(attempt 2, no attempt-1 survivors, no duplicates), and an exhausted retry budget
proceeds gracefully to `report` rather than spinning or erroring.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.db.repository import create_session, get_session_detail
from app.domain.identity import new_session_id
from app.domain.sections import Section
from app.graph.build import build_graph, build_initial_state
from app.providers.mock import MockLLMClient, MockSearchProvider

pytestmark = pytest.mark.asyncio

_THRESHOLD = 0.7
_MAX_EXTRA_PASSES = 1


async def _run(db, checkpointer, llm: MockLLMClient):
    """Run the full graph with a caller-supplied (configurable) LLM mock."""
    graph = build_graph(
        llm=llm,
        search=MockSearchProvider(),
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
        objective="Prep for a renewal call",
        status="planning",
    )
    config = {"configurable": {"thread_id": session_id}}
    final = await graph.ainvoke(
        build_initial_state(
            session_id=session_id,
            company="Acme",
            website="https://acme.com",
            objective="Prep for a renewal call",
            company_key="acme.com",
            max_extra_passes=_MAX_EXTRA_PASSES,
        ),
        config=config,
    )
    return session_id, final


def _question_id(final, section: Section) -> str:
    return next(q.id for q in final["plan"].questions if q.section == section)


def _facts_for(final, question_id: str) -> Sequence:
    return [f for f in final["facts"] if f.question_id == question_id]


async def test_low_score_triggers_exactly_one_targeted_repass(db, checkpointer):
    """Marquee acceptance: a low first draft → one targeted re-research pass;
    the weak question's facts are *replaced* (attempt 2 only), not duplicated."""
    weak_section = Section.COMPANY_OVERVIEW
    llm = MockLLMClient(quality_scores=[0.5, 1.0], weak_sections=[weak_section])

    session_id, final = await _run(db, checkpointer, llm)

    # Exactly one extra pass: quality graded twice, retry budget fully spent.
    assert llm.quality_calls == 2
    assert final["retries_left"] == 0
    assert final["status"] == "done"
    assert final["report"] is not None
    assert final["report"].quality_score == 1.0  # the upgraded second verdict

    weak_q = _question_id(final, weak_section)
    weak_facts = _facts_for(final, weak_q)
    assert weak_facts, "weak question should have been re-researched"
    # Replaced, not duplicated: only attempt-2 facts survive for the weak question.
    assert all(f.attempt == 2 for f in weak_facts)
    assert not any(f.attempt == 1 for f in weak_facts)

    # Every other question keeps its untouched first-pass facts.
    other_facts = [f for f in final["facts"] if f.question_id != weak_q]
    assert other_facts
    assert all(f.attempt == 1 for f in other_facts)

    # No duplicate fact ids anywhere (supersede dropped the stale predecessors).
    ids = [f.id for f in final["facts"]]
    assert len(ids) == len(set(ids))

    # The persisted report reflects the upgraded run.
    detail = await get_session_detail(db, session_id)
    assert detail is not None and detail.status == "done"
    assert detail.report is not None


async def test_retry_budget_exhausted_proceeds_to_report(db, checkpointer):
    """A draft that stays weak spends its one extra pass and then reports
    gracefully (no error, no infinite loop)."""
    weak_section = Section.COMPANY_OVERVIEW
    llm = MockLLMClient(quality_scores=[0.5, 0.5], weak_sections=[weak_section])

    _, final = await _run(db, checkpointer, llm)

    # Bounded: one extra pass only, even though the score never recovered.
    assert llm.quality_calls == 2
    assert final["retries_left"] == 0
    assert final["status"] == "done"  # graceful — not failed
    assert final["report"] is not None
    assert final["report"].quality_score == 0.5  # last (still-weak) verdict

    # The re-research still happened: the weak question is at attempt 2.
    weak_facts = _facts_for(final, _question_id(final, weak_section))
    assert weak_facts and all(f.attempt == 2 for f in weak_facts)


async def test_high_first_score_does_not_loop(db, checkpointer):
    """A passing first draft goes straight to report — no extra pass."""
    llm = MockLLMClient()  # default: score 1.0, no weak sections

    _, final = await _run(db, checkpointer, llm)

    assert llm.quality_calls == 1
    assert final["retries_left"] == _MAX_EXTRA_PASSES  # untouched
    assert final["status"] == "done"
    assert all(f.attempt == 1 for f in final["facts"])


async def test_low_score_without_weak_questions_does_not_loop(db, checkpointer):
    """Defensive: a sub-threshold score with no identifiable weak question must
    NOT loop (an empty weak set would otherwise re-research everything)."""
    llm = MockLLMClient(quality_scores=[0.5], weak_sections=[])

    _, final = await _run(db, checkpointer, llm)

    assert llm.quality_calls == 1  # graded once, no targeted work to do
    assert final["retries_left"] == _MAX_EXTRA_PASSES
    assert final["status"] == "done"
    assert all(f.attempt == 1 for f in final["facts"])
