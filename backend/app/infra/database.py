"""Postgres access via a psycopg3 async connection pool.

We standardise on psycopg3 (not SQLAlchemy/asyncpg) because LangGraph's
``AsyncPostgresSaver`` checkpointer uses the same driver — one connection stack
across the app keeps the durability story coherent. This thin wrapper owns the
pool lifecycle and exposes a cheap liveness probe for ``/healthz``.
"""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from app.logging import get_logger

log = get_logger(__name__)


class Database:
    """Owns a lazily-opened async connection pool."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        # open=False defers connecting until an event loop is running (and is the
        # forward-compatible default in psycopg-pool 3.2+).
        self._pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            name="copilot-pg",
        )

    async def open(self) -> None:
        """Open the pool; connections fill in the background (non-blocking)."""
        await self._pool.open(wait=False)
        log.info("database.pool_opened", min_size=self._pool.min_size, max_size=self._pool.max_size)

    async def close(self) -> None:
        await self._pool.close()
        log.info("database.pool_closed")

    async def healthcheck(self) -> bool:
        """Return True iff a connection can run ``SELECT 1`` right now."""
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute("SELECT 1")
                row = await cur.fetchone()
                return row == (1,)
        except Exception:  # noqa: BLE001 — health probe must never raise
            log.warning("database.healthcheck_failed", exc_info=True)
            return False

    @property
    def pool(self) -> AsyncConnectionPool:
        return self._pool
