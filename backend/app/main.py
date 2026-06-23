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
from app.api.errors import register_exception_handlers
from app.api.routes import health, sessions
from app.db.ledger import CallLedger, LedgerConfig
from app.db.migrator import run_migrations
from app.graph import GraphRunner, build_chat_graph, build_checkpointer, build_graph
from app.infra import Cache, Database, ProgressBus
from app.logging import configure_logging, get_logger
from app.providers import build_providers
from app.services import ChatService, SessionService
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

    # Memory revalidator: only the real fetch path can re-validate a stale memory
    # url by content_hash (§7.1). With mocks it stays None, so stale volatile facts
    # are simply re-derived by gather.
    memory_revalidator = None
    if providers.model_meta.get("providers") != "mock":
        from app.domain.identity import content_hash
        from app.providers.fetch import fetch_text

        async def memory_revalidator(url: str) -> str | None:  # noqa: F811
            try:
                body = await fetch_text(
                    url,
                    timeout=settings.fetch_timeout_seconds,
                    max_bytes=settings.fetch_max_bytes,
                    allow_private=settings.allow_private_fetch,
                )
                return content_hash(body)
            except Exception:  # noqa: BLE001 — revalidation is best-effort
                return None
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
        embeddings=providers.embeddings,
        memory_ttl_durable_seconds=settings.memory_ttl_durable_seconds,
        memory_ttl_volatile_seconds=settings.memory_ttl_volatile_seconds,
        memory_match_max_distance=settings.memory_match_max_distance,
        memory_revalidator=memory_revalidator,
        hitl_plan_approval=settings.hitl_plan_approval,
    )
    progress_bus = ProgressBus(cache)
    runner = GraphRunner(graph, db, progress_bus)
    # Chat uses the raw providers (it is not part of the resumable research run, so
    # not ledgered): retrieve → answer over the persisted pgvector chunks.
    chat_graph = build_chat_graph(
        llm=providers.llm, embeddings=providers.embeddings, db=db
    )

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
    app.state.chat_service = ChatService(db, chat_graph)

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

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(sessions.router)
    return app


app = create_app()
