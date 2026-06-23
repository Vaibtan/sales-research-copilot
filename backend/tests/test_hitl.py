"""Slice 12 — HITL plan-approval (interrupt → edit → resume), PRD §6.1, §13 P2.

With the gate on, the graph interrupts after `plan` (status `awaiting_approval`);
`/approve-plan` resumes with the (optionally edited) questions, and the edited plan
drives the run. Stable question_ids are retained for kept questions.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.domain.models import Question
from app.domain.sections import Section
from app.graph.nodes import _apply_plan_edits
from app.main import create_app
from app.settings import Settings

# No module asyncio mark: this file mixes a sync unit test with async app tests;
# `asyncio_mode = "auto"` runs the async ones without marking the sync one.


def _app():
    return create_app(
        Settings(log_json=False, environment="test", hitl_plan_approval=True)
    )


async def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create(client) -> str:
    resp = await client.post(
        "/sessions",
        json={"company": "Acme", "website": "https://acme.com", "objective": "Prep"},
    )
    assert resp.status_code == 201
    return resp.json()["session_id"]


async def _wait_status(client, sid, wanted, *, tries=150):
    for _ in range(tries):
        body = (await client.get(f"/sessions/{sid}")).json()
        if body["status"] in wanted:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"status never reached {wanted} (last={body['status']})")


# --- unit: stable ids on edit ------------------------------------------------


def test_apply_plan_edits_keeps_stable_ids_and_mints_new() -> None:
    original = [
        Question(id="q-keep", text="old text", section=Section.COMPANY_OVERVIEW),
        Question(id="q-drop", text="dropped", section=Section.PRODUCTS_SERVICES),
    ]
    edited = [
        {"id": "q-keep", "text": "edited text", "section": "Company Overview"},
        {"text": "brand new question", "section": "Target Customers"},  # no id → new
    ]
    result = _apply_plan_edits("sess", original, edited)

    assert result[0].id == "q-keep"  # kept its stable id despite the text edit
    assert result[0].text == "edited text"
    assert result[1].id not in {"q-keep", "q-drop"}  # a fresh id for the added one
    assert all(q.id != "q-drop" for q in result)  # the dropped question is gone


# --- app: pause + plan -------------------------------------------------------


async def test_run_pauses_for_approval_and_exposes_plan(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            sid = await _create(client)
            await _wait_status(client, sid, {"awaiting_approval"})

            plan = (await client.get(f"/sessions/{sid}/plan")).json()["questions"]
            assert plan, "plan questions should be exposed for review"
            assert all(q["id"] and q["text"] and q["section"] for q in plan)
            # It really paused — no report yet.
            assert (await client.get(f"/sessions/{sid}")).json()["report"] is None


# --- app: approve edited plan drives the run ---------------------------------


async def test_approve_edited_plan_drives_the_run(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            sid = await _create(client)
            await _wait_status(client, sid, {"awaiting_approval"})
            plan = (await client.get(f"/sessions/{sid}/plan")).json()["questions"]

            # Drop the Products & Services question; keep the rest (stable ids).
            edited = [q for q in plan if q["section"] != "Products & Services"]
            assert len(edited) == len(plan) - 1
            approve = await client.post(
                f"/sessions/{sid}/approve-plan", json={"questions": edited}
            )
            assert approve.status_code == 200
            assert approve.json()["approved"] is True

            detail = await _wait_status(client, sid, {"done", "degraded"})
            assert detail["status"] == "done"
            report = detail["report"]
            rendered = {s["section"] for s in report["sections"]}
            # The dropped question's section was never researched → honest Unknown.
            assert "Products & Services" not in rendered
            assert any("Products & Services" in u for u in report["unknowns"])


async def test_approve_plan_errors(settings):
    app = _app()
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            # unknown session → 404
            missing = await client.post(
                "/sessions/nope/approve-plan",
                json={"questions": [{"text": "x", "section": "Company Overview"}]},
            )
            assert missing.status_code == 404

            # a session past approval → 409 (not awaiting)
            sid = await _create(client)
            await _wait_status(client, sid, {"awaiting_approval"})
            plan = (await client.get(f"/sessions/{sid}/plan")).json()["questions"]
            await client.post(f"/sessions/{sid}/approve-plan", json={"questions": plan})
            await _wait_status(client, sid, {"done", "degraded"})

            again = await client.post(
                f"/sessions/{sid}/approve-plan", json={"questions": plan}
            )
            assert again.status_code == 409
