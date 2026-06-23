"""Domain models — the typed payloads that flow through the graph and are
persisted. These are the *shape* contracts; identity/ids are assigned by
``app.domain.identity``.

LLM-output models (``*Draft``, ``Extracted*``, ``DraftReport``, ``QualityVerdict``)
are deliberately small and schema-clean so the real OpenAI Responses ``parse``
(slice 7) can target them directly.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.sections import FreshnessClass, Section

# --- Plan --------------------------------------------------------------------


class PlannedQuestion(BaseModel):
    """A research question as proposed by the planner LLM (no id yet)."""

    text: str
    section: Section


class ResearchPlanDraft(BaseModel):
    """Raw planner output."""

    questions: list[PlannedQuestion]


class Question(BaseModel):
    """A planned question with a stable, session-scoped id."""

    id: str
    text: str
    section: Section


class ResearchPlan(BaseModel):
    questions: list[Question]


# --- Evidence ----------------------------------------------------------------


class Source(BaseModel):
    id: str
    url: str
    title: str
    snippet: str
    content_hash: str
    fetched_at: datetime


class QuestionSource(BaseModel):
    """A (question → source) assignment produced by ``gather``, consumed by
    ``extract``. A source can answer multiple questions, so this link lives
    outside ``Source`` itself.

    ``attempt`` records which gather pass emitted the link so a loop-back can
    *supersede* a question's older assignments (Codex F1) — without it, a
    re-research over *changed* real sources would re-extract over the weak
    question's stale attempt-1 sources too.
    """

    question_id: str
    source_id: str
    attempt: int = 1


class ExtractedClaim(BaseModel):
    claim: str
    confidence: float = 0.7


class ExtractedFacts(BaseModel):
    """Raw per-(question, source) extraction output."""

    claims: list[ExtractedClaim]


class Fact(BaseModel):
    id: str
    session_id: str
    question_id: str
    section: Section
    freshness_class: FreshnessClass
    claim: str
    claim_norm: str
    source_id: str
    confidence: float
    attempt: int


# --- Report ------------------------------------------------------------------


class ReportItem(BaseModel):
    """A single claim in a section, citing the sources that support it."""

    text: str
    source_ids: list[str] = Field(default_factory=list)


class ReportSection(BaseModel):
    section: Section
    items: list[ReportItem] = Field(default_factory=list)


class DraftReport(BaseModel):
    """Analyzer output: the seven content sections + honest Unknowns."""

    sections: list[ReportSection] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class ResearchReport(BaseModel):
    """The assembled, served report: nine sections (7 content + Unknowns + Sources)."""

    sections: list[ReportSection] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    quality_score: float | None = None


# --- Chat (slice 10) ---------------------------------------------------------


class ChatPassage(BaseModel):
    """A retrieved chunk handed to the answer step. ``source_id`` is set for source
    chunks (citable) and ``None`` for report chunks (synthesis context)."""

    text: str
    source_id: str | None = None
    kind: str  # "report" | "source"


class ChatTurn(BaseModel):
    role: str
    content: str


class ChatAnswer(BaseModel):
    """Answer step output: grounded prose + the source ids it cites."""

    answer: str
    source_ids: list[str] = Field(default_factory=list)


class CitationSnapshot(BaseModel):
    """A self-contained citation stored on a message (§8.1) — resolves even if the
    source is later dropped by a regenerate."""

    source_id: str
    url: str
    title: str
    snippet: str


class ChatMessage(BaseModel):
    id: str
    role: str
    content: str
    citations: list[CitationSnapshot] = Field(default_factory=list)
    report_version: int | None = None
    stale: bool = False
    created_at: datetime


# --- Quality / errors --------------------------------------------------------


class QualityVerdict(BaseModel):
    score: float
    gaps: list[str] = Field(default_factory=list)
    weak_questions: list[str] = Field(default_factory=list)


class NodeErrorRecord(BaseModel):
    node: str
    error_type: str
    message: str


# --- Live progress (slice 3) -------------------------------------------------


class ProgressSource(BaseModel):
    """A discovered source, trimmed to what the live progress view needs."""

    id: str
    title: str
    url: str


class ProgressEvent(BaseModel):
    """A full snapshot of a run's progress at one super-step (PRD §6.3).

    Every event carries the *complete* current progress (not a delta) so a
    client that connects late or misses an event self-heals on the next one;
    ``step`` is monotonic per run so clients can de-dupe/ignore stale events.
    ``status`` is the §6.2 status literal (kept as ``str`` to avoid a circular
    import with ``app.domain.state``).
    """

    session_id: str
    step: int
    status: str
    plan: list[str] = Field(default_factory=list)
    sources: list[ProgressSource] = Field(default_factory=list)
    facts_count: int = 0
    draft_ready: bool = False
    degraded: bool = False
    terminal: bool = False
