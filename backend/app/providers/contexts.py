"""Typed per-task LLM input contexts.

Passing structured contexts (not pre-rendered strings) keeps the node code
identical across mock and real clients: the real adapter serializes these to the
Responses ``input``; the mock reads their fields directly.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.domain.models import ChatPassage, ChatTurn, DraftReport, Fact, Question, Source
from app.domain.sections import Section


class PlanContext(BaseModel):
    company: str
    website: str | None
    objective: str
    sections: list[Section]


class ExtractContext(BaseModel):
    question: Question
    source: Source
    # Which research pass this is (1 = first, ≥2 = a quality-loop re-research).
    # A re-research pass may look harder / differently than the first.
    attempt: int = 1


class AnalyzeContext(BaseModel):
    company: str
    objective: str
    facts: list[Fact]
    sources: list[Source]


class QualityContext(BaseModel):
    company: str
    objective: str
    draft: DraftReport
    # The approved questions, so the judge can name which ones are under-supported
    # (its ``weak_questions`` are returned as ``question_id``s).
    questions: list[Question]
    attempt: int = 1


class ChatContext(BaseModel):
    """Grounded-chat answer input: the question, the retrieved passages (the only
    knowledge the answer may use), and the recent conversation turns."""

    question: str
    passages: list[ChatPassage]
    history: list[ChatTurn]
