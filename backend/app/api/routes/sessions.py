"""Session endpoints (PRD §9): create, detail, history list, and live SSE stream.

The stream re-attaches to a run already executing in the background ``GraphRunner``
by **subscribing** to its progress fan-out — it never drives the graph itself. On
connect it replays the latest snapshot (so a reconnect/refresh shows current
progress, not a blank UI), then forwards live ``ProgressEvent``s until a terminal
status, where it emits ``event: end`` and closes.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.api.schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    ResumeResponse,
    SessionDetailResponse,
    SessionHistoryResponse,
    SessionSummaryOut,
)
from app.db.repository import SessionDetail
from app.domain.models import ProgressEvent, ProgressSource
from app.infra.events import ProgressBus
from app.services.sessions import SessionBusyError, SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])

_TERMINAL = {"done", "degraded", "failed"}
#: Idle gap after which the stream emits a keepalive comment (and re-checks the
#: DB for a terminal status it may have missed) — keeps proxies from closing.
_HEARTBEAT_SECONDS = 15.0


def _service(request: Request) -> SessionService:
    return request.app.state.session_service


def _bus(request: Request) -> ProgressBus:
    return request.app.state.progress_bus


@router.post(
    "",
    response_model=CreateSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a research session and start the graph",
)
async def create_session(
    body: CreateSessionRequest, request: Request
) -> CreateSessionResponse:
    session_id, run_status = await _service(request).create(
        company=body.company, website=body.website, objective=body.objective
    )
    return CreateSessionResponse(session_id=session_id, status=run_status)


@router.get(
    "",
    response_model=SessionHistoryResponse,
    summary="Session history (most-recent-first)",
)
async def list_sessions(request: Request) -> SessionHistoryResponse:
    summaries = await _service(request).list_history()
    return SessionHistoryResponse(
        sessions=[SessionSummaryOut.from_summary(s) for s in summaries]
    )


@router.get(
    "/{session_id}",
    response_model=SessionDetailResponse,
    summary="Session detail + report (when ready)",
)
async def get_session(session_id: str, request: Request) -> SessionDetailResponse:
    detail = await _service(request).get_detail(session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionDetailResponse.from_detail(detail)


@router.post(
    "/{session_id}/resume",
    response_model=ResumeResponse,
    summary="Resume a failed/degraded/interrupted run from its last checkpoint",
)
async def resume_session(session_id: str, request: Request) -> ResumeResponse:
    try:
        result = await _service(request).resume(session_id)
    except SessionBusyError as exc:
        raise HTTPException(status_code=409, detail="session is already running") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="session not found")
    run_status, resumed = result
    return ResumeResponse(session_id=session_id, status=run_status, resumed=resumed)


@router.get(
    "/{session_id}/stream",
    summary="Live node-by-node progress (Server-Sent Events)",
)
async def stream_session(session_id: str, request: Request) -> StreamingResponse:
    service = _service(request)
    bus = _bus(request)

    detail = await service.get_detail(session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="session not found")

    async def event_stream() -> AsyncIterator[str]:
        # Subscribe *before* reading the snapshot so no event is missed in the
        # gap between snapshot and the first live message.
        async with bus.subscribe(session_id) as pubsub:
            snapshot = await bus.latest(session_id) or _synthesize(detail)

            if snapshot.terminal:
                # F5: a cached *terminal* snapshot is only trustworthy if the DB
                # still agrees it's terminal. A resume may have flipped the status
                # non-terminal (and is about to publish a fresh "resumed" snapshot);
                # replaying the stale `end` here would close the stream while the
                # upgrade runs. Re-check the fresh status before honoring it.
                fresh = await service.get_detail(session_id)
                if fresh is None or fresh.status in _TERMINAL:
                    yield _sse("progress", snapshot.model_dump_json())
                    yield _sse("end", json.dumps({"status": snapshot.status}))
                    return
                # DB is non-terminal → ignore the stale terminal snapshot and show a
                # live view (the resumed snapshot if cached, else a synthesized one).
                snapshot = await bus.latest(session_id)
                if snapshot is None or snapshot.terminal:
                    snapshot = _synthesize(fresh)

            yield _sse("progress", snapshot.model_dump_json())
            last_step = snapshot.step
            while True:
                if await request.is_disconnected():
                    return

                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=_HEARTBEAT_SECONDS
                )
                if message is None:
                    # Idle: keep the connection warm and self-heal if a terminal
                    # event was somehow missed (e.g. published before subscribe).
                    fresh = await service.get_detail(session_id)
                    if fresh is not None and fresh.status in _TERMINAL:
                        final = await bus.latest(session_id) or _synthesize(fresh)
                        yield _sse("progress", final.model_dump_json())
                        yield _sse("end", json.dumps({"status": fresh.status}))
                        return
                    yield ": keepalive\n\n"
                    continue

                event = ProgressEvent.model_validate_json(message["data"])
                if event.step <= last_step:
                    continue  # stale/duplicate relative to the snapshot
                last_step = event.step
                yield _sse("progress", event.model_dump_json())
                if event.terminal:
                    yield _sse("end", json.dumps({"status": event.status}))
                    return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx)
        },
    )


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _synthesize(detail: SessionDetail) -> ProgressEvent:
    """Build a snapshot from the DB row when no cached event exists (Redis
    flushed, or a terminal run viewed fresh after the snapshot TTL)."""
    report = detail.report
    sources = (
        [ProgressSource(id=s.id, title=s.title, url=s.url) for s in report.sources]
        if report is not None
        else []
    )
    facts_count = (
        sum(len(section.items) for section in report.sections)
        if report is not None
        else 0
    )
    return ProgressEvent(
        session_id=detail.id,
        step=-1,  # below any real step so live events always supersede
        status=detail.status,
        plan=[],  # the plan isn't persisted relationally; live events carry it
        sources=sources,
        facts_count=facts_count,
        draft_ready=report is not None,
        degraded=detail.degraded,
        terminal=detail.status in _TERMINAL,
    )
