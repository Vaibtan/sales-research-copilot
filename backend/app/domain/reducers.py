"""State-channel reducers — the idempotency heart of the graph (§6.2).

``merge_by_id`` makes parallel/loop-back *source* merges safe (dedupe by
deterministic id). ``supersede_by_question`` makes *fact* re-research convergent:
a later gather attempt for a question replaces that question's entire prior fact
set rather than accreting duplicates, while same-attempt facts from different
sources accumulate.
"""

from __future__ import annotations

from app.domain.models import Fact, QuestionSource, Source


def keep_degraded(left: bool | None, right: bool | None) -> bool:
    """OR-merge the ``degraded`` flag: once any node/worker degrades, the run stays
    degraded. Needed because the parallel ``gather_one`` fan-out (slice 13) can have
    several workers set ``degraded=True`` concurrently — a single-value channel
    would reject the concurrent writes."""
    return bool(left) or bool(right)


def merge_by_id(left: list[Source] | None, right: list[Source] | None) -> list[Source]:
    """Dedupe sources by ``id``; the most recent value wins, order preserved."""
    merged: dict[str, Source] = {s.id: s for s in (left or [])}
    for source in right or []:
        merged[source.id] = source
    return list(merged.values())


def supersede_assignments_by_question(
    left: list[QuestionSource] | None, right: list[QuestionSource] | None
) -> list[QuestionSource]:
    """Supersede (question → source) links per question by attempt — the mirror of
    :func:`supersede_by_question` for the gather→extract working channel (Codex F1).

    Same-attempt links accumulate (concurrent gather / multiple sources per
    question) and dedupe by ``(question_id, source_id)``; a higher attempt (a
    loop-back re-research) drops that question's *older* links so ``extract`` never
    re-runs over a weak question's stale predecessor sources. Untouched questions
    keep their links.
    """
    incoming = right or []
    if not incoming:
        return list(left or [])

    max_attempt: dict[str, int] = {}
    for a in incoming:
        max_attempt[a.question_id] = max(max_attempt.get(a.question_id, a.attempt), a.attempt)

    result: dict[tuple[str, str], QuestionSource] = {}
    for a in left or []:
        superseding = max_attempt.get(a.question_id)
        if superseding is not None and a.attempt < superseding:
            continue  # dropped — an older link for a re-researched question
        result[(a.question_id, a.source_id)] = a
    for a in incoming:
        result[(a.question_id, a.source_id)] = a
    return list(result.values())


def supersede_by_question(
    left: list[Fact] | None, right: list[Fact] | None
) -> list[Fact]:
    """Supersede facts per question by attempt.

    For every question present in ``right``, drop existing facts of that question
    from a *strictly older* attempt, then merge the incoming facts (dedup by
    fact id). Same-attempt facts accumulate (multiple sources per question); a
    higher attempt (a loop-back re-research) wipes the question's older set.

    Note: supersession is driven by questions that actually produce new facts. If a
    re-research yields *nothing* for a weak question (extract returns no claims),
    that question is absent from ``right`` and its prior facts are kept — the
    best-available grounded evidence — rather than dropping it to an empty section.
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
