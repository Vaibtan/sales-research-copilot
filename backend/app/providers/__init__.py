"""Provider seams + a factory that selects mock vs. real backends.

Slice 2 always returns the deterministic mocks; slice 7 swaps in the OpenAI
Responses client and Tavily-backed search when API keys are configured.
"""

from __future__ import annotations

from app.providers.base import LLMClient, LLMTask, SearchHit, SearchProvider
from app.providers.mock import MockLLMClient, MockSearchProvider
from app.settings import Settings

__all__ = [
    "LLMClient",
    "LLMTask",
    "SearchHit",
    "SearchProvider",
    "MockLLMClient",
    "MockSearchProvider",
    "build_providers",
]


def build_providers(settings: Settings) -> tuple[LLMClient, SearchProvider]:
    return MockLLMClient(), MockSearchProvider()
