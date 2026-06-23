"""Relational persistence for the Layer-2 entities the API serves.

``persist_results`` is the single-transaction seam (PRD §11): session row +
report + sources + facts commit together or roll back. Slice 2 does idempotent
upserts on deterministic ids; slice 5 adds full-materialization replacement
(orphan deletion) and *content-conditional* version bumps — ``reports.version``
advances only when the served report actually changed, so a resume-replay of an
identical persist is idempotent (Codex F2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain.identity import content_hash, memory_id, new_message_id, report_id
from app.domain.models import (
    ChatMessage,
    ChatPassage,
    CitationSnapshot,
    Fact,
    ResearchReport,
    Source,
)
from app.infra.database import Database


@dataclass(slots=True)
class ChunkRow:
    """An embedded chunk to persist. ``owner`` is the section value (report chunk)
    or the ``source_id`` (source chunk); ``embedding`` is the raw 1536-float vector."""

    id: str
    session_id: str
    owner: str
    chunk_ix: int
    text: str
    embedding: list[float]


def _vec_literal(embedding: list[float]) -> str:
    """pgvector text input format ``[f1,f2,…]`` — cast with ``%s::vector`` so no
    extra driver adapter is needed."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


@dataclass(slots=True)
class MemoryRow:
    """A self-contained cross-session memory fact to upsert (§7.1, §10)."""

    company_key: str
    section: str
    freshness_class: str
    url: str
    title: str
    snippet: str
    content_hash: str
    fact: str
    fact_norm: str
    embedding: list[float]
    fetched_at: datetime


@dataclass(slots=True)
class MemoryMatch:
    """A recalled memory candidate + its cosine distance to a plan question."""

    id: str
    section: str
    freshness_class: str
    url: str
    title: str
    snippet: str
    content_hash: str
    fact: str
    fact_norm: str
    fetched_at: datetime
    distance: float


def _materialization_hash(report: ResearchReport) -> str:
    """Fingerprint the *served* report (sections, unknowns, sources, quality
    score) so an identical re-persist is detectable. Excludes ``model_meta`` and
    raw facts: ``version`` is a staleness signal for what clients read (§8.1), not
    for provider metadata or internal evidence that doesn't change the report.
    ``sort_keys`` makes the encoding canonical regardless of field order."""
    canonical = json.dumps(
        report.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return content_hash(canonical)


@dataclass(slots=True)
class SessionDetail:
    id: str
    company: str
    company_key: str
    website: str | None
    objective: str
    status: str
    degraded: bool
    created_at: datetime
    updated_at: datetime
    report: ResearchReport | None
    report_version: int | None
    report_created_at: datetime | None


@dataclass(slots=True)
class SessionSummary:
    """A history-list row — no report body, just enough to list + link."""

    id: str
    company: str
    company_key: str
    website: str | None
    objective: str
    status: str
    degraded: bool
    created_at: datetime
    updated_at: datetime
    report_version: int | None


async def create_session(
    db: Database,
    *,
    session_id: str,
    company: str,
    company_key: str,
    website: str | None,
    objective: str,
    status: str,
) -> None:
    async with db.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO sessions (id, company, company_key, website, objective, status)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (session_id, company, company_key, website, objective, status),
        )
        await conn.commit()


async def set_session_status(
    db: Database, *, session_id: str, status: str, degraded: bool = False
) -> None:
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE sessions SET status = %s, degraded = %s, updated_at = now()"
            " WHERE id = %s",
            (status, degraded, session_id),
        )
        await conn.commit()


