"""Slice 6 — durable LLM-call ledger acceptance tests (PRD §6.5).

The three named guarantees, proven against a real Postgres ``llm_calls`` table:

* **concurrent-resume → single-flight:** many waiters on one key ⇒ exactly one
  provider call; the rest read the committed result.
* **crash-after-reserve-before-call → lease takeover, no deadlock:** a reservation
  whose owner died (expired lease) is taken over by a waiter and completed.
* **kill-before-commit → no double-charge:** a re-sent call after a crash carries
  the same provider idempotency key, so the provider de-dupes and charges once.

Plus the cache short-circuit and a graph-integration check that LLM calls really
route through the ledger.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.ledger import CallLedger, LedgerConfig, LedgerKey
from app.db.repository import create_session, get_session_detail
from app.domain.identity import new_session_id
from app.graph.build import build_graph, build_initial_state
from app.providers.base import LLMTask
from app.providers.mock import MockLLMClient, MockSearchProvider

pytestmark = pytest.mark.asyncio


def _ledger(
    db,
    *,
    lease: float = 5.0,
    poll: float = 0.05,
    hb: float = 1.0,
    timeout: float = 10.0,
) -> CallLedger:
    return CallLedger(
        db,
        LedgerConfig(
            lease_seconds=lease,
            heartbeat_seconds=hb,
            poll_seconds=poll,
            wait_timeout_seconds=timeout,
        ),
    )


def _key(node: str = "plan", input_hash: str = "h") -> LedgerKey:
    # session-scoped → never collides with a prior run in the long-lived dev DB.
    return LedgerKey(new_session_id(), node, 1, input_hash)


class _FakeProvider:
    """A provider that charges **once per idempotency key** — modelling OpenAI's
    idempotency window, so a re-sent request returns the cached charge."""

    def __init__(self) -> None:
        self.by_key: dict[str, dict] = {}
        self.charges = 0

    async def charge(self, idem: str) -> dict:
        if idem in self.by_key:
            return self.by_key[idem]  # de-duped — no new charge
        self.charges += 1
        resp = {"charge_id": self.charges, "idem": idem}
        self.by_key[idem] = resp
        return resp


# --- reserve / cache ---------------------------------------------------------


async def test_done_call_returns_cache_without_reinvoking_provider(db):
    ledger = _ledger(db)
    key = _key("plan", "cache")
    calls = 0

    async def produce(idem: str) -> dict:
        nonlocal calls
        calls += 1
        return {"answer": 42, "idem": idem}

    first = await ledger.run(key, produce)
    second = await ledger.run(key, produce)

    assert first == {"answer": 42, "idem": key.idempotency_key}
    assert second == first  # served from the committed row
    assert calls == 1  # the provider ran exactly once


# --- concurrent-resume → single-flight ---------------------------------------


async def test_concurrent_resume_single_flights(db):
    """Three concurrent calls on one key ⇒ one provider call; the others wait on
    the valid lease and read the committed response."""
    ledger = _ledger(db, lease=5.0, poll=0.02)
    key = _key("extract", "single")
    calls = 0

    async def produce(idem: str) -> dict:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.25)  # hold the reservation so waiters observe it
        return {"n": calls}

    results = await asyncio.gather(
        ledger.run(key, produce),
        ledger.run(key, produce),
        ledger.run(key, produce),
    )

    assert calls == 1, "provider must be called exactly once (single-flight)"
    assert all(r == {"n": 1} for r in results)


# --- crash-after-reserve-before-call → takeover, no deadlock -----------------


async def test_crash_after_reserve_before_call_takes_over_without_deadlock(db):
    ledger = _ledger(db, lease=0.3, poll=0.05, timeout=5.0)
    key = _key("analyze", "takeover")

    # A crashed owner: an in_progress reservation with an already-expired lease.
    async with db.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO llm_calls (session_id, node, attempt, input_hash, status,"
            " idempotency_key, locked_by, lease_expires_at)"
            " VALUES (%s, %s, %s, %s, 'in_progress', %s, 'dead-owner',"
            " now() - make_interval(secs => 10))",
            (key.session_id, key.node, key.attempt, key.input_hash, key.idempotency_key),
        )
        await conn.commit()

    calls = 0

    async def produce(idem: str) -> dict:
        nonlocal calls
        calls += 1
        return {"recovered": True}

    # Must NOT deadlock waiting on the dead owner — the wait_for guards that.
    result = await asyncio.wait_for(ledger.run(key, produce), timeout=5.0)
    assert result == {"recovered": True}
    assert calls == 1

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status FROM llm_calls WHERE session_id=%s AND node=%s"
            " AND attempt=%s AND input_hash=%s",
            (key.session_id, key.node, key.attempt, key.input_hash),
        )
        assert (await cur.fetchone())[0] == "done"


# --- kill-before-commit → no double-charge -----------------------------------


async def test_kill_before_commit_does_not_double_charge(db):
    """The owner pays the provider then crashes before committing the ledger. A
    waiter takes over and re-sends with the *same* idempotency key, so the provider
    de-dupes — total charge stays 1 (the §6.5 honest guarantee)."""
    provider = _FakeProvider()
    ledger = _ledger(db, lease=0.3, poll=0.05, timeout=5.0)
    key = _key("extract", "nocharge")

    async def produce(idem: str) -> dict:
        return await provider.charge(idem)

    # Owner A reserves and pays, but "crashes" before commit (no _commit call).
    assert await ledger._reserve(key, "owner-A") is True
    await produce(key.idempotency_key)
    assert provider.charges == 1

    # The crash leaves the row in_progress; force the lease to expire for takeover.
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE llm_calls SET lease_expires_at = now() - make_interval(secs => 10)"
            " WHERE session_id=%s AND node=%s AND attempt=%s AND input_hash=%s",
            (key.session_id, key.node, key.attempt, key.input_hash),
        )
        await conn.commit()

    # Waiter B takes over and re-sends — same idempotency key ⇒ no new charge.
    result = await asyncio.wait_for(ledger.run(key, produce), timeout=5.0)
    assert provider.charges == 1, "re-sent call must not be re-charged"
    assert result["charge_id"] == 1


async def test_failed_row_is_retryable(db):
    """A permanent failure marks the row ``failed`` so a later call can retry it
    (and then succeed)."""
    ledger = _ledger(db)
    key = _key("quality_gate", "retry")
    attempts = 0

    async def flaky(idem: str) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    with pytest.raises(RuntimeError):
        await ledger.run(key, flaky)

    # The failed key is retryable — the second run takes it over and commits.
    result = await ledger.run(key, flaky)
    assert result == {"ok": True}
    assert attempts == 2


# --- graph integration -------------------------------------------------------


async def test_graph_routes_llm_calls_through_ledger(db, checkpointer):
    """A full run built with a ledger writes committed ledger rows for its LLM
    calls and still produces a correct report."""
    llm = MockLLMClient()
    ledger = _ledger(db, lease=30.0)
    graph = build_graph(
        llm=llm,
        search=MockSearchProvider(),
        db=db,
        checkpointer=checkpointer.saver,
        max_extra_passes=1,
        quality_threshold=0.7,
        ledger=ledger,
    )
    session_id = new_session_id()
    await create_session(
        db,
        session_id=session_id,
        company="Acme",
        company_key="acme.com",
        website="https://acme.com",
        objective="Prep",
        status="planning",
    )
    config = {"configurable": {"thread_id": session_id}}
    final = await graph.ainvoke(
        build_initial_state(
            session_id=session_id,
            company="Acme",
            website="https://acme.com",
            objective="Prep",
            company_key="acme.com",
            max_extra_passes=1,
        ),
        config=config,
    )
    assert final["status"] == "done"

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT node, status FROM llm_calls WHERE session_id=%s", (session_id,)
        )
        rows = await cur.fetchall()
    nodes = {node for node, _ in rows}
    assert {"plan", "extract", "analyze", "quality_gate"} <= nodes
    assert all(status == "done" for _, status in rows)

    detail = await get_session_detail(db, session_id)
    assert detail is not None and detail.status == "done"
