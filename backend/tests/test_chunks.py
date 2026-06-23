"""Slice 9 — pgvector chunking & embeddings on persist (PRD §7, §10).

Chunks for the report + each source are embedded (through the ledger) and written
*inside* the persist transaction, and the slice-5 replacement rules extend to them
(a shrinking regenerate leaves no orphan chunks).
"""

from __future__ import annotations

import pytest

from app.db.ledger import CallLedger, LedgerConfig
from app.db.repository import ChunkRow, create_session, persist_results
from app.domain.chunking import chunk_text, report_chunk_specs, source_chunk_specs
from app.domain.identity import new_session_id, report_chunk_id, source_chunk_id
from app.domain.models import (
    ReportItem,
    ReportSection,
    ResearchReport,
    Source,
)
from app.domain.sections import Section
from app.graph.build import build_graph, build_initial_state
from app.providers.embeddings import EMBEDDING_DIM, MockEmbeddingClient
from app.providers.mock import MockLLMClient, MockSearchProvider
from datetime import datetime, timezone

# No module-level asyncio mark: this file mixes sync (chunk_text) and async (DB)
# tests; `asyncio_mode = "auto"` runs the async ones without marking the sync ones.


# --- chunk_text unit ---------------------------------------------------------


def test_chunk_text_respects_max_and_paragraphs() -> None:
    text = "para one." + "\n\n" + "para two is here."
    chunks = chunk_text(text, max_chars=20)
    assert chunks  # split into >=1
    assert all(len(c) <= 20 for c in chunks)
    assert "".join(c.replace("\n\n", "") for c in chunks).replace(" ", "")  # non-empty


def test_chunk_text_hard_splits_a_long_paragraph() -> None:
    chunks = chunk_text("x" * 50, max_chars=20)
    assert len(chunks) == 3
    assert all(len(c) <= 20 for c in chunks)


def test_chunk_text_empty_is_empty() -> None:
    assert chunk_text("   ") == []


def test_report_and_source_specs_cover_content() -> None:
    report = ResearchReport(
        sections=[
            ReportSection(
                section=Section.COMPANY_OVERVIEW,
                items=[ReportItem(text="Acme builds widgets.", source_ids=["s1"])],
            )
        ],
        unknowns=["No risk info."],
        sources=[_source("sid", "s1")],
    )
    rspecs = report_chunk_specs("sess", report)
    sspecs = source_chunk_specs(report.sources)
    assert any(s.owner == "Company Overview" for s in rspecs)
    assert any(s.owner == "Unknowns" for s in rspecs)
    assert sspecs and sspecs[0].owner == "sid"


# --- builders ----------------------------------------------------------------


def _source(sid: str, label: str) -> Source:
    return Source(
        id=sid,
        url=f"https://example.test/{label}",
        title=f"title-{label}",
        snippet="snippet text",
        content_hash="h",
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _report(sources: list[Source]) -> ResearchReport:
    return ResearchReport(
        sections=[
            ReportSection(
                section=Section.COMPANY_OVERVIEW,
                items=[ReportItem(text="claim", source_ids=[s.id for s in sources])],
            )
        ],
        unknowns=[],
        sources=sources,
        quality_score=1.0,
    )


def _vec() -> list[float]:
    return [0.1] * EMBEDDING_DIM


def _rchunk(session_id: str, section: str, ix: int) -> ChunkRow:
    return ChunkRow(
        id=report_chunk_id(session_id, section, ix),
        session_id=session_id,
        owner=section,
        chunk_ix=ix,
        text=f"r-{section}-{ix}",
        embedding=_vec(),
    )


def _schunk(session_id: str, source_id: str, ix: int) -> ChunkRow:
    return ChunkRow(
        id=source_chunk_id(source_id, ix),
        session_id=session_id,
        owner=source_id,
        chunk_ix=ix,
        text=f"s-{source_id}-{ix}",
        embedding=_vec(),
    )


async def _new_session(db) -> str:
    session_id = new_session_id()
    await create_session(
        db,
        session_id=session_id,
        company="Acme",
        company_key="acme.com",
        website="https://acme.com",
        objective="Prep",
        status="planning",
    )
    return session_id


async def _chunk_counts(db, session_id: str) -> tuple[int, int]:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM report_chunks WHERE session_id=%s", (session_id,)
        )
        rc = (await cur.fetchone())[0]
        cur = await conn.execute(
            "SELECT count(*) FROM source_chunks WHERE session_id=%s", (session_id,)
        )
        sc = (await cur.fetchone())[0]
    return rc, sc


