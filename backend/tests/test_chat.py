"""Slice 10 — grounded follow-up chat (PRD §8, §8.1).

End-to-end over the real app + mock providers: a run completes, then chat retrieves
the persisted pgvector chunks and answers with citations; out-of-scope questions
decline; citations are self-contained snapshots that survive a source being dropped;
and a regenerate flags earlier messages stale.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.db import repository
from app.domain.models import ReportItem, ReportSection, ResearchReport
from app.domain.sections import Section
from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.asyncio


def _app():
    return create_app(Settings(log_json=False, environment="test"))


async def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create_and_finish(client) -> str:
    resp = await client.post(
        "/sessions",
        json={"company": "Acme", "website": "https://acme.com", "objective": "Prep"},
    )
    sid = resp.json()["session_id"]
    for _ in range(150):
        body = (await client.get(f"/sessions/{sid}")).json()
        if body["status"] in {"done", "degraded", "failed"}:
            break
        await asyncio.sleep(0.1)
    assert body["status"] == "done"
    return sid


def _parse_sse(chunk: str):
    events = []
    for block in chunk.split("\n\n"):
        event, data = "message", None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if data is not None:
            events.append((event, json.loads(data)))
    return events


async def _chat(client, sid, message):
    buffer = ""
    async with client.stream(
        "POST", f"/sessions/{sid}/chat", json={"message": message}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for text in resp.aiter_text():
            buffer += text
    return _parse_sse(buffer)


async def test_chat_in_scope_answer_is_cited(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            sid = await _create_and_finish(client)
            events = await _chat(client, sid, "What products and services does Acme offer?")

    tokens = [d["text"] for e, d in events if e == "token"]
    done = next(d for e, d in events if e == "done")
    assert tokens, "answer streamed no tokens"
    assert done["citations"], "in-scope answer must cite sources"
    assert all(c["url"] for c in done["citations"])  # snapshot carries the url
    assert done["stale"] is False


async def test_chat_out_of_scope_declines(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            sid = await _create_and_finish(client)
            events = await _chat(client, sid, "What is the founder's favorite hobby?")

    answer = "".join(d["text"] for e, d in events if e == "token")
    done = next(d for e, d in events if e == "done")
    assert "not in the report" in answer.lower()
    assert done["citations"] == []  # no fabrication, nothing cited


async def test_messages_history_records_user_and_assistant(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            sid = await _create_and_finish(client)
            await _chat(client, sid, "What products and services does Acme offer?")
            history = (await client.get(f"/sessions/{sid}/messages")).json()["messages"]

    roles = [m["role"] for m in history]
    assert roles == ["user", "assistant"]
    assistant = history[1]
    assert assistant["report_version"] == 1
    assert assistant["citations"]
    assert assistant["stale"] is False


async def test_chat_requires_a_ready_report(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            # A session row with no report yet → chat is 409 (not a 500).
            from app.domain.identity import new_session_id

            db = app.state.db
            not_ready = new_session_id()  # fresh id (long-lived dev DB)
            await repository.create_session(
                db,
                session_id=not_ready,
                company="Acme",
                company_key="acme.com",
                website=None,
                objective="Prep",
                status="planning",
            )
            resp = await client.post(
                f"/sessions/{not_ready}/chat", json={"message": "hi"}
            )
            assert resp.status_code == 409

            missing = await client.post(
                "/sessions/nope/chat", json={"message": "hi"}
            )
            assert missing.status_code == 404


async def test_citation_snapshot_survives_source_drop_and_marks_stale(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            sid = await _create_and_finish(client)
            await _chat(client, sid, "What products and services does Acme offer?")

            # A regenerate that drops every source AND changes the report → bumps
            # the version (stale) and removes the cited sources from the DB.
            await repository.persist_results(
                app.state.db,
                session_id=sid,
                sources=[],
                facts=[],
                report=ResearchReport(
                    sections=[
                        ReportSection(
                            section=Section.COMPANY_OVERVIEW,
                            items=[ReportItem(text="rewritten and shrunk", source_ids=[])],
                        )
                    ],
                    unknowns=[],
                    sources=[],
                    quality_score=1.0,
                ),
                status="done",
                degraded=False,
                report_chunks=[],
                source_chunks=[],
            )

            history = (await client.get(f"/sessions/{sid}/messages")).json()["messages"]

    assistant = history[1]
    # §8.1: the citation still resolves from its snapshot even though the source
    # row was dropped, and the message is now flagged stale.
    assert assistant["citations"], "snapshot citation must survive the source drop"
    assert all(c["url"] for c in assistant["citations"])
    assert assistant["stale"] is True
