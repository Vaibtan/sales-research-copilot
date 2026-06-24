"""Deterministic mock providers.

These are real test doubles (not stubs): they thread the actual gathered source
ids through extraction and analysis so the graph produces a coherent, fully
cited 9-section report — including a genuine **Unknown** (the mock search returns
nothing for the Risks question, exercising the no-fabrication contract).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

from pydantic import BaseModel

from app.providers.base import CallSite, LLMTask, SearchHit
from app.providers.contexts import (
    AnalyzeContext,
    ChatContext,
    ExtractContext,
    PlanContext,
    QualityContext,
)
from app.domain.identity import normalize_claim
from app.domain.models import (
    ChatAnswer,
    DraftReport,
    ExtractedClaim,
    ExtractedFacts,
    PlannedQuestion,
    QualityVerdict,
    ReportItem,
    ReportSection,
    ResearchPlanDraft,
)
from app.domain.sections import CONTENT_SECTIONS, Section

# Common words the deterministic chat ignores when deciding if a question is in
# scope, so a generic phrasing ("what is the …") doesn't match every passage.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "of", "to", "for", "and",
        "or", "in", "on", "at", "by", "with", "what", "who", "how", "does", "do",
        "did", "this", "that", "s", "about", "tell", "me", "can", "you",
    }
)

_QUESTION_TEMPLATES: dict[Section, str] = {
    Section.COMPANY_OVERVIEW: "What is {company}'s overview, mission, and size?",
    Section.PRODUCTS_SERVICES: "What products and services does {company} offer?",
    Section.TARGET_CUSTOMERS: "Who are {company}'s target customers and segments?",
    Section.BUSINESS_SIGNALS: "What recent business signals (funding, growth, hiring) has {company} shown?",
    Section.RISKS_CHALLENGES: "What are the key risks and challenges facing {company}?",
    Section.DISCOVERY_QUESTIONS: "What discovery questions should a seller ask {company}?",
    Section.OUTREACH_STRATEGY: "What outreach strategy fits {company} for this objective?",
}


class MockSearchProvider:
    """Deterministic search. Returns nothing for risk-related queries so the
    Risks & Challenges section lands in Unknowns (honest-gaps demo)."""

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        call: CallSite | None = None,
    ) -> list[SearchHit]:
        if "risk" in query.lower():
            return []
        slug = "-".join(query.lower().split())[:60]
        n = max(1, min(2, max_results))
        return [
            SearchHit(
                url=f"https://sources.example/{slug}/{i}",
                title=f"{query.strip()[:48]} — reference {i}",
                snippet=f"Deterministic evidence #{i} addressing: {query.strip()}.",
                content=f"Full mock content #{i} for query '{query.strip()}'.",
            )
            for i in range(1, n + 1)
        ]


class MockLLMClient:
    """Deterministic structured-output dispatcher keyed by ``LLMTask``.

    The quality verdict is configurable so tests can drive the slice-4 loop:
    ``quality_scores`` is indexed by ``attempt`` (pass 1 → ``[0]``, pass 2 →
    ``[1]``, …, clamped) and ``weak_sections`` names which sections the judge
    flags as under-supported. Defaults reproduce the slice-2 behaviour (a single
    perfect 1.0 score, nothing weak) so the straight-through path is unchanged.
    """

    def __init__(
        self,
        *,
        quality_scores: Sequence[float] = (1.0,),
        weak_sections: Iterable[Section] = (),
    ) -> None:
        self._quality_scores = list(quality_scores)
        self._weak_sections = set(weak_sections)
        self.calls: Counter[LLMTask] = Counter()  # per-task call counts (tests)

    @property
    def quality_calls(self) -> int:
        return self.calls[LLMTask.QUALITY]

    async def parse(
        self,
        *,
        task: LLMTask,
        context: BaseModel,
        output_type: type[BaseModel],
        model: str | None = None,
        call: CallSite | None = None,
        idempotency_key: str | None = None,
    ) -> BaseModel:
        # `call` / `idempotency_key` are ledger/provider concerns the mock ignores;
        # `self.calls` counts *actual* provider invocations (one per produce()).
        self.calls[task] += 1
        match task:
            case LLMTask.PLAN:
                return self._plan(_as(context, PlanContext))
            case LLMTask.EXTRACT:
                return self._extract(_as(context, ExtractContext))
            case LLMTask.ANALYZE:
                return self._analyze(_as(context, AnalyzeContext))
            case LLMTask.QUALITY:
                return self._quality(_as(context, QualityContext))
            case LLMTask.CHAT:
                return self._chat(_as(context, ChatContext))
        raise ValueError(f"unhandled task: {task}")  # pragma: no cover

    # -- handlers -------------------------------------------------------------

    def _plan(self, ctx: PlanContext) -> ResearchPlanDraft:
        return ResearchPlanDraft(
            questions=[
                PlannedQuestion(
                    text=_QUESTION_TEMPLATES[section].format(company=ctx.company),
                    section=section,
                )
                for section in ctx.sections
            ]
        )

    def _extract(self, ctx: ExtractContext) -> ExtractedFacts:
        claim = f"{ctx.source.title}: {ctx.source.snippet}"
        # A re-research pass yields a distinct claim (so it earns a new fact id),
        # letting ``supersede_by_question`` visibly *replace* the prior attempt
        # rather than collapse to an idempotent no-op.
        if ctx.attempt > 1:
            claim = f"[re-research pass {ctx.attempt}] {claim}"
        return ExtractedFacts(claims=[ExtractedClaim(claim=claim, confidence=0.8)])

    def _analyze(self, ctx: AnalyzeContext) -> DraftReport:
        facts_by_section: dict[Section, list] = {s: [] for s in CONTENT_SECTIONS}
        for fact in ctx.facts:
            facts_by_section[fact.section].append(fact)

        sections: list[ReportSection] = []
        unknowns: list[str] = []
        for section in CONTENT_SECTIONS:
            section_facts = facts_by_section[section]
            if not section_facts:
                unknowns.append(f"No grounded information found for {section.value}.")
                continue
            sections.append(
                ReportSection(
                    section=section,
                    items=[
                        ReportItem(text=fact.claim, source_ids=[fact.source_id])
                        for fact in section_facts
                    ],
                )
            )
        return DraftReport(sections=sections, unknowns=unknowns)

    def _chat(self, ctx: ChatContext) -> ChatAnswer:
        # Deterministic grounding: answer only from passages that share a
        # *content* word with the question (stopwords ignored so a generic
        # "what is the …" doesn't spuriously match), and *require* a citable
        # source — otherwise honestly decline ("not in the report", §8).
        q_words = set(normalize_claim(ctx.question).split()) - _STOPWORDS
        relevant = [
            p
            for p in ctx.passages
            if q_words & (set(normalize_claim(p.text).split()) - _STOPWORDS)
        ]
        cited = sorted({p.source_id for p in relevant if p.source_id})
        if not cited:  # fall back to any retrieved source chunk
            cited = sorted({p.source_id for p in ctx.passages if p.source_id})
        if not relevant or not cited:
            return ChatAnswer(
                answer="That information is not in the report.", source_ids=[]
            )
        snippet = " ".join(relevant[0].text.split())[:200]
        return ChatAnswer(answer=f"Based on the report: {snippet}", source_ids=cited)

    def _quality(self, ctx: QualityContext) -> QualityVerdict:
        idx = min(ctx.attempt - 1, len(self._quality_scores) - 1)
        score = self._quality_scores[idx]
        # A perfect score names nothing weak; an imperfect one flags the
        # configured sections' questions (by id) for targeted re-research.
        if score >= 1.0:
            return QualityVerdict(score=score, gaps=[], weak_questions=[])
        weak = [q for q in ctx.questions if q.section in self._weak_sections]
        return QualityVerdict(
            score=score,
            gaps=[f"Under-supported: {q.section.value}" for q in weak],
            weak_questions=[q.id for q in weak],
        )


def _as(context: BaseModel, expected: type[BaseModel]) -> BaseModel:
    if not isinstance(context, expected):  # pragma: no cover - defensive
        raise TypeError(f"expected {expected.__name__}, got {type(context).__name__}")
    return context