async def persist_results(
    db: Database,
    *,
    session_id: str,
    sources: list[Source],
    facts: list[Fact],
    report: ResearchReport,
    status: str,
    degraded: bool,
    model_meta: dict | None = None,
    report_chunks: list[ChunkRow] | None = None,
    source_chunks: list[ChunkRow] | None = None,
    memory_rows: list[MemoryRow] | None = None,
) -> None:
    """Write the full session materialization in one transaction.

    Beyond upserting on deterministic ids, this **replaces** the whole session
    set so a regenerate/resume is *convergent*, not merely duplicate-free: any
    ``sources`` / ``facts`` / chunks absent from the new output are deleted (a
    dropped source cascades to its ``facts`` and ``source_chunks``; a re-worded
    claim's stale predecessor is removed even though its source survives; a
    shrinking regenerate leaves no orphan ``report_chunks``/``source_chunks``).

    The pgvector chunk writes (slice 9) commit **with** the relational rows — pass
    ``report_chunks``/``source_chunks`` (possibly empty to clear them); ``None``
    leaves the chunk tables untouched (direct unit-test calls that don't chunk).
    """
    source_ids = [s.id for s in sources]
    fact_ids = [f.id for f in facts]
    report_hash = _materialization_hash(report)
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE sessions SET status = %s, degraded = %s, updated_at = now()"
                " WHERE id = %s",
                (status, degraded, session_id),
            )

            # Bump `version` (and `created_at`) only when the served materialization
            # actually changed. A resume-replay re-runs `persist` with the *same*
            # checkpointed state → identical hash → no spurious bump (Codex F2);
            # a real regenerate differs → bump, signalling chat staleness (§8.1).
            await conn.execute(
                "INSERT INTO reports (id, session_id, version, sections_jsonb,"
                " quality_score, model_meta_jsonb, content_hash)"
                " VALUES (%s, %s, 1, %s, %s, %s, %s)"
                " ON CONFLICT (session_id) DO UPDATE SET"
                " version = CASE WHEN reports.content_hash IS DISTINCT FROM EXCLUDED.content_hash"
                "   THEN reports.version + 1 ELSE reports.version END,"
                " sections_jsonb = EXCLUDED.sections_jsonb,"
                " quality_score = EXCLUDED.quality_score,"
                " model_meta_jsonb = EXCLUDED.model_meta_jsonb,"
                " content_hash = EXCLUDED.content_hash,"
                " created_at = CASE WHEN reports.content_hash IS DISTINCT FROM EXCLUDED.content_hash"
                "   THEN now() ELSE reports.created_at END",
                (
                    report_id(session_id),
                    session_id,
                    Jsonb(report.model_dump(mode="json")),
                    report.quality_score,
                    Jsonb(model_meta or {}),
                    report_hash,
                ),
            )

            # Staleness signal (§8.1): any chat message grounded on an older report
            # version is flagged stale, so the UI can warn it may cite changed
            # sources. Idempotent — only flips messages below the current version.
            await conn.execute(
                "UPDATE messages SET stale = TRUE WHERE session_id = %s"
                " AND stale = FALSE AND report_version IS NOT NULL"
                " AND report_version < (SELECT version FROM reports WHERE session_id = %s)",
                (session_id, session_id),
            )

            # Prune first, then upsert the current set. Deleting a dropped source
            # cascades to its facts and source_chunks; the explicit fact delete
            # also removes a stale predecessor on a *surviving* source (e.g. a
            # superseded re-research claim). `<> ALL('{}')` is true for every row,
            # so an empty new set correctly clears the table for this session.
            await conn.execute(
                "DELETE FROM sources WHERE session_id = %s AND id <> ALL(%s::text[])",
                (session_id, source_ids),
            )
            await conn.execute(
                "DELETE FROM facts WHERE session_id = %s AND id <> ALL(%s::text[])",
                (session_id, fact_ids),
            )

            # Chunk replacement (slice 9): same prune-then-upsert as the relational
            # rows, in the same transaction. Dropped sources already cascade their
            # source_chunks; this also removes stale chunks on *surviving* owners
            # after a shrink. An empty list clears the table for this session.
            if report_chunks is not None:
                await conn.execute(
                    "DELETE FROM report_chunks WHERE session_id = %s"
                    " AND id <> ALL(%s::text[])",
                    (session_id, [c.id for c in report_chunks]),
                )
            if source_chunks is not None:
                await conn.execute(
                    "DELETE FROM source_chunks WHERE session_id = %s"
                    " AND id <> ALL(%s::text[])",
                    (session_id, [c.id for c in source_chunks]),
                )

            async with conn.cursor() as cur:
                if sources:
                    await cur.executemany(
                        "INSERT INTO sources (id, session_id, url, title, snippet,"
                        " content_hash, fetched_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (id) DO UPDATE SET url = EXCLUDED.url,"
                        " title = EXCLUDED.title, snippet = EXCLUDED.snippet,"
                        " content_hash = EXCLUDED.content_hash,"
                        " fetched_at = EXCLUDED.fetched_at",
                        [
                            (s.id, session_id, s.url, s.title, s.snippet,
                             s.content_hash, s.fetched_at)
                            for s in sources
                        ],
                    )
                if facts:
                    await cur.executemany(
                        "INSERT INTO facts (id, session_id, question_id, section,"
                        " freshness_class, claim, claim_norm, source_id, confidence,"
                        " attempt) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (id) DO UPDATE SET section = EXCLUDED.section,"
                        " freshness_class = EXCLUDED.freshness_class,"
                        " claim = EXCLUDED.claim, confidence = EXCLUDED.confidence,"
                        " attempt = EXCLUDED.attempt",
                        [
                            (f.id, session_id, f.question_id, f.section.value,
                             f.freshness_class.value, f.claim, f.claim_norm,
                             f.source_id, f.confidence, f.attempt)
                            for f in facts
                        ],
                    )
                if report_chunks:
                    await cur.executemany(
                        "INSERT INTO report_chunks (id, session_id, section,"
                        " chunk_ix, text, embedding) VALUES (%s, %s, %s, %s, %s, %s::vector)"
                        " ON CONFLICT (id) DO UPDATE SET section = EXCLUDED.section,"
                        " chunk_ix = EXCLUDED.chunk_ix, text = EXCLUDED.text,"
                        " embedding = EXCLUDED.embedding",
                        [
                            (c.id, session_id, c.owner, c.chunk_ix, c.text,
                             _vec_literal(c.embedding))
                            for c in report_chunks
                        ],
                    )
                if source_chunks:
                    await cur.executemany(
                        "INSERT INTO source_chunks (id, session_id, source_id,"
                        " chunk_ix, text, embedding) VALUES (%s, %s, %s, %s, %s, %s::vector)"
                        " ON CONFLICT (id) DO UPDATE SET source_id = EXCLUDED.source_id,"
                        " chunk_ix = EXCLUDED.chunk_ix, text = EXCLUDED.text,"
                        " embedding = EXCLUDED.embedding",
                        [
                            (c.id, session_id, c.owner, c.chunk_ix, c.text,
                             _vec_literal(c.embedding))
                            for c in source_chunks
                        ],
                    )
                if memory_rows:
                    # Upsert cross-session memory for future warm-starts (§7.1).
                    # Self-contained — keyed by (company_key, content_hash,
                    # fact_norm), refreshing last_validated_at on conflict.
                    await cur.executemany(
                        "INSERT INTO research_memory (id, company_key, section,"
                        " freshness_class, url, title, snippet, content_hash, fact,"
                        " fact_norm, embedding, fetched_at, last_validated_at)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector,"
                        " %s, now())"
                        " ON CONFLICT (company_key, content_hash, fact_norm) DO UPDATE"
                        " SET section = EXCLUDED.section,"
                        " freshness_class = EXCLUDED.freshness_class,"
                        " url = EXCLUDED.url, title = EXCLUDED.title,"
                        " snippet = EXCLUDED.snippet, fact = EXCLUDED.fact,"
                        " embedding = EXCLUDED.embedding, last_validated_at = now()",
                        [
                            (
                                memory_id(m.company_key, m.content_hash, m.fact_norm),
                                m.company_key, m.section, m.freshness_class, m.url,
                                m.title, m.snippet, m.content_hash, m.fact, m.fact_norm,
                                _vec_literal(m.embedding), m.fetched_at,
                            )
                            for m in memory_rows
                        ],
                    )


