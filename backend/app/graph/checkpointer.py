"""LangGraph durable checkpointer (Layer 1 — run state / resume).

The checkpointer gets its *own* psycopg pool configured ``autocommit=True`` +
``dict_row`` (what ``AsyncPostgresSaver`` requires), kept separate from the
relational pool, which needs explicit transactions for ``persist``. Same driver
(psycopg3) across both, so the durability story stays coherent.
"""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Checkpointer:
    saver: AsyncPostgresSaver
    pool: AsyncConnectionPool

    async def close(self) -> None:
        await self.pool.close()
        log.info("checkpointer.pool_closed")


async def build_checkpointer(
    dsn: str, *, min_size: int = 1, max_size: int = 5
) -> Checkpointer:
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        open=False,
        name="copilot-checkpointer",
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    saver = AsyncPostgresSaver(pool)
    await saver.setup()  # idempotent — creates/migrates the checkpoint tables
    log.info("checkpointer.ready")
    return Checkpointer(saver=saver, pool=pool)
