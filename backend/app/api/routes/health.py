"""Liveness/readiness endpoint with *live* dependency checks.

``/healthz`` reflects the real state of Postgres and Redis (not a static 200):
both probes run concurrently and any failure flips the response to 503 so an
orchestrator can gate traffic on actual readiness.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class DependencyChecks(BaseModel):
    postgres: bool
    redis: bool


class HealthResponse(BaseModel):
    status: Literal["healthy", "unhealthy"]
    checks: DependencyChecks


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness/readiness with live db + redis checks",
)
async def healthz(request: Request, response: Response) -> HealthResponse:
    db = request.app.state.db
    cache = request.app.state.cache

    postgres_ok, redis_ok = await asyncio.gather(
        db.healthcheck(),
        cache.healthcheck(),
    )

    healthy = postgres_ok and redis_ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="healthy" if healthy else "unhealthy",
        checks=DependencyChecks(postgres=postgres_ok, redis=redis_ok),
    )