async def list_sessions(
    db: Database, *, limit: int = 50, offset: int = 0
) -> list[SessionSummary]:
    """Session history, most-recent-first (PRD §9)."""
    async with db.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT s.id, s.company, s.company_key, s.website, s.objective,"
                " s.status, s.degraded, s.created_at, s.updated_at,"
                " r.version AS report_version"
                " FROM sessions s LEFT JOIN reports r ON r.session_id = s.id"
                " ORDER BY s.created_at DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            rows = await cur.fetchall()

    return [
        SessionSummary(
            id=row["id"],
            company=row["company"],
            company_key=row["company_key"],
            website=row["website"],
            objective=row["objective"],
            status=row["status"],
            degraded=row["degraded"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            report_version=row["report_version"],
        )
        for row in rows
    ]


async def get_session_detail(db: Database, session_id: str) -> SessionDetail | None:
    async with db.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT s.id, s.company, s.company_key, s.website, s.objective,"
                " s.status, s.degraded, s.created_at, s.updated_at,"
                " r.version AS report_version, r.sections_jsonb,"
                " r.created_at AS report_created_at"
                " FROM sessions s LEFT JOIN reports r ON r.session_id = s.id"
                " WHERE s.id = %s",
                (session_id,),
            )
            row = await cur.fetchone()

    if row is None:
        return None

    report = (
        ResearchReport.model_validate(row["sections_jsonb"])
        if row["sections_jsonb"] is not None
        else None
    )
    return SessionDetail(
        id=row["id"],
        company=row["company"],
        company_key=row["company_key"],
        website=row["website"],
        objective=row["objective"],
        status=row["status"],
        degraded=row["degraded"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        report=report,
        report_version=row["report_version"],
        report_created_at=row["report_created_at"],
    )


# --- chat (slice 10) ---------------------------------------------------------


async def retrieve_chunks(
    db: Database, *, session_id: str, query_vector: list[float], top_k: int
) -> list[ChatPassage]:
    """Top-k chunks (report + source) for a session by cosine distance to the
    query embedding — the grounded-chat retrieval (§8). Source chunks carry their
    ``source_id`` (citable); report chunks are synthesis context."""
    vec = _vec_literal(query_vector)
    async with db.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT text, source_id, kind FROM ("
                "  SELECT text, NULL::text AS source_id, 'report' AS kind,"
                "    embedding <=> %s::vector AS dist"
                "    FROM report_chunks WHERE session_id = %s"
                "  UNION ALL"
                "  SELECT text, source_id, 'source' AS kind,"
                "    embedding <=> %s::vector AS dist"
                "    FROM source_chunks WHERE session_id = %s"
                ") q ORDER BY dist ASC LIMIT %s",
                (vec, session_id, vec, session_id, top_k),
            )
            rows = await cur.fetchall()
    return [
        ChatPassage(text=r["text"], source_id=r["source_id"], kind=r["kind"])
        for r in rows
    ]


async def resolve_citations(
    db: Database, *, session_id: str, source_ids: list[str]
) -> list[CitationSnapshot]:
    """Snapshot the cited sources (url/title/snippet) from the *current* session
    sources, in the order given — stored on the message so it resolves even if a
    source is later dropped (§8.1)."""
    if not source_ids:
        return []
    async with db.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, url, title, snippet FROM sources"
                " WHERE session_id = %s AND id = ANY(%s)",
                (session_id, source_ids),
            )
            by_id = {r["id"]: r for r in await cur.fetchall()}
    return [
        CitationSnapshot(
            source_id=sid,
            url=by_id[sid]["url"],
            title=by_id[sid]["title"],
            snippet=by_id[sid]["snippet"],
        )
        for sid in source_ids
        if sid in by_id
    ]


