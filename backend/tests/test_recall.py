"""Slice 11 — cross-session research memory / recall made real (PRD §7.1).

Recall warm-starts a run from a prior company's memory: fresh facts are remapped
into the session (new session-scoped source + a fact bound to the matched approved
question), stale **volatile** facts are re-derived, and the grounding invariant
holds (every report claim cites a *this-session* source). `persist` upserts memory
so a second run warm-starts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.repository import create_session, get_session_detail
from app.domain.identity import (
    content_hash,
    memory_id,
    new_session_id,
    normalize_claim,
    normalize_url,
    source_id,
)
from app.domain.sections import Section
from app.graph.build import build_graph, build_initial_state
from app.providers.embeddings import MockEmbeddingClient
from app.providers.mock import MockLLMClient, MockSearchProvider

pytestmark = pytest.mark.asyncio

_EMB = MockEmbeddingClient()


async def _seed_memory(
    db,
    *,
    company_key: str,
    section: Section,
    freshness: str,
    fact_text: str,
    url: str,
    fetched_at: datetime,
) -> None:
    [vec] = await _EMB.embed([fact_text])
    vec_lit = "[" + ",".join(repr(x) for x in vec) + "]"
    chash = content_hash(url)
    fnorm = normalize_claim(fact_text)
    async with db.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO research_memory (id, company_key, section, freshness_class,"
            " url, title, snippet, content_hash, fact, fact_norm, embedding,"
            " fetched_at, last_validated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            " %s::vector,%s, now()) ON CONFLICT (company_key, content_hash, fact_norm)"
            " DO UPDATE SET fetched_at = EXCLUDED.fetched_at",
            (
                memory_id(company_key, chash, fnorm),
                company_key,
                section.value,
                freshness,
                url,
                f"memory: {section.value}",
                fact_text[:80],
                chash,
                fact_text,
                fnorm,
                vec_lit,
                fetched_at,
            ),
        )
        await conn.commit()


def _build(db, checkpointer, **kw):
    return build_graph(
        llm=MockLLMClient(),
        search=MockSearchProvider(),
        db=db,
        checkpointer=checkpointer.saver,
        max_extra_passes=1,
        quality_threshold=0.7,
        embeddings=MockEmbeddingClient(),
        **kw,
    )


async def _run(db, graph, company_key: str):
    session_id = new_session_id()
    await create_session(
        db,
        session_id=session_id,
        company="Acme",
        company_key=company_key,
        website="https://acme.com",
        objective="Prep",
        status="planning",
    )
    final = await graph.ainvoke(
        build_initial_state(
            session_id=session_id,
            company="Acme",
            website="https://acme.com",
            objective="Prep",
            company_key=company_key,
            max_extra_passes=1,
        ),
        {"configurable": {"thread_id": session_id}},
    )
    return session_id, final


def _facts_for(final, section: Section):
    q = next(q for q in final["plan"].questions if q.section == section)
    return [f for f in final["facts"] if f.question_id == q.id]


# --- warm start + invariant --------------------------------------------------


async def test_fresh_memory_warm_starts_and_invariant_holds(db, checkpointer):
    ck = f"recall-{new_session_id()[:8]}.test"
    now = datetime.now(timezone.utc)
    # A fresh durable Overview fact (no company name, so it matches only the
    # Overview question, not others).
    await _seed_memory(
        db, company_key=ck, section=Section.COMPANY_OVERVIEW, freshness="durable",
        fact_text="Overview mission and size of the organization.",
        url="https://mem.test/overview", fetched_at=now,
    )
    graph = _build(db, checkpointer)
    session_id, final = await _run(db, graph, ck)

    assert final["status"] == "done"
    assert final["reused_from_memory"] is True

    # The Overview fact was remapped from memory (its source is the memory url),
    # and the source id is *session-scoped*.
    sources_by_id = {s.id: s for s in final["report"].sources}
    overview_facts = _facts_for(final, Section.COMPANY_OVERVIEW)
    assert overview_facts
    overview_urls = [sources_by_id[f.source_id].url for f in overview_facts]
    assert any("mem.test/overview" in u for u in overview_urls), overview_urls

    # Invariant: every report claim cites a source that exists *in this session*
    # (a deterministic session-scoped id), never another session's row.
    cited = {f.source_id for f in final["facts"]}
    report_ids = {s.id for s in final["report"].sources}
    assert cited <= report_ids
    for s in final["report"].sources:
        assert s.id == source_id(session_id, normalize_url(s.url))

    # Persisted detail agrees.
    detail = await get_session_detail(db, session_id)
    assert detail is not None and detail.report is not None


# --- freshness split ---------------------------------------------------------


async def test_freshness_split_volatile_refetched_durable_warmstarted(db, checkpointer):
    ck = f"recall-{new_session_id()[:8]}.test"
    now = datetime.now(timezone.utc)
    # Durable fact is fresh (warm-starts); volatile fact is 2 days old → stale →
    # with no revalidator (mock) it's discarded and `gather` re-derives it.
    await _seed_memory(
        db, company_key=ck, section=Section.COMPANY_OVERVIEW, freshness="durable",
        fact_text="Overview mission and size of the organization.",
        url="https://mem.test/overview", fetched_at=now,
    )
    await _seed_memory(
        db, company_key=ck, section=Section.BUSINESS_SIGNALS, freshness="volatile",
        fact_text="Recent business signals funding growth and hiring momentum.",
        url="https://mem.test/signals", fetched_at=now - timedelta(days=2),
    )
    graph = _build(
        db, checkpointer,
        memory_ttl_durable_seconds=14 * 86400,
        memory_ttl_volatile_seconds=86400,  # 1 day → the 2-day-old volatile is stale
    )
    session_id, final = await _run(db, graph, ck)
    sources_by_id = {s.id: s for s in final["report"].sources}

    # Durable Overview → warm-started from memory.
    overview_urls = [sources_by_id[f.source_id].url for f in _facts_for(final, Section.COMPANY_OVERVIEW)]
    assert any("mem.test/overview" in u for u in overview_urls)

    # Volatile Business Signals → NOT from memory; re-derived by gather.
    signal_facts = _facts_for(final, Section.BUSINESS_SIGNALS)
    assert signal_facts, "stale volatile fact should have been re-researched"
    signal_urls = [sources_by_id[f.source_id].url for f in signal_facts]
    assert all("mem.test" not in u for u in signal_urls), signal_urls
    assert any("sources.example" in u for u in signal_urls)


# --- persist upserts memory + cold start -------------------------------------


async def test_persist_upserts_memory_for_future_warmstart(db, checkpointer):
    ck = f"recall-{new_session_id()[:8]}.test"
    graph = _build(db, checkpointer)
    # First (cold) run: no memory yet → nothing reused.
    _, final = await _run(db, graph, ck)
    assert final["reused_from_memory"] is False

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM research_memory WHERE company_key = %s", (ck,)
        )
        assert (await cur.fetchone())[0] > 0  # memory upserted for next time

    # Second run on the same company_key now warm-starts from that memory.
    _, final2 = await _run(db, graph, ck)
    assert final2["reused_from_memory"] is True
