"""Provider seams — the swap points between deterministic mocks (slices 2–6) and
real OpenAI / Tavily backends (slice 7).

``LLMClient.parse`` mirrors the OpenAI Responses ``parse`` surface: a task picks
the instruction template, ``context`` is the typed input, and ``output_type`` is
the Pydantic schema the response is parsed into — so the real adapter is a thin
serialization layer and the mock is a deterministic dispatcher.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMTask(str, Enum):
    PLAN = "plan"
    EXTRACT = "extract"
    ANALYZE = "analyze"
    QUALITY = "quality"


@runtime_checkable
class LLMClient(Protocol):
    async def parse(
        self,
        *,
        task: LLMTask,
        context: BaseModel,
        output_type: type[T],
        model: str | None = None,
    ) -> T: ...


class SearchHit(BaseModel):
    url: str
    title: str
    snippet: str
    content: str | None = None


@runtime_checkable
class SearchProvider(Protocol):
    async def search(self, query: str, *, max_results: int = 5) -> list[SearchHit]: ...
