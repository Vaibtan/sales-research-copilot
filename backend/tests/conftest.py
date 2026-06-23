"""Shared test fixtures.

DB-backed tests require a running Postgres + Redis (``docker compose up -d
postgres redis``). ``DATABASE_URL`` / ``REDIS_URL`` are read from the env, falling
back to the local compose defaults.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
import pytest_asyncio

# psycopg3's async pool requires a SelectorEventLoop; Windows defaults to the
# ProactorEventLoop. Set the policy before pytest-asyncio creates any loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.db.migrator import run_migrations
from app.graph.checkpointer import build_checkpointer
from app.infra.database import Database
from app.settings import Settings

os.environ.setdefault(
    "DATABASE_URL", "postgresql://copilot:copilot@localhost:5432/copilot"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

# Hermetic provider selection: force the *mock* backends regardless of any
# OPENAI_API_KEY / TAVILY_API_KEY in the developer's shell, so the suite never
# makes a live provider call. (Empty string is falsy and overrides .env.)
os.environ["OPENAI_API_KEY"] = ""
os.environ["TAVILY_API_KEY"] = ""

# Default the general suite to the straight-through (auto-approve) flow; the HITL
# plan-approval path is exercised explicitly in test_hitl.py with the flag on.
os.environ["HITL_PLAN_APPROVAL"] = "false"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(log_json=False, environment="test")


@pytest_asyncio.fixture
async def db(settings: Settings):
    database = Database(settings.database_url)
    await database.open()
    await run_migrations(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def checkpointer(settings: Settings):
    cp = await build_checkpointer(settings.database_url)
    yield cp
    await cp.close()


@pytest_asyncio.fixture(autouse=True)
async def _reset_cross_session_memory(settings: Settings):
    """Isolate the one **cross-session** table. ``research_memory`` is keyed by
    ``company_key`` (not session) and the dev DB is long-lived, so a prior test's
    warm-start memory would otherwise pollute a cold-start expectation. Per-session
    tables are keyed by a random ``session_id`` and don't collide, so they need no
    reset. Best-effort: skipped if Postgres is down (keeps pure-unit tests
    runnable without it)."""
    import psycopg

    try:
        conn = await psycopg.AsyncConnection.connect(
            settings.database_url, autocommit=True
        )
    except Exception:
        yield
        return
    try:
        await conn.execute("TRUNCATE research_memory")
    except Exception:
        pass  # table not migrated yet — the first DB test will create it
    finally:
        await conn.close()
    yield
