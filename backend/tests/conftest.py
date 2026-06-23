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
