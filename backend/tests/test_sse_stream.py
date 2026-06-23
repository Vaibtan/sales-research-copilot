"""Live SSE progress + history acceptance (slice 3).

Drives the real ASGI app (lifespan → db, checkpointer, graph, runner, Redis
progress bus) so the stream re-attaches to a genuinely in-flight background run.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.asyncio


def _app():
    return create_app(Settings(log_json=False, environment="test"))


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _create(client, company="Acme", website="https://acme.com"):
    resp = await client.post(
        "/sessions",
        json={"company": company, "website": website, "objective": "Renewal prep"},
    )
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _parse_sse(chunk: str) -> list[tuple[str, dict]]:
    """Parse an SSE text buffer into (event, data) pairs (ignores comments)."""
    events: list[tuple[str, dict]] = []
    for block in chunk.split("\n\n"):
        event = "message"
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        if data is not None:
            events.append((event, json.loads(data)))
    return events


async def _collect_stream(client, session_id, *, max_bytes=1_000_000):
    """Read the SSE stream until the `end` event, returning all parsed events."""
    buffer = ""
    async with client.stream("GET", f"/sessions/{session_id}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for text in resp.aiter_text():
            buffer += text
            if "event: end" in buffer:
                break
            assert len(buffer) < max_bytes, "stream did not terminate"
    return _parse_sse(buffer)


async def test_stream_emits_live_progress_then_end(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            session_id = await _create(client)
            events = await _collect_stream(client, session_id)

    kinds = [e for e, _ in events]
    assert "progress" in kinds
    assert kinds[-1] == "end"

    progress = [d for e, d in events if e == "progress"]
    # Intermediate outputs surface live, not just status (§6.3).
    assert any(d["plan"] for d in progress), "plan questions never streamed"
    assert any(d["sources"] for d in progress), "sources never streamed"
    assert any(d["facts_count"] > 0 for d in progress), "fact counts never streamed"
    assert any(d["draft_ready"] for d in progress), "draft-ready never streamed"

    # Status transitions advance through the §6.2 lifecycle to a terminal end.
    statuses = [d["status"] for d in progress]
    assert "gathering" in statuses
    assert events[-1][1]["status"] == "done"


async def test_reconnect_after_done_replays_snapshot(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            session_id = await _create(client)
            await _collect_stream(client, session_id)  # run to completion

            # Reconnecting after the run finished must show current progress
            # (a terminal snapshot + end), not a blank or hung stream.
            events = await _collect_stream(client, session_id)

    assert events, "reconnect produced no events"
    assert events[0][0] == "progress"
    assert events[0][1]["terminal"] is True
    assert events[-1] == ("end", {"status": "done"})


async def test_stream_unknown_session_404(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            resp = await client.get("/sessions/nope/stream")
            assert resp.status_code == 404


async def test_history_lists_most_recent_first(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            first = await _create(client, company="Alpha", website="https://alpha.com")
            second = await _create(client, company="Beta", website="https://beta.com")

            resp = await client.get("/sessions")
            assert resp.status_code == 200
            sessions = resp.json()["sessions"]

    ids = [s["id"] for s in sessions]
    assert first in ids and second in ids
    # Most-recent-first: the later-created session precedes the earlier one.
    assert ids.index(second) < ids.index(first)
    companies = {s["id"]: s["company"] for s in sessions}
    assert companies[first] == "Alpha" and companies[second] == "Beta"
