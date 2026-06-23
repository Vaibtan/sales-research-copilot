"""Graph node implementations (PRD §6.1).

Nodes are built by small factories that capture their dependencies (LLM, search,
db) so the graph is assembled once at startup and the nodes stay pure functions
of ``ResearchState``. The quality loop-back, degraded routing, and real providers
arrive in later slices; the topology here is the straight-through spine
(``quality_gate`` already exists as a pass-through so slice 4 only adds routing).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from langgraph.errors import NodeError
from langgraph.types import Command

from app.db.repository import ChunkRow, persist_results
from app.domain.chunking import report_chunk_specs, source_chunk_specs
from app.domain.identity import (
    content_hash,
    fact_id,
    normalize_claim,
    normalize_url,
    question_id,
    source_id,
)
from app.domain.models import (
    DraftReport,
    ExtractedFacts,
    Fact,
    NodeErrorRecord,
    Question,
    QuestionSource,
    QualityVerdict,
    ReportItem,
    ReportSection,
    ResearchPlan,
    ResearchPlanDraft,
    ResearchReport,
    Source,
)
from app.domain.sections import CONTENT_SECTIONS, Section, freshness_for
from app.domain.state import ResearchState
from app.infra.database import Database
from app.logging import get_logger
from app.providers.base import CallSite, LLMClient, LLMTask, SearchProvider
from app.providers.embeddings import EmbeddingClient
from app.providers.contexts import (
    AnalyzeContext,
    ExtractContext,
    PlanContext,
    QualityContext,
)

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_plan_node(llm: LLMClient):
    async def plan(state: ResearchState) -> dict:
        ctx = PlanContext(
            company=state["company"],
            website=state["website"],
            objective=state["objective"],
            sections=list(CONTENT_SECTIONS),
        )
        draft = await llm.parse(
            task=LLMTask.PLAN,
            context=ctx,
            output_type=ResearchPlanDraft,
            call=CallSite(state["session_id"], "plan", 1),
        )
        questions = [
            Question(
                id=question_id(state["session_id"], i, q.text),
                text=q.text,
                section=q.section,
            )
            for i, q in enumerate(draft.questions)
        ]
        return {"plan": ResearchPlan(questions=questions), "status": "recalling"}

    return plan


def make_recall_node():
    async def recall(state: ResearchState) -> dict:
        # No-op until slice 11. Positioned after `plan` so reused facts can later
        # bind to an approved question_id.
        return {"reused_from_memory": False, "status": "gathering"}

    return recall


def make_gather_node(search: SearchProvider, *, max_extra_passes: int):
    async def gather(state: ResearchState) -> dict:
        plan = state["plan"]
        assert plan is not None
        attempt = max_extra_passes - state["retries_left"] + 1
        weak = set(state.get("weak_questions") or [])
        # Slice 4 loop-back: re-research only weak questions; else all.
        questions = [q for q in plan.questions if not weak or q.id in weak]

        sources: list[Source] = []
        assignments: list[QuestionSource] = []
        for question in questions:
            hits = await search.search(
                f"{state['company']} {question.text}", max_results=2
            )
            for hit in hits:
                sid = source_id(state["session_id"], hit.url)
                sources.append(
                    Source(
                        id=sid,
                        url=normalize_url(hit.url),
                        title=hit.title,
                        snippet=hit.snippet,
                        content_hash=content_hash(hit.content or hit.snippet),
                        fetched_at=_now(),
                    )
                )
                # Stamp the attempt so a loop-back supersedes this question's older
                # links (Codex F1) — extract then re-runs only over the fresh hits.
                assignments.append(
                    QuestionSource(
                        question_id=question.id, source_id=sid, attempt=attempt
                    )
                )
        return {"sources": sources, "assignments": assignments, "status": "gathering"}

    return gather


def make_extract_node(llm: LLMClient, *, max_extra_passes: int):
    async def extract(state: ResearchState) -> dict:
        attempt = max_extra_passes - state["retries_left"] + 1
        sources_by_id = {s.id: s for s in state["sources"]}
        questions_by_id = {q.id: q for q in state["plan"].questions}  # type: ignore[union-attr]
        weak = set(state.get("weak_questions") or [])

        facts: list[Fact] = []
        for assignment in state["assignments"]:
            if weak and assignment.question_id not in weak:
                continue  # loop-back: only re-extract weak questions (no re-pay)
            source = sources_by_id.get(assignment.source_id)
            question = questions_by_id.get(assignment.question_id)
            if source is None or question is None:
                continue
            extracted = await llm.parse(
                task=LLMTask.EXTRACT,
                context=ExtractContext(question=question, source=source, attempt=attempt),
                output_type=ExtractedFacts,
                call=CallSite(state["session_id"], "extract", attempt),
            )
            for claim in extracted.claims:
                claim_norm = normalize_claim(claim.claim)
                facts.append(
                    Fact(
                        id=fact_id(state["session_id"], question.id, source.id, claim_norm),
                        session_id=state["session_id"],
                        question_id=question.id,
                        section=question.section,
                        freshness_class=freshness_for(question.section),
                        claim=claim.claim,
                        claim_norm=claim_norm,
                        source_id=source.id,
                        confidence=claim.confidence,
                        attempt=attempt,
                    )
                )
        return {"facts": facts, "status": "analyzing"}

    return extract


def make_analyze_node(llm: LLMClient, *, max_extra_passes: int):
    async def analyze(state: ResearchState) -> dict:
        attempt = max_extra_passes - state["retries_left"] + 1
        ctx = AnalyzeContext(
            company=state["company"],
            objective=state["objective"],
            facts=state["facts"],
            sources=state["sources"],
        )
        draft = await llm.parse(
            task=LLMTask.ANALYZE,
            context=ctx,
            output_type=DraftReport,
            call=CallSite(state["session_id"], "analyze", attempt),
        )
        return {"draft": draft, "status": "grading"}

    return analyze


def make_quality_node(llm: LLMClient, *, quality_threshold: float, max_extra_passes: int):
    async def quality_gate(
        state: ResearchState,
    ) -> Command[Literal["gather", "report"]]:
        attempt = max_extra_passes - state["retries_left"] + 1
        ctx = QualityContext(
            company=state["company"],
            objective=state["objective"],
            draft=state["draft"],  # type: ignore[arg-type]
            questions=state["plan"].questions,  # type: ignore[union-attr]
            attempt=attempt,
        )
        verdict = await llm.parse(
            task=LLMTask.QUALITY,
            context=ctx,
            output_type=QualityVerdict,
            call=CallSite(state["session_id"], "quality_gate", attempt),
        )

        # Loop back only when the draft is sub-threshold, we still have budget, AND
        # the judge named specific weak questions to target. An empty weak set is
        # treated as "nothing actionable" — re-researching *everything* would be
        # both wasteful and unbounded-in-spirit, so we report what we have.
        retries_left = state["retries_left"]
        if verdict.score < quality_threshold and retries_left > 0 and verdict.weak_questions:
            log.info(
                "graph.quality_loopback",
                session_id=state["session_id"],
                score=verdict.score,
                threshold=quality_threshold,
                weak=len(verdict.weak_questions),
                retries_left=retries_left - 1,
            )
            return Command(
                goto="gather",
                update={
                    "quality": verdict,
                    "weak_questions": verdict.weak_questions,
                    "retries_left": retries_left - 1,
                    "status": "gathering",
                },
            )
        return Command(
            goto="report",
            update={"quality": verdict, "weak_questions": [], "status": "reporting"},
        )

    return quality_gate


def _degraded_draft(facts: list[Fact]) -> DraftReport:
    """Assemble an honest partial draft directly from whatever facts survived when
    ``analyze`` never produced one (degraded path, §6.1/§9.1) — sections with no
    facts become Unknowns; nothing is fabricated."""
    by_section: dict[Section, list[Fact]] = {s: [] for s in CONTENT_SECTIONS}
    for fact in facts:
        by_section[fact.section].append(fact)
    sections: list[ReportSection] = []
    unknowns: list[str] = []
    for section in CONTENT_SECTIONS:
        section_facts = by_section[section]
        if not section_facts:
            unknowns.append(
                f"No grounded information available for {section.value} "
                "(a research step did not complete)."
            )
            continue
        sections.append(
            ReportSection(
                section=section,
                items=[
                    ReportItem(text=f.claim, source_ids=[f.source_id])
                    for f in section_facts
                ],
            )
        )
    return DraftReport(sections=sections, unknowns=unknowns)


async def report_node(state: ResearchState) -> dict:
    # Degraded path: an error_handler may route here with no draft (gather/extract/
    # analyze failed) — assemble an honest partial from the surviving facts rather
    # than crash, so a partial run still yields a (cited) report, never a 500.
    draft = state.get("draft") or _degraded_draft(state["facts"])
    quality = state.get("quality")
    # Derive the served Sources from surviving fact citations (Codex F1): a
    # loop-back can leave a superseded source in the `sources` channel (merge_by_id
    # never drops), but if no surviving fact cites it, it must not reach the report
    # — and `persist`'s replacement then prunes it from the DB.
    cited = {f.source_id for f in state["facts"]}
    sources = [s for s in state["sources"] if s.id in cited]
    final = ResearchReport(
        sections=draft.sections,
        unknowns=draft.unknowns,
        sources=sources,
        quality_score=quality.score if quality else None,
    )
    return {"report": final, "status": "reporting"}


def make_error_handler(node: str):
    """Build an ``error_handler`` (langgraph ≥1.2) that converts a node failure —
    *after* its RetryPolicy is exhausted — into a degraded run: route to ``report``
    with ``degraded=True`` and record the error, so the graph yields an honest
    partial report instead of aborting (PRD §6.1, never a 500)."""

    def handle(state: ResearchState, error: NodeError) -> Command:
        original = getattr(error, "error", error)
        log.warning(
            "graph.node_degraded",
            node=node,
            session_id=state["session_id"],
            error_type=type(original).__name__,
        )
        return Command(
            goto="report",
            update={
                "degraded": True,
                "status": "reporting",
                "errors": [
                    NodeErrorRecord(
                        node=node,
                        error_type=type(original).__name__,
                        message=str(original)[:500],
                    )
                ],
            },
        )

    return handle


def make_persist_node(
    db: Database,
    *,
    model_meta: dict | None = None,
    embeddings: EmbeddingClient | None = None,
):
    meta = model_meta or {"providers": "mock"}

    async def persist(state: ResearchState) -> dict:
        report = state["report"]
        assert report is not None
        session_id = state["session_id"]
        degraded = state["degraded"]
        status = "degraded" if degraded else "done"

        # Slice 9: chunk + embed the served report and sources, then write the
        # vectors *inside* the persist transaction. Embeddings are generated through
        # the ledger (idempotent on resume); empty lists still prune stale chunks.
        report_chunks: list[ChunkRow] | None = None
        source_chunks: list[ChunkRow] | None = None
        if embeddings is not None:
            specs = report_chunk_specs(session_id, report) + source_chunk_specs(
                report.sources
            )
            report_chunks, source_chunks = [], []
            if specs:
                vectors = await embeddings.embed(
                    [s.text for s in specs],
                    call=CallSite(session_id, "embed", 1),
                )
                for spec, vector in zip(specs, vectors):
                    row = ChunkRow(
                        id=spec.id,
                        session_id=session_id,
                        owner=spec.owner,
                        chunk_ix=spec.chunk_ix,
                        text=spec.text,
                        embedding=vector,
                    )
                    target = report_chunks if spec.kind == "report" else source_chunks
                    target.append(row)

        # The report's Sources are the cited set (report_node derived them); persist
        # that set so the full-materialization replacement prunes any source the
        # loop-back left uncited (Codex F1).
        await persist_results(
            db,
            session_id=session_id,
            sources=report.sources,
            facts=state["facts"],
            report=report,
            status=status,
            degraded=degraded,
            model_meta=meta,
            report_chunks=report_chunks,
            source_chunks=source_chunks,
        )
        log.info(
            "graph.persisted",
            session_id=session_id,
            status=status,
            sources=len(report.sources),
            facts=len(state["facts"]),
            report_chunks=len(report_chunks) if report_chunks is not None else 0,
            source_chunks=len(source_chunks) if source_chunks is not None else 0,
        )
        return {"status": status}

    return persist
