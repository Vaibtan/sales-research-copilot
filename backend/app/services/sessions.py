"""Session orchestration: create (persist row + kick off the graph) and read."""

from __future__ import annotations

from app.db import repository
from app.db.repository import SessionDetail, SessionSummary
from app.domain.company import company_key as compute_company_key
from app.domain.identity import new_session_id
from app.graph.build import build_initial_state
from app.graph.runner import GraphRunner, SessionBusyError
from app.infra.database import Database
from app.logging import get_logger

log = get_logger(__name__)

# Re-exported so the API route (and tests) keep importing it from the service.
__all__ = ["SessionService", "SessionBusyError"]


class SessionService:
    def __init__(self, db: Database, runner: GraphRunner, *, max_extra_passes: int) -> None:
        self._db = db
        self._runner = runner
        self._max_extra_passes = max_extra_passes

    async def create(
        self, *, company: str, website: str | None, objective: str
    ) -> tuple[str, str]:
        session_id = new_session_id()
        key = compute_company_key(website=website, company=company)

        await repository.create_session(
            self._db,
            session_id=session_id,
            company=company,
            company_key=key,
            website=website,
            objective=objective,
            status="planning",
        )

        initial_state = build_initial_state(
            session_id=session_id,
            company=company,
            website=website,
            objective=objective,
            company_key=key,
            max_extra_passes=self._max_extra_passes,
        )
        self._runner.start(session_id, initial_state)
        log.info("session.created", session_id=session_id, company_key=key)
        return session_id, "planning"

    async def get_detail(self, session_id: str) -> SessionDetail | None:
        return await repository.get_session_detail(self._db, session_id)

    async def resume(self, session_id: str) -> tuple[str, bool] | None:
        """Attempt to re-enter a run from its checkpoint (PRD §9.1, Codex F3).

        Returns ``None`` if the session is unknown (→ 404). Otherwise returns the
        post-resume ``(status, resumed)``: ``resumed`` is ``False`` for an
        already-finished run (no-op). Raises :class:`SessionBusyError` if the run
        is still executing in this process (→ 409).

        The no-op gate is on the **persisted status** ``done`` (Codex F3) — *not* on
        the checkpoint being at END — so a ``degraded``/``failed`` run that ran all
        the way to END (error_handler → report → persist) is still upgradable. For
        that case we hand the runner a fresh initial state to re-attempt from START;
        the ledger means successful nodes aren't re-paid.
        """
        detail = await repository.get_session_detail(self._db, session_id)
        if detail is None:
            return None
        # A successful run is terminal — resume is a no-op even during the brief
        # window where its executor is still flushing the final checkpoint.
        if detail.status == "done":
            return "done", False

        # Fresh state to re-attempt a degraded/failed run that reached END. The
        # runner uses it only when the checkpoint has no remaining work; a killed
        # mid-run (next non-empty) is continued from its checkpoint instead.
        fresh_state = build_initial_state(
            session_id=session_id,
            company=detail.company,
            website=detail.website,
            objective=detail.objective,
            company_key=detail.company_key,
            max_extra_passes=self._max_extra_passes,
        )
        # No racy is_active() pre-check here: the runner reserves the session
        # atomically and raises SessionBusyError if a run is already live (F4).
        resumed = await self._runner.resume(session_id, fresh_state=fresh_state)
        fresh = await repository.get_session_detail(self._db, session_id)
        status = fresh.status if fresh is not None else detail.status
        log.info("session.resume", session_id=session_id, resumed=resumed, status=status)
        return status, resumed

    async def pending_plan(self, session_id: str):
        """The proposed plan questions for the HITL review screen, or ``None`` if
        the session is unknown (the list is empty if there is no plan yet)."""
        detail = await repository.get_session_detail(self._db, session_id)
        if detail is None:
            return None
        return await self._runner.snapshot_plan(session_id) or []

    async def approve_plan(
        self, session_id: str, edited_questions: list[dict]
    ) -> tuple[str, bool]:
        """Resume a run paused at the HITL gate with the (edited) questions (§9).
        Raises :class:`SessionBusyError` if a run is already executing."""
        approved = await self._runner.approve_plan(session_id, edited_questions)
        fresh = await repository.get_session_detail(self._db, session_id)
        status = fresh.status if fresh is not None else "recalling"
        log.info(
            "session.approve_plan",
            session_id=session_id,
            approved=approved,
            questions=len(edited_questions),
        )
        return status, approved

    async def list_history(self, *, limit: int = 50, offset: int = 0) -> list[SessionSummary]:
        return await repository.list_sessions(self._db, limit=limit, offset=offset)
