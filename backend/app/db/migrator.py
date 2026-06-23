"""A tiny, dependency-free forward-only migration runner.

Ordered ``NNNN_name.sql`` files in ``migrations/`` are applied once each, inside
a transaction, and recorded in ``schema_migrations``. No ORM, no Alembic — full
control over the DDL, which suits the deterministic-id / unique-constraint design
this project leans on. (LangGraph's own checkpoint tables are created separately
by ``AsyncPostgresSaver.setup()``.)
"""

from __future__ import annotations

from pathlib import Path

from app.infra.database import Database
from app.logging import get_logger

log = get_logger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def run_migrations(db: Database) -> None:
    migrations = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    async with db.pool.connection() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        await conn.commit()

        cur = await conn.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in await cur.fetchall()}

        for path in migrations:
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                )
            log.info("db.migration_applied", version=version)

    pending = len(migrations) - len(applied)
    log.info("db.migrations_complete", total=len(migrations), newly_applied=max(pending, 0))