async def insert_message(
    db: Database,
    *,
    session_id: str,
    role: str,
    content: str,
    citations: list[CitationSnapshot],
    report_version: int | None,
) -> ChatMessage:
    message_id = new_message_id()
    payload = Jsonb([c.model_dump() for c in citations])
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO messages (id, session_id, role, content, citations_jsonb,"
            " report_version) VALUES (%s, %s, %s, %s, %s, %s)"
            " RETURNING created_at",
            (message_id, session_id, role, content, payload, report_version),
        )
        created_at = (await cur.fetchone())[0]
        await conn.commit()
    return ChatMessage(
        id=message_id,
        role=role,
        content=content,
        citations=citations,
        report_version=report_version,
        stale=False,
        created_at=created_at,
    )


async def list_messages(db: Database, session_id: str) -> list[ChatMessage]:
    async with db.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, role, content, citations_jsonb, report_version, stale,"
                " created_at FROM messages WHERE session_id = %s ORDER BY created_at",
                (session_id,),
            )
            rows = await cur.fetchall()
    return [
        ChatMessage(
            id=r["id"],
            role=r["role"],
            content=r["content"],
            citations=[CitationSnapshot(**c) for c in (r["citations_jsonb"] or [])],
            report_version=r["report_version"],
            stale=r["stale"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


# --- cross-session memory (slice 11) -----------------------------------------


async def match_memory(
    db: Database,
    *,
    company_key: str,
    section: str,
    query_vector: list[float],
    exclude_ids: list[str],
    max_distance: float,
) -> MemoryMatch | None:
    """The nearest unused memory row for ``company_key`` **in the question's
    section** within ``max_distance`` (cosine), or ``None`` — the semantic bind of
    a memory fact to a plan question (§7.1). Scoping to the section keeps a fact
    from one section (e.g. Products) from ever being bound to another (e.g. Risks),
    which would fabricate a section that should stay an honest Unknown."""
    vec = _vec_literal(query_vector)
    async with db.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, section, freshness_class, url, title, snippet,"
                " content_hash, fact, fact_norm, fetched_at,"
                " (embedding <=> %s::vector) AS distance"
                " FROM research_memory"
                " WHERE company_key = %s AND section = %s AND id <> ALL(%s::text[])"
                " ORDER BY distance ASC LIMIT 1",
                (vec, company_key, section, exclude_ids),
            )
            row = await cur.fetchone()
    if row is None or row["distance"] > max_distance:
        return None
    return MemoryMatch(
        id=row["id"],
        section=row["section"],
        freshness_class=row["freshness_class"],
        url=row["url"],
        title=row["title"],
        snippet=row["snippet"],
        content_hash=row["content_hash"],
        fact=row["fact"],
        fact_norm=row["fact_norm"],
        fetched_at=row["fetched_at"],
        distance=row["distance"],
    )


async def touch_memory_validated(db: Database, memory_id_: str) -> None:
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE research_memory SET last_validated_at = now() WHERE id = %s",
            (memory_id_,),
        )
        await conn.commit()


async def recent_turns(db: Database, session_id: str, *, limit: int) -> list[dict]:
    """The last ``limit`` turns (role/content), oldest-first, for chat context."""
    async with db.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT role, content FROM messages WHERE session_id = %s"
                " ORDER BY created_at DESC LIMIT %s",
                (session_id, limit),
            )
            rows = await cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
