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

from app.domain.identity import content_hash, report_id
from app.domain.models import Fact, ResearchReport, Source
from app.infra.database import Database


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
) -> None:
    """Write the full session materialization in one transaction.

    Beyond upserting on deterministic ids, this **replaces** the whole session
    set so a regenerate/resume is *convergent*, not merely duplicate-free: any
    ``sources`` / ``facts`` absent from the new output are deleted (a dropped
    source cascades to its ``facts`` and ``source_chunks``; a re-worded claim's
    stale predecessor is removed even though its source survives). ``report_chunks``
    replacement lands with the vector layer in slice 9.
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
