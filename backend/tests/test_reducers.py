"""Unit tests for the state-channel reducers (the idempotency guarantees)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.models import Fact, QuestionSource, Source
from app.domain.reducers import merge_assignments, merge_by_id, supersede_by_question
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


def test_merge_assignments_dedupes_pairs() -> None:
    left = [QuestionSource(question_id="q1", source_id="s1")]
    right = [
        QuestionSource(question_id="q1", source_id="s1"),  # dup
        QuestionSource(question_id="q1", source_id="s2"),
    ]
    merged = merge_assignments(left, right)
    assert len(merged) == 2


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
