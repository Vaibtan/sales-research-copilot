"""Provider seams — the swap points between deterministic mocks (slices 2–6) and
real OpenAI / Tavily backends (slice 7).

``LLMClient.parse`` mirrors the OpenAI Responses ``parse`` surface: a task picks
the instruction template, ``context`` is the typed input, and ``output_type`` is
the Pydantic schema the response is parsed into — so the real adapter is a thin
serialization layer and the mock is a deterministic dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMTask(str, Enum):
    PLAN = "plan"
    EXTRACT = "extract"
    ANALYZE = "analyze"
    QUALITY = "quality"
    CHAT = "chat"


@dataclass(frozen=True, slots=True)
class CallSite:
    """Identifies *which* graph step is making a provider call, so the durable
    ledger (slice 6, §6.5) can key the reservation on ``(session_id, node,
    attempt, input_hash)``. Nodes pass this on every call; a bare provider
    ignores it, the :class:`~app.providers.ledger.LedgerLLMClient` uses it.
    """

    session_id: str
    node: str
    attempt: int = 1


@runtime_checkable
class LLMClient(Protocol):
    async def parse(
        self,
        *,
        task: LLMTask,
        context: BaseModel,
        output_type: type[T],
        model: str | None = None,
        call: CallSite | None = None,
        idempotency_key: str | None = None,
    ) -> T:
        """Parse a structured response for ``task``.

        ``call`` carries the ledger call-site (consumed by the ledger wrapper);
        ``idempotency_key`` is the provider-level dedup key (consumed by the real
        OpenAI client). A bare/mock client ignores both.
        """
        ...


class SearchHit(BaseModel):
    url: str
    title: str
    snippet: str
    content: str | None = None


@runtime_checkable
class SearchProvider(Protocol):
    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        call: CallSite | None = None,
    ) -> list[SearchHit]:
        """Search for ``query``. ``call`` carries the ledger call-site (consumed by
        :class:`~app.providers.ledger.LedgerSearchProvider` so a resume re-pays no
        external search); a bare/mock provider ignores it."""
        ...
