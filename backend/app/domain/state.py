"""``ResearchState`` — the shared graph state (PRD §6.2).

Channels carrying lists use idempotent reducers so parallel fan-in (slice 13)
and loop-back re-research (slice 4) merge safely. ``assignments`` is a transient
working channel (gather → extract) not served to the API.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from app.domain.models import (
    DraftReport,
    Fact,
    NodeErrorRecord,
    QualityVerdict,
    QuestionSource,
    ResearchPlan,
    ResearchReport,
    Source,
)
from app.domain.reducers import (
    merge_by_id,
    supersede_assignments_by_question,
    supersede_by_question,
)

Status = Literal[
    "planning",
    "awaiting_approval",
    "recalling",
    "gathering",
    "analyzing",
    "grading",
    "reporting",
    "done",
    "degraded",
    "failed",
]


class ResearchState(TypedDict):
    session_id: str
    company: str
    website: str | None
    objective: str
    company_key: str

    plan: ResearchPlan | None
    sources: Annotated[list[Source], merge_by_id]
    assignments: Annotated[list[QuestionSource], supersede_assignments_by_question]
    facts: Annotated[list[Fact], supersede_by_question]
    draft: DraftReport | None
    report: ResearchReport | None
    quality: QualityVerdict | None

    retries_left: int
    weak_questions: list[str]
    reused_from_memory: bool
    degraded: bool
    status: Status
    errors: Annotated[list[NodeErrorRecord], operator.add]
