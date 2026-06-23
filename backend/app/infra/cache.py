"""Redis access via an async client.

Redis backs SSE fan-out / pub-sub and (optionally) the durable LLM-call ledger
in later slices. Here it only needs a connection pool and a liveness probe.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.logging import get_logger

log = get_logger(__name__)


class Cache:
    """Owns an async Redis client (which manages its own connection pool)."""

    def __init__(self, url: str) -> None:
        self._client: Redis = Redis.from_url(url, decode_responses=True)

    async def close(self) -> None:
        await self._client.aclose()
        log.info("cache.closed")

    async def healthcheck(self) -> bool:
        """Return True iff Redis answers PING right now."""
        try:
            return bool(await self._client.ping())
        except Exception:  # noqa: BLE001 — health probe must never raise
            log.warning("cache.healthcheck_failed", exc_info=True)
            return False

    @property
    def client(self) -> Redis:
        return self._client
