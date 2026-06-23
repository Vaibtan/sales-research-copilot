"""API contract tests for create + detail (slice 2 acceptance).

Drives the real ASGI app through its lifespan (db, migrations, checkpointer,
graph, background runner) so this is a true end-to-end test over mock providers.
"""

from __future__ import annotations

import asyncio

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


async def _poll_until_done(client, session_id, *, attempts=150):
    for _ in range(attempts):
        resp = await client.get(f"/sessions/{session_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] in {"done", "degraded", "failed"}:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError("session did not reach a terminal status in time")


async def test_create_then_detail_renders_nine_sections(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            create = await client.post(
                "/sessions",
                json={
                    "company": "Acme",
                    "website": "https://acme.com",
                    "objective": "Prepare for a renewal conversation",
                },
            )
            assert create.status_code == 201
            payload = create.json()
            session_id = payload["session_id"]
            assert payload["status"] == "planning"

            detail = await _poll_until_done(client, session_id)

            assert detail["status"] == "done"
            assert detail["company_key"] == "acme.com"
            report = detail["report"]
            assert report is not None

            rendered = {s["section"] for s in report["sections"]}
            assert "Risks & Challenges" not in rendered  # honest gap
            assert any("Risks & Challenges" in u for u in report["unknowns"])
            assert report["sources"], "Sources list must be present"

            source_ids = {s["id"] for s in report["sources"]}
            for section in report["sections"]:
                for item in section["items"]:
                    assert item["source_ids"]
                    assert all(sid in source_ids for sid in item["source_ids"])


async def test_resume_done_session_is_noop(settings):
    """A completed run is at END — POST /resume neither re-runs nor re-versions."""
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            create = await client.post(
                "/sessions",
                json={"company": "Acme", "website": "https://acme.com", "objective": "Prep"},
            )
            session_id = create.json()["session_id"]
            detail = await _poll_until_done(client, session_id)
            assert detail["status"] == "done"
            version_before = detail["report"]["version"]

            resume = await client.post(f"/sessions/{session_id}/resume")
            assert resume.status_code == 200
            body = resume.json()
            assert body["resumed"] is False
            assert body["status"] == "done"

            after = await client.get(f"/sessions/{session_id}")
            assert after.json()["report"]["version"] == version_before  # not re-persisted


async def test_resume_unknown_session_returns_404(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            resp = await client.post("/sessions/nonexistent/resume")
            assert resp.status_code == 404


async def test_get_unknown_session_returns_404(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            resp = await client.get("/sessions/nonexistent")
            assert resp.status_code == 404


async def test_create_rejects_blank_company(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/sessions", json={"company": "  ", "objective": "x"}
            )
            assert resp.status_code == 422