# --- graph: chunks written with embeddings -----------------------------------


async def test_run_persists_chunks_with_embeddings_via_ledger(db, checkpointer):
    ledger = CallLedger(db, LedgerConfig(lease_seconds=30.0))
    graph = build_graph(
        llm=MockLLMClient(),
        search=MockSearchProvider(),
        db=db,
        checkpointer=checkpointer.saver,
        max_extra_passes=1,
        quality_threshold=0.7,
        ledger=ledger,
        embeddings=MockEmbeddingClient(),
    )
    session_id = await _new_session(db)
    config = {"configurable": {"thread_id": session_id}}
    final = await graph.ainvoke(
        build_initial_state(
            session_id=session_id,
            company="Acme",
            website="https://acme.com",
            objective="Prep",
            company_key="acme.com",
            max_extra_passes=1,
        ),
        config=config,
    )
    assert final["status"] == "done"

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*), min(vector_dims(embedding)), max(vector_dims(embedding))"
            " FROM report_chunks WHERE session_id=%s",
            (session_id,),
        )
        count, dmin, dmax = await cur.fetchone()
        assert count > 0 and dmin == EMBEDDING_DIM and dmax == EMBEDDING_DIM

        # Each served source got at least one chunk.
        cur = await conn.execute(
            "SELECT count(DISTINCT source_id) FROM source_chunks WHERE session_id=%s",
            (session_id,),
        )
        chunked_sources = (await cur.fetchone())[0]
        assert chunked_sources == len(final["report"].sources)

        # Embeddings were generated through the ledger (idempotent on resume).
        cur = await conn.execute(
            "SELECT count(*) FROM llm_calls WHERE session_id=%s AND node='embed'"
            " AND status='done'",
            (session_id,),
        )
        assert (await cur.fetchone())[0] >= 1


# --- shrink replacement extends to chunks ------------------------------------


async def test_shrinking_regenerate_leaves_no_orphan_chunks(db):
    session_id = await _new_session(db)
    s1 = _source(f"{session_id}-s1", "s1")

    await persist_results(
        db,
        session_id=session_id,
        sources=[s1],
        facts=[],
        report=_report([s1]),
        status="done",
        degraded=False,
        report_chunks=[_rchunk(session_id, "Company Overview", 0), _rchunk(session_id, "Company Overview", 1)],
        source_chunks=[_schunk(session_id, s1.id, 0), _schunk(session_id, s1.id, 1)],
    )
    assert await _chunk_counts(db, session_id) == (2, 2)

    # Shrink: fewer chunks for the surviving source + section.
    await persist_results(
        db,
        session_id=session_id,
        sources=[s1],
        facts=[],
        report=_report([s1]),
        status="done",
        degraded=False,
        report_chunks=[_rchunk(session_id, "Company Overview", 0)],
        source_chunks=[_schunk(session_id, s1.id, 0)],
    )
    assert await _chunk_counts(db, session_id) == (1, 1)  # stale chunks pruned


async def test_dropping_source_cascades_to_its_chunks(db):
    session_id = await _new_session(db)
    s1 = _source(f"{session_id}-s1", "s1")
    s2 = _source(f"{session_id}-s2", "s2")

    await persist_results(
        db, session_id=session_id, sources=[s1, s2], facts=[], report=_report([s1, s2]),
        status="done", degraded=False,
        report_chunks=[_rchunk(session_id, "Company Overview", 0)],
        source_chunks=[_schunk(session_id, s1.id, 0), _schunk(session_id, s2.id, 0)],
    )
    assert await _chunk_counts(db, session_id) == (1, 2)

    # Drop s2 entirely → its source_chunks cascade away.
    await persist_results(
        db, session_id=session_id, sources=[s1], facts=[], report=_report([s1]),
        status="done", degraded=False,
        report_chunks=[_rchunk(session_id, "Company Overview", 0)],
        source_chunks=[_schunk(session_id, s1.id, 0)],
    )
    assert await _chunk_counts(db, session_id) == (1, 1)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM source_chunks WHERE source_id=%s", (s2.id,)
        )
        assert (await cur.fetchone())[0] == 0  # s2's chunks gone
