"""Background graph execution + live-progress fan-out.

``POST /sessions`` returns immediately; the run proceeds as a tracked asyncio
task that **streams** the compiled graph (``stream_mode="values"`` → full state
per super-step, checkpointed) and publishes a ``ProgressEvent`` snapshot per step
to the :class:`ProgressBus` so ``GET /sessions/{id}/stream`` can fan it out over
SSE. The runner is the *single* executor for a thread_id — the SSE endpoint never
runs its own ``astream`` (two concurrent runs on one thread would corrupt the
checkpoint). A run that raises before ``persist`` marks the session ``failed`` and
publishes a terminal event so streaming clients close; the honest error taxonomy
and degraded routing land in slice 8.
"""

from __future__ import annotations

import asyncio
import contextlib

from langgraph.graph.state import CompiledStateGraph

from app.db.repository import set_session_status
from app.domain.models import ProgressEvent, ProgressSource
from app.domain.state import ResearchState
from app.infra.database import Database
from app.infra.events import ProgressBus
from app.logging import get_logger

log = get_logger(__name__)

_TERMINAL = {"done", "degraded", "failed"}


class SessionBusyError(Exception):
    """Raised when a resume is requested for a run already executing in-process.

    Lives here (not in the service) so the *check-and-reserve* is atomic with the
    ``_active`` mutation inside :meth:`GraphRunner.resume` — see that method for the
    single-flight rationale.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} is already running")
        self.session_id = session_id


def _progress_from_state(session_id: str, step: int, state: ResearchState) -> ProgressEvent:
    """Project a streamed graph state into a live-progress snapshot."""
    plan = state.get("plan")
    sources = state.get("sources") or []
    status = state.get("status", "planning")
    return ProgressEvent(
        session_id=session_id,
        step=step,
        status=status,
        plan=[q.text for q in plan.questions] if plan is not None else [],
        sources=[ProgressSource(id=s.id, title=s.title, url=s.url) for s in sources],
        facts_count=len(state.get("facts") or []),
        draft_ready=state.get("draft") is not None,
        degraded=bool(state.get("degraded")),
        terminal=status in _TERMINAL,
    )


class GraphRunner:
    def __init__(self, graph: CompiledStateGraph, db: Database, bus: ProgressBus) -> None:
        self._graph = graph
        self._db = db
        self._bus = bus
        self._tasks: set[asyncio.Task] = set()
        #: session_ids with a live executor — guards against double-driving one
        #: thread_id (which would corrupt its checkpoint).
        self._active: set[str] = set()

    def is_active(self, session_id: str) -> bool:
        return session_id in self._active

    def start(self, session_id: str, initial_state: ResearchState) -> None:
        """Begin a fresh run (input = the initial state)."""
        self._spawn(session_id, initial_state)

    async def resume(self, session_id: str) -> bool:
        """Re-enter a run from its last checkpoint to attempt an upgrade (§9.1).

        Single-flight: the ``_active`` check-and-reserve happens **synchronously,
        before any ``await``**, so two concurrent resumes can't both observe the
        thread idle and double-drive one ``thread_id`` (which would corrupt its
        checkpoint). A duplicate while a run is live raises :class:`SessionBusyError`.
        The reservation is released here only when no executor is spawned (no-op or
        failure); once spawned, the run's done-callback owns the release.

        Returns ``False`` (a no-op) when the checkpoint is already at ``END`` — so
        resuming a ``done`` run does nothing. Otherwise marks the session with its
        in-flight phase (so a reconnecting stream shows progress, not a stuck
        terminal) and re-enters with ``None`` input, which continues without
        re-executing completed nodes.
        """
        if session_id in self._active:
            raise SessionBusyError(session_id)
        self._active.add(session_id)  # reserve atomically with the check (no await between)
        spawned = False
        try:
            config = {"configurable": {"thread_id": session_id}}
            snapshot = await self._graph.aget_state(config)
            if not snapshot.next:
                return False
            phase = snapshot.values.get("status", "recalling")
            if phase in _TERMINAL:  # defensive — a non-empty `next` shouldn't be terminal
                phase = "recalling"
            await set_session_status(self._db, session_id=session_id, status=phase)
            self._spawn(session_id, None)
            spawned = True
            return True
        finally:
            if not spawned:
                self._active.discard(session_id)

    def _spawn(self, session_id: str, graph_input: ResearchState | None) -> None:
        self._active.add(session_id)
        task = asyncio.create_task(
            self._run(session_id, graph_input), name=f"graph-run:{session_id}"
        )
        self._tasks.add(task)

        def _finish(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            self._active.discard(session_id)

        task.add_done_callback(_finish)

    async def _run(self, session_id: str, graph_input: ResearchState | None) -> None:
        config = {"configurable": {"thread_id": session_id}}
        step = 0
        try:
            async for state in self._graph.astream(
                graph_input, config=config, stream_mode="values"
            ):
                await self._publish(_progress_from_state(session_id, step, state))
                step += 1
            log.info("graph.run_complete", session_id=session_id)
        except Exception:
            log.exception("graph.run_failed", session_id=session_id)
            with contextlib.suppress(Exception):
                await set_session_status(self._db, session_id=session_id, status="failed")
            # Tell any connected stream the run ended so it stops waiting.
            await self._publish(
                ProgressEvent(
                    session_id=session_id, step=step, status="failed", terminal=True
                )
            )

    async def _publish(self, event: ProgressEvent) -> None:
        # A fan-out failure must never abort the run itself.
        with contextlib.suppress(Exception):
            await self._bus.publish(event)

    async def shutdown(self, *, grace: float = 5.0) -> None:
        """Drain in-flight runs before tearing down.

        A near-done run is given ``grace`` seconds to finish so its checkpoints
        flush *before* the checkpointer pool closes (cancelling mid-``aput`` would
        otherwise raise ``PoolClosed`` on a background put). Stragglers past the
        grace window are cancelled — LangGraph checkpointed per super-step, so they
        stay resumable.
        """
        tasks = list(self._tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
