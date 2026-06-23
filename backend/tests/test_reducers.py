"""Unit tests for the state-channel reducers (the idempotency guarantees)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.models import Fact, QuestionSource, Source
from app.domain.reducers import (
    merge_by_id,
    supersede_assignments_by_question,
    supersede_by_question,
)
from app.domain.sections import FreshnessClass, Section


def _source(id_: str, url: str) -> Source:
    return Source(
        id=id_,
        url=url,
        title=f"title-{id_}",
        snippet="snip",
        content_hash="h",
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _fact(id_: str, question_id: str, source_id: str, attempt: int) -> Fact:
    return Fact(
        id=id_,
        session_id="s",
        question_id=question_id,
        section=Section.COMPANY_OVERVIEW,
        freshness_class=FreshnessClass.DURABLE,
        claim=f"claim-{id_}",
        claim_norm=f"claim-{id_}",
        source_id=source_id,
        confidence=0.8,
        attempt=attempt,
    )


# --- merge_by_id -------------------------------------------------------------


def test_merge_by_id_dedupes_and_right_wins() -> None:
    left = [_source("a", "http://a"), _source("b", "http://b")]
    right = [_source("b", "http://b-updated"), _source("c", "http://c")]

    merged = merge_by_id(left, right)

    by_id = {s.id: s for s in merged}
    assert set(by_id) == {"a", "b", "c"}
    assert by_id["b"].url == "http://b-updated"  # right wins


def test_merge_by_id_handles_empty_left() -> None:
    assert merge_by_id(None, [_source("a", "http://a")]) == [_source("a", "http://a")]


def test_supersede_assignments_dedupes_same_attempt_pairs() -> None:
    left = [QuestionSource(question_id="q1", source_id="s1", attempt=1)]
    right = [
        QuestionSource(question_id="q1", source_id="s1", attempt=1),  # dup
        QuestionSource(question_id="q1", source_id="s2", attempt=1),
    ]
    merged = supersede_assignments_by_question(left, right)
    assert {(a.question_id, a.source_id) for a in merged} == {("q1", "s1"), ("q1", "s2")}


def test_supersede_assignments_drops_old_attempt_for_reresearched_question() -> None:
    """F1: a loop-back (attempt 2) over a question drops that question's stale
    attempt-1 links so extract never re-runs over the old sources — other
    questions are untouched."""
    left = [
        QuestionSource(question_id="q1", source_id="old", attempt=1),
        QuestionSource(question_id="q2", source_id="keep", attempt=1),
    ]
    right = [QuestionSource(question_id="q1", source_id="fresh", attempt=2)]

    merged = supersede_assignments_by_question(left, right)

    assert {(a.question_id, a.source_id) for a in merged} == {
        ("q1", "fresh"),  # q1's attempt-2 link
        ("q2", "keep"),  # q2 untouched
    }
    assert not any(a.source_id == "old" for a in merged)  # stale link dropped


# --- supersede_by_question ---------------------------------------------------


def test_same_attempt_facts_accumulate_across_sources() -> None:
    """Two sources answering one question in the same pass both survive."""
    left = [_fact("f1", "q1", "src1", attempt=1)]
    right = [_fact("f2", "q1", "src2", attempt=1)]

    merged = supersede_by_question(left, right)

    assert {f.id for f in merged} == {"f1", "f2"}


def test_higher_attempt_supersedes_whole_question_set() -> None:
    """A loop-back (attempt 2) wipes attempt-1 facts for that question only."""
    left = [
        _fact("f1", "q1", "src1", attempt=1),
        _fact("f2", "q1", "src2", attempt=1),
        _fact("f3", "q2", "src3", attempt=1),  # different question — untouched
    ]
    right = [_fact("f4", "q1", "src4", attempt=2)]

    merged = supersede_by_question(left, right)
    by_id = {f.id for f in merged}

    assert by_id == {"f3", "f4"}  # q1's attempt-1 facts dropped, q2 kept


def test_identical_fact_id_is_idempotent() -> None:
    """Re-emitting the same fact (same id) does not duplicate."""
    fact = _fact("f1", "q1", "src1", attempt=1)
    merged = supersede_by_question([fact], [fact])
    assert len(merged) == 1


def test_empty_update_returns_left_unchanged() -> None:
    left = [_fact("f1", "q1", "src1", attempt=1)]
    assert supersede_by_question(left, []) == left
