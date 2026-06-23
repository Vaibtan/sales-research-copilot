"""State-channel reducers — the idempotency heart of the graph (§6.2).

``merge_by_id`` makes parallel/loop-back *source* merges safe (dedupe by
deterministic id). ``supersede_by_question`` makes *fact* re-research convergent:
a later gather attempt for a question replaces that question's entire prior fact
set rather than accreting duplicates, while same-attempt facts from different
sources accumulate.
"""

from __future__ import annotations

from app.domain.models import Fact, QuestionSource, Source


def merge_by_id(left: list[Source] | None, right: list[Source] | None) -> list[Source]:
    """Dedupe sources by ``id``; the most recent value wins, order preserved."""
    merged: dict[str, Source] = {s.id: s for s in (left or [])}
    for source in right or []:
        merged[source.id] = source
    return list(merged.values())


def merge_assignments(
    left: list[QuestionSource] | None, right: list[QuestionSource] | None
) -> list[QuestionSource]:
    """Dedupe (question_id, source_id) links (idempotent under concurrent gather)."""
    seen: dict[tuple[str, str], QuestionSource] = {
        (a.question_id, a.source_id): a for a in (left or [])
    }
    for assignment in right or []:
        seen[(assignment.question_id, assignment.source_id)] = assignment
    return list(seen.values())


def supersede_by_question(
    left: list[Fact] | None, right: list[Fact] | None
) -> list[Fact]:
    """Supersede facts per question by attempt.

    For every question present in ``right``, drop existing facts of that question
    from a *strictly older* attempt, then merge the incoming facts (dedup by
    fact id). Same-attempt facts accumulate (multiple sources per question); a
    higher attempt (a loop-back re-research) wipes the question's older set so no
    stale predecessor survives.
    """
    incoming = right or []
    if not incoming:
        return list(left or [])

    max_attempt: dict[str, int] = {}
    for fact in incoming:
        prev = max_attempt.get(fact.question_id, fact.attempt)
        max_attempt[fact.question_id] = max(prev, fact.attempt)

    result: dict[str, Fact] = {}
    for fact in left or []:
        superseding = max_attempt.get(fact.question_id)
        if superseding is not None and fact.attempt < superseding:
            continue  # dropped — an older attempt for a re-researched question
        result[fact.id] = fact

    for fact in incoming:
        result[fact.id] = fact

    return list(result.values())
