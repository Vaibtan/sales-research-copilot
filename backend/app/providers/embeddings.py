"""Embedding provider seam (PRD §0 — ``text-embedding-3-small``, vector(1536)).

Wired and id-verified at boot in slice 7; first *exercised* by the pgvector
chunk/embed persist in slice 9 and chat retrieval in slice 10. The mock is
deterministic (hash-seeded) so vector tables can be tested without the network.
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.providers.base import CallSite
from app.providers.errors import ProviderError

if TYPE_CHECKING:
    from openai import AsyncOpenAI

EMBEDDING_DIM = 1536


@runtime_checkable
class EmbeddingClient(Protocol):
    async def embed(
        self,
        texts: list[str],
        *,
        call: CallSite | None = None,
        idempotency_key: str | None = None,
    ) -> list[list[float]]:
        """Return one ``EMBEDDING_DIM``-vector per input text (order preserved)."""
        ...


class OpenAIEmbeddingClient:
    def __init__(self, client: "AsyncOpenAI", model: str) -> None:
        self._client = client
        self._model = model

    async def embed(
        self,
        texts: list[str],
        *,
        call: CallSite | None = None,
        idempotency_key: str | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        extra_headers = (
            {"Idempotency-Key": idempotency_key} if idempotency_key else None
        )
        resp = await self._client.embeddings.create(
            model=self._model, input=texts, extra_headers=extra_headers
        )
        vectors = [item.embedding for item in resp.data]
        if len(vectors) != len(texts):
            raise ProviderError("embedding count did not match input count")
        return vectors


class MockEmbeddingClient:
    """Deterministic unit-norm vectors seeded by the text — stable across runs so
    cosine similarity in tests is reproducible."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    async def embed(
        self,
        texts: list[str],
        *,
        call: CallSite | None = None,
        idempotency_key: str | None = None,
    ) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        # Expand a digest into `dim` floats, then L2-normalize.
        raw: list[float] = []
        counter = 0
        while len(raw) < self._dim:
            digest = hashlib.sha256(f"{text}\x1f{counter}".encode()).digest()
            raw.extend(b / 255.0 - 0.5 for b in digest)
            counter += 1
        vec = raw[: self._dim]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]
