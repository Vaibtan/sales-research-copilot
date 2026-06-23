"""The durable LLM-call ledger (PRD §6.5) — token idempotency across crash/resume.

LangGraph checkpoints *between* super-steps, so a crash mid-node re-runs that node
on resume. Without the ledger that would re-pay every provider call the node had
already made. The ledger wraps each call in a durable reserve → (single-flight |
lease-takeover) → call-with-provider-idempotency-key → commit protocol, keyed by
``(session_id, node, attempt, input_hash)`` and backed by Postgres so it survives
the crash.

Guarantees (deliberately not absolute, per §6.5):
* A call already **committed** (`status='done'`) is returned from the ledger with
  **no provider call** — never re-paid.
* Concurrent waiters on one key **single-flight**: exactly one runs the provider
  call while a valid lease holds; the rest wait and read the cached result.
* If the owner **crashes** (lease expires), a waiter **takes over** via a
  compare-and-swap on ``locked_by`` — no deadlock. Both the original (if it
  revives) and the new owner send the **same provider idempotency key**, so the
  provider de-dupes and charges once.

Each step is its own committed statement (not one long transaction) so other
waiters on other connections observe state transitions immediately. Lease math is
evaluated **DB-side** (``now()``) to avoid app/DB clock skew.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain.identity import content_hash
from app.infra.database import Database
from app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LedgerKey:
    """The durable identity of a single provider call."""

    session_id: str
    node: str
    attempt: int
    input_hash: str

    @property
    def idempotency_key(self) -> str:
        """The provider idempotency key = the ledger key (§6.5 step 3), so a
        re-sent request after a crash/takeover is de-duped by the provider."""
        return content_hash(
            f"{self.session_id}\x1f{self.node}\x1f{self.attempt}\x1f{self.input_hash}"
        )


class LedgerTimeout(Exception):
    """A single-flight wait exceeded the configured deadline (the owner neither
    committed nor let its lease expire within ``wait_timeout``)."""


@dataclass(slots=True)
class LedgerConfig:
    lease_seconds: float = 30.0  # how long a reservation is owned before takeover
    heartbeat_seconds: float = 10.0  # owner extends its lease this often in-flight
    poll_seconds: float = 0.25  # waiter re-checks a held reservation this often
    wait_timeout_seconds: float = 120.0  # give up single-flighting after this


class CallLedger:
    """Postgres-backed durable call ledger (the ``llm_calls`` table)."""

    def __init__(self, db: Database, config: LedgerConfig | None = None) -> None:
        self._db = db
        self._cfg = config or LedgerConfig()

    async def run(self, key: LedgerKey, produce) -> dict:
        """Return the response for ``key``, paying the provider at most once.

        ``produce(idempotency_key: str) -> Awaitable[dict]`` performs the actual
        provider call and returns a JSON-serialisable dict. It is invoked only by
        the owner of the reservation; a cached/`done` row short-circuits it.
        """
        deadline = self._cfg.wait_timeout_seconds
        waited = 0.0
        while True:
            owner = uuid4().hex
            if await self._reserve(key, owner):
                return await self._produce_and_commit(key, owner, produce)

            row = await self._read(key)
            if row is None:
                continue  # raced with a delete/expiry — retry the reserve

            status = row["status"]
            if status == "done":
                return row["response_jsonb"]
            if status == "failed":
                if await self._take_failed(key, owner):
                    return await self._produce_and_commit(key, owner, produce)
                continue  # someone else grabbed the retry — re-read
            # status == 'in_progress'
            if row["lease_expired"]:
                if await self._take_expired(key, owner, prev_owner=row["locked_by"]):
                    log.info(
                        "ledger.takeover", node=key.node, session_id=key.session_id
                    )
                    return await self._produce_and_commit(key, owner, produce)
                continue  # lost the takeover race — re-read
            # A valid lease holds → single-flight: wait for the owner to commit.
            if waited >= deadline:
                raise LedgerTimeout(f"timed out waiting on {key.node} reservation")
            await asyncio.sleep(self._cfg.poll_seconds)
            waited += self._cfg.poll_seconds

    # -- ownership transitions (each commits independently) -------------------

    async def _reserve(self, key: LedgerKey, owner: str) -> bool:
        """Atomically insert a fresh ``in_progress`` reservation. Returns True iff
        we won the insert (no existing row)."""
        async with self._db.pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO llm_calls (session_id, node, attempt, input_hash,"
                " status, idempotency_key, locked_by,"
                " lease_expires_at, updated_at)"
                " VALUES (%s, %s, %s, %s, 'in_progress', %s, %s,"
                " now() + make_interval(secs => %s), now())"
                " ON CONFLICT (session_id, node, attempt, input_hash) DO NOTHING"
                " RETURNING locked_by",
                (
                    key.session_id, key.node, key.attempt, key.input_hash,
                    key.idempotency_key, owner, self._cfg.lease_seconds,
                ),
            )
            won = await cur.fetchone() is not None
            await conn.commit()
        return won

    async def _read(self, key: LedgerKey) -> dict | None:
        async with self._db.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT status, response_jsonb, locked_by,"
                    " (lease_expires_at <= now()) AS lease_expired"
                    " FROM llm_calls WHERE session_id=%s AND node=%s"
                    " AND attempt=%s AND input_hash=%s",
                    (key.session_id, key.node, key.attempt, key.input_hash),
                )
                return await cur.fetchone()

    async def _take_expired(self, key: LedgerKey, owner: str, *, prev_owner) -> bool:
        """CAS-take an ``in_progress`` row whose lease expired. The
        ``locked_by = prev_owner`` guard means only one waiter wins; once taken,
        the owner column changes and other waiters' CAS fails."""
        async with self._db.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE llm_calls SET locked_by=%s,"
                " lease_expires_at = now() + make_interval(secs => %s), updated_at=now()"
                " WHERE session_id=%s AND node=%s AND attempt=%s AND input_hash=%s"
                " AND status='in_progress' AND lease_expires_at <= now()"
                " AND locked_by IS NOT DISTINCT FROM %s"
                " RETURNING locked_by",
                (
                    owner, self._cfg.lease_seconds,
                    key.session_id, key.node, key.attempt, key.input_hash,
                    prev_owner,
                ),
            )
            took = await cur.fetchone() is not None
            await conn.commit()
        return took

    async def _take_failed(self, key: LedgerKey, owner: str) -> bool:
        """CAS-retry a ``failed`` row: flip it back to ``in_progress`` under our
        ownership. The ``status='failed'`` guard ensures only one retrier wins."""
        async with self._db.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE llm_calls SET status='in_progress', locked_by=%s,"
                " lease_expires_at = now() + make_interval(secs => %s),"
                " response_jsonb=NULL, updated_at=now()"
                " WHERE session_id=%s AND node=%s AND attempt=%s AND input_hash=%s"
                " AND status='failed'"
                " RETURNING locked_by",
                (
                    owner, self._cfg.lease_seconds,
                    key.session_id, key.node, key.attempt, key.input_hash,
                ),
            )
            took = await cur.fetchone() is not None
            await conn.commit()
        return took

    async def _heartbeat(self, key: LedgerKey, owner: str) -> None:
        """Periodically extend our lease while the provider call is in flight, so a
        legitimately slow call is not taken over as if the owner had crashed.

        A transient DB/pool error on one beat must NOT kill heartbeating for the
        rest of the call (that would silently let the lease expire and trigger a
        needless takeover) — so non-cancel errors are logged and the loop continues.
        Stops contributing once we no longer own the row (taken over)."""
        while True:
            try:
                await asyncio.sleep(self._cfg.heartbeat_seconds)
                async with self._db.pool.connection() as conn:
                    await conn.execute(
                        "UPDATE llm_calls SET"
                        " lease_expires_at = now() + make_interval(secs => %s),"
                        " updated_at=now()"
                        " WHERE session_id=%s AND node=%s AND attempt=%s"
                        " AND input_hash=%s AND status='in_progress' AND locked_by=%s",
                        (
                            self._cfg.lease_seconds,
                            key.session_id, key.node, key.attempt, key.input_hash,
                            owner,
                        ),
                    )
                    await conn.commit()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 — one bad beat must not stop heartbeating
                log.warning("ledger.heartbeat_failed", node=key.node, exc_info=True)

    async def _produce_and_commit(self, key: LedgerKey, owner: str, produce) -> dict:
        """Run the provider call (heartbeating the lease) then commit the result.

        On a graceful failure the row is set ``failed`` so the key can be retried.
        A hard crash here leaves the row ``in_progress``; its lease then expires
        and a waiter takes over (the crash-recovery path)."""
        hb = asyncio.create_task(self._heartbeat(key, owner))
        try:
            response = await produce(key.idempotency_key)
        except Exception:
            await self._fail(key, owner)
            # If we were taken over mid-flight and the new owner already committed,
            # the call logically succeeded — return its result instead of surfacing
            # a spurious failure to a still-alive caller (mirror the commit path).
            row = await self._read(key)
            if row is not None and row["status"] == "done":
                return row["response_jsonb"]
            raise
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb

        committed = await self._commit(key, owner, response)
        if not committed:
            # We were taken over mid-flight: the takeover owner is authoritative.
            # Return the committed response so the caller still gets a value (the
            # provider idempotency key meant our call was de-duped, not re-charged).
            row = await self._read(key)
            if row is not None and row["status"] == "done":
                return row["response_jsonb"]
        return response

    async def _commit(self, key: LedgerKey, owner: str, response: dict) -> bool:
        async with self._db.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE llm_calls SET status='done', response_jsonb=%s,"
                " locked_by=NULL, updated_at=now()"
                " WHERE session_id=%s AND node=%s AND attempt=%s AND input_hash=%s"
                " AND locked_by=%s"
                " RETURNING status",
                (
                    Jsonb(response),
                    key.session_id, key.node, key.attempt, key.input_hash, owner,
                ),
            )
            committed = await cur.fetchone() is not None
            await conn.commit()
        return committed

    async def _fail(self, key: LedgerKey, owner: str) -> None:
        async with self._db.pool.connection() as conn:
            await conn.execute(
                "UPDATE llm_calls SET status='failed', locked_by=NULL, updated_at=now()"
                " WHERE session_id=%s AND node=%s AND attempt=%s AND input_hash=%s"
                " AND locked_by=%s",
                (key.session_id, key.node, key.attempt, key.input_hash, owner),
            )
            await conn.commit()
