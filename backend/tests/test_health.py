"""Contract tests for ``/healthz`` using fake datastores.

The route reads ``app.state.db`` / ``app.state.cache``; we inject fakes and
drive the ASGI app directly (no lifespan, no real Postgres/Redis), so the
liveness contract — healthy → 200, any dependency down → 503 — is asserted
deterministically.
"""

from __future__ import annotations

import httpx
import pytest

from app.main import create_app
from app.settings import Settings


class _FakeDependency:
    def __init__(self, *, healthy: bool) -> None:
        self._healthy = healthy

    async def healthcheck(self) -> bool:
        return self._healthy


def _app_with(*, db_healthy: bool, redis_healthy: bool):
    app = create_app(Settings(log_json=False, environment="test"))
    app.state.db = _FakeDependency(healthy=db_healthy)
    app.state.cache = _FakeDependency(healthy=redis_healthy)
    return app


async def _get_health(app) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/healthz")


@pytest.mark.asyncio
async def test_healthz_all_up_returns_200_healthy() -> None:
    resp = await _get_health(_app_with(db_healthy=True, redis_healthy=True))

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "healthy",
        "checks": {"postgres": True, "redis": True},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("db_healthy", "redis_healthy", "expected_checks"),
    [
        (False, True, {"postgres": False, "redis": True}),
        (True, False, {"postgres": True, "redis": False}),
        (False, False, {"postgres": False, "redis": False}),
    ],
)
async def test_healthz_dependency_down_returns_503(
    db_healthy: bool, redis_healthy: bool, expected_checks: dict[str, bool]
) -> None:
    resp = await _get_health(_app_with(db_healthy=db_healthy, redis_healthy=redis_healthy))

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["checks"] == expected_checks
