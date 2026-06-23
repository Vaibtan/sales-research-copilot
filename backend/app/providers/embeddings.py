"""Embedding provider seam (PRD §0 — ``text-embedding-3-small``, vector(1536)).

Wired and id-verified at boot in slice 7; first *exercised* by the pgvector
chunk/embed persist in slice 9 and chat retrieval in slice 10. The mock is
deterministic (hash-seeded) so vector tables can be tested without the network.
"""

from __future__ import annotations

import hashlib
import math
import re
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
    """Deterministic **bag-of-words** unit vectors (the hashing trick): each word
    maps to a dimension, so texts that share words get high cosine similarity. This
    makes retrieval *semantically meaningful* in tests (a "products" question
    surfaces the products chunk) while staying fully deterministic and offline."""

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
        vec = [0.0] * self._dim
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            idx = int(hashlib.sha256(word.encode()).hexdigest(), 16) % self._dim
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:  # blank text — return a fixed unit vector, never zero-norm
            vec[0] = 1.0
            return vec
        return [x / norm for x in vec]
