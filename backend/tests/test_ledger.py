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
from psycopg.types.json import Jsonb

from app.db.ledger import CallLedger, LedgerConfig, LedgerKey
from app.db.repository import create_session, get_session_detail
from app.domain.identity import new_session_id
from app.graph.build import build_graph, build_initial_state
from app.providers.base import CallSite, LLMTask
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


# --- lost-ownership commit → only committed values reach the caller ----------


async def test_run_returns_committed_value_not_local_after_losing_ownership(db):
    """Codex finding: if our commit CAS fails because we lost ownership mid-flight
    and the row is **not yet done** (the takeover owner is still working), the old
    code fell through to ``return response`` — handing the caller a value the
    durable ledger never recorded. The invariant: whatever ``run`` returns must
    equal the committed row, so a resume replays the same value the run saw."""
    ledger = _ledger(db, lease=5.0, hb=100.0, poll=0.05, timeout=5.0)
    key = _key("extract", "lostown")
    took_over = {"done": False}

    async def produce(idem: str) -> dict:
        if not took_over["done"]:
            took_over["done"] = True
            # Simulate a takeover happening *during* our provider call: a different
            # owner (B) grabs the row, still ``in_progress`` (not committed) and with
            # an already-expired lease (B also stalled). Our subsequent commit CAS
            # will fail, and the row is NOT done — the exact buggy window.
            async with db.pool.connection() as conn:
                await conn.execute(
                    "UPDATE llm_calls SET locked_by='owner-B', status='in_progress',"
                    " lease_expires_at = now() - make_interval(secs => 10),"
                    " updated_at=now()"
                    " WHERE session_id=%s AND node=%s AND attempt=%s AND input_hash=%s",
                    (key.session_id, key.node, key.attempt, key.input_hash),
                )
                await conn.commit()
            return {"v": "A-first-uncommitted"}
        return {"v": "A-second-committed"}

    result = await asyncio.wait_for(ledger.run(key, produce), timeout=5.0)

    final = await ledger._read(key)
    assert final is not None and final["status"] == "done", (
        "run must not return while the key is left uncommitted"
    )
    assert result == final["response_jsonb"], (
        "caller must get the committed value, never a local uncommitted one"
    )


async def test_lost_ownership_producer_exception_re_resolves_not_raises(db):
    """Codex re-review: the lost-ownership fix must also cover the *exception* path.
    If ``produce()`` raises *after* a takeover changed ``locked_by``, our ``_fail``
    can't mark the row (owner mismatch) and the durable row is still authoritative —
    so the stale local exception must NOT escape and fail/degrade the node. ``run``
    must re-resolve (re-read / take over) exactly like the failed-commit path."""
    ledger = _ledger(db, lease=5.0, hb=100.0, poll=0.05, timeout=5.0)
    key = _key("extract", "lostown-exc")
    calls = 0

    async def produce(idem: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Simulate losing ownership *during* our call: a takeover owner (B) grabs
            # the row and also stalls (still in_progress, expired lease). Our later
            # ``_fail`` won't match locked_by, so our exception is non-authoritative.
            async with db.pool.connection() as conn:
                await conn.execute(
                    "UPDATE llm_calls SET locked_by='owner-B', status='in_progress',"
                    " lease_expires_at = now() - make_interval(secs => 10),"
                    " updated_at=now()"
                    " WHERE session_id=%s AND node=%s AND attempt=%s AND input_hash=%s",
                    (key.session_id, key.node, key.attempt, key.input_hash),
                )
                await conn.commit()
            raise RuntimeError("provider boom after losing ownership")
        return {"v": "recovered-after-takeback"}

    # The stale RuntimeError from the non-authoritative owner must not surface; run
    # re-resolves, takes the expired row back, and completes on the retry.
    result = await asyncio.wait_for(ledger.run(key, produce), timeout=5.0)
    assert result == {"v": "recovered-after-takeback"}
    assert calls == 2, "must re-resolve and re-produce, not raise the stale local error"

    final = await ledger._read(key)
    assert final is not None and final["status"] == "done"


# --- ledger key fingerprints the full output schema, not just its name -------


async def test_output_schema_change_is_a_distinct_ledger_call(db):
    """Codex finding: the ledger input hash keyed on the output *class name* — so a
    changed schema (added/removed fields) behind the same name replayed stale cached
    output across deploys. Two same-named schemas with different fields must be
    distinct ledger calls, or a resume after a schema change serves the old shape."""
    from pydantic import create_model

    from app.providers.ledger import LedgerLLMClient

    Ctx = create_model("Ctx", q=(str, "hello"))
    Out1 = create_model("SameName", a=(int, 1))
    Out2 = create_model("SameName", a=(int, 1), b=(str, "x"))  # extra field, same name

    class _CountingLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def parse(self, *, task, context, output_type, model=None,
                        idempotency_key=None):
            self.calls += 1
            return output_type()

    inner = _CountingLLM()
    client = LedgerLLMClient(inner, _ledger(db))
    ctx = Ctx()
    call = CallSite(new_session_id(), "plan", 1)  # identical call-site for both

    await client.parse(task=LLMTask.PLAN, context=ctx, output_type=Out1, call=call)
    await client.parse(task=LLMTask.PLAN, context=ctx, output_type=Out2, call=call)

    assert inner.calls == 2, (
        "a changed output schema must be a distinct ledger call, not a stale cache hit"
    )


# --- search calls route through the durable ledger ---------------------------


async def test_search_routes_through_ledger_not_repaid_on_resume(db):
    """Codex finding: ``gather_one`` called ``search.search()`` directly, bypassing
    the ledger — so a crash/resume re-paid the external Tavily search. Behind the
    ledger, the same call-site is served from the committed row (no re-pay); a
    loop-back re-research (a higher ``attempt``) is a distinct, fresh search."""
    from app.providers.base import SearchHit
    from app.providers.ledger import LedgerSearchProvider

    class _CountingSearch:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query, *, max_results=5, call=None):
            self.calls += 1
            return [SearchHit(url=f"https://x/{self.calls}", title="t", snippet="s")]

    inner = _CountingSearch()
    search = LedgerSearchProvider(inner, _ledger(db))
    sid = new_session_id()
    call = CallSite(sid, "gather_one", 1)

    first = await search.search("acme products", max_results=2, call=call)
    second = await search.search("acme products", max_results=2, call=call)
    assert inner.calls == 1, "a resume must read the committed search, not re-pay"
    assert [h.url for h in first] == [h.url for h in second]

    # A loop-back re-research (attempt=2) is a fresh, distinct search.
    await search.search(
        "acme products", max_results=2, call=CallSite(sid, "gather_one", 2)
    )
    assert inner.calls == 2


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
    # gather_one proves the Tavily search path is ledgered too (Codex follow-up), not
    # just the LLM calls — so a resume re-pays no external search.
    assert {"plan", "gather_one", "extract", "analyze", "quality_gate"} <= nodes
    assert all(status == "done" for _, status in rows)

    detail = await get_session_detail(db, session_id)
    assert detail is not None and detail.status == "done"
