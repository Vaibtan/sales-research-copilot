"""FastAPI application factory and lifespan.

Owns process wiring: configure logging, build datastore adapters + the durable
checkpointer, run schema migrations, assemble the research graph (mock providers
until slice 7) behind a background runner, and expose it all on ``app.state``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import health, sessions
from app.db.ledger import CallLedger, LedgerConfig
from app.db.migrator import run_migrations
from app.graph import GraphRunner, build_checkpointer, build_graph
from app.infra import Cache, Database, ProgressBus
from app.logging import configure_logging, get_logger
from app.providers import build_providers
from app.services import SessionService
from app.settings import Settings, get_settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    db = Database(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    cache = Cache(settings.redis_url)
    await db.open()
    await run_migrations(db)

    checkpointer = await build_checkpointer(settings.database_url)
    providers = build_providers(settings)
    if providers.verify is not None:
        # Fail fast on an unknown/unavailable model id before serving (§16).
        await providers.verify()
    ledger = CallLedger(
        db,
        LedgerConfig(
            lease_seconds=settings.ledger_lease_seconds,
            heartbeat_seconds=settings.ledger_heartbeat_seconds,
            poll_seconds=settings.ledger_poll_seconds,
            wait_timeout_seconds=settings.ledger_wait_timeout_seconds,
        ),
    )
    graph = build_graph(
        llm=providers.llm,
        search=providers.search,
        db=db,
        checkpointer=checkpointer.saver,
        max_extra_passes=settings.max_extra_passes,
        quality_threshold=settings.quality_threshold,
        ledger=ledger,
        model_meta=providers.model_meta,
        retry_max_attempts=settings.retry_max_attempts,
    )
    progress_bus = ProgressBus(cache)
    runner = GraphRunner(graph, db, progress_bus)

    app.state.db = db
    app.state.cache = cache
    app.state.checkpointer = checkpointer
    app.state.progress_bus = progress_bus
    app.state.runner = runner
    app.state.providers = providers
    app.state.embeddings = providers.embeddings
    app.state.ledger = ledger
    app.state.session_service = SessionService(
        db, runner, max_extra_passes=settings.max_extra_passes
    )

    log.info("app.startup", app=settings.app_name, environment=settings.environment)
    try:
        yield
    finally:
        await runner.shutdown()
        await checkpointer.close()
        await cache.close()
        await db.close()
        log.info("app.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(json_logs=settings.log_json, level=settings.log_level)

    app = FastAPI(
        title="AI Sales Research Copilot",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(sessions.router)
    return app


app = create_app()
