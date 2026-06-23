"""Error taxonomy → HTTP mapping (PRD §9).

The request-path mapping registered here complements two contracts handled
elsewhere: **validation → 422** (pydantic request models) and **degraded → 200 +
``degraded:true``** (a degraded run is a *successful* read — the partial report is
returned with the flag). Transient infrastructure failures during a request map to
**503 + Retry-After**; upstream provider failures to **502**; a blocked SSRF url to
**400**. (Transient *provider* errors inside a background run are absorbed by the
node RetryPolicy + degraded path, not surfaced as a 5xx to the client.)
"""

from __future__ import annotations

import psycopg
import redis.exceptions
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.logging import get_logger
from app.providers.errors import ProviderError
from app.providers.fetch import SsrfError

log = get_logger(__name__)

RETRY_AFTER_SECONDS = 5


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(psycopg.OperationalError)
    async def _db_unavailable(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
        log.warning("api.transient_db_error", path=request.url.path, exc_info=True)
        return _transient("datastore temporarily unavailable")

    @app.exception_handler(redis.exceptions.ConnectionError)
    async def _redis_unavailable(
        request: Request, exc: redis.exceptions.ConnectionError
    ) -> JSONResponse:
        log.warning("api.transient_redis_error", path=request.url.path, exc_info=True)
        return _transient("event bus temporarily unavailable")

    @app.exception_handler(ProviderError)
    async def _provider_error(request: Request, exc: ProviderError) -> JSONResponse:
        # ModelVerificationError (a subclass) lands here too.
        log.warning("api.provider_error", path=request.url.path, exc_info=True)
        return JSONResponse(status_code=502, content={"detail": str(exc) or "upstream provider error"})

    @app.exception_handler(SsrfError)
    async def _ssrf_error(request: Request, exc: SsrfError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": "blocked unsafe url"})


def _transient(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": detail, "transient": True},
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )
