"""Live-progress fan-out over Redis pub/sub (slice 3).

The graph runs as a single background executor that **publishes** a
``ProgressEvent`` per super-step; ``GET /sessions/{id}/stream`` **subscribes** and
forwards them as SSE. Pub/sub is fire-and-forget (not durable), so the
reconnect-safety floor is the snapshot cached at ``session:{id}:latest`` (the last
event, kept with a TTL), falling back to the DB session row — never Redis alone.

The publisher does SET-latest **then** PUBLISH so a subscriber that just missed
the publish can still read the fresh snapshot on connect. Each event carries a
monotonic ``step`` so a subscriber de-dupes the snapshot against live events.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from redis.asyncio.client import PubSub

from app.domain.models import ProgressEvent
from app.infra.cache import Cache
from app.logging import get_logger

log = get_logger(__name__)


class ProgressBus:
    """Publish/subscribe + snapshot cache for a run's live progress."""

    #: How long the latest-snapshot survives for late/reconnecting subscribers.
    SNAPSHOT_TTL_SECONDS = 3600

    def __init__(self, cache: Cache) -> None:
        self._cache = cache

    @staticmethod
    def channel(session_id: str) -> str:
        return f"session:{session_id}:events"

    @staticmethod
    def _latest_key(session_id: str) -> str:
        return f"session:{session_id}:latest"

    async def publish(self, event: ProgressEvent) -> None:
        """Cache the snapshot, then fan it out to live subscribers."""
        payload = event.model_dump_json()
        # SET-then-PUBLISH: a subscriber connecting between the two still reads a
        # snapshot at least as fresh as the message it may have missed.
        await self._cache.client.set(
            self._latest_key(event.session_id),
            payload,
            ex=self.SNAPSHOT_TTL_SECONDS,
        )
        await self._cache.client.publish(self.channel(event.session_id), payload)

    async def latest(self, session_id: str) -> ProgressEvent | None:
        """The most recent snapshot for reconnect, or ``None`` if none cached."""
        raw = await self._cache.client.get(self._latest_key(session_id))
        if raw is None:
            return None
        return ProgressEvent.model_validate_json(raw)

    @asynccontextmanager
    async def subscribe(self, session_id: str) -> AsyncIterator[PubSub]:
        """Subscribe to a session's channel for the duration of the context.

        ``ignore_subscribe_messages=True`` drops the subscribe confirmation so
        ``get_message`` only yields application events (or ``None`` on timeout,
        which the SSE layer turns into a keepalive).
        """
        pubsub = self._cache.client.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(self.channel(session_id))
        try:
            yield pubsub
        finally:
            try:
                await pubsub.unsubscribe(self.channel(session_id))
                await pubsub.aclose()
            except Exception:  # noqa: BLE001 — teardown must not mask the response
                log.warning("progress_bus.unsubscribe_failed", session_id=session_id)
