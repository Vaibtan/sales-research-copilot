"""Provider seams + a factory that selects mock vs. real backends.

The deterministic mocks are the default and the only thing the test suite exercises
(no secrets, no network). When **both** ``OPENAI_API_KEY`` and ``TAVILY_API_KEY``
are configured, ``build_providers`` swaps in the OpenAI Responses client, the
Tavily-backed search, and the OpenAI embedding client behind the same interfaces
(slice 7). The real SDKs are imported lazily so the mock path never loads them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.providers.base import CallSite, LLMClient, LLMTask, SearchHit, SearchProvider
from app.providers.embeddings import EmbeddingClient, MockEmbeddingClient
from app.providers.errors import ModelVerificationError, ProviderError, is_transient
from app.providers.mock import MockLLMClient, MockSearchProvider
from app.settings import Settings
from app.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "LLMClient",
    "LLMTask",
    "CallSite",
    "SearchHit",
    "SearchProvider",
    "EmbeddingClient",
    "MockLLMClient",
    "MockSearchProvider",
    "MockEmbeddingClient",
    "ProviderError",
    "ModelVerificationError",
    "is_transient",
    "Providers",
    "build_providers",
]


@dataclass(slots=True)
class Providers:
    llm: LLMClient
    search: SearchProvider
    embeddings: EmbeddingClient
    model_meta: dict
    #: Boot-time model-id verification (real backend only; ``None`` for mocks).
    verify: Callable[[], Awaitable[None]] | None = None


def build_providers(settings: Settings) -> Providers:
    has_openai = bool(settings.openai_api_key)
    has_tavily = bool(settings.tavily_api_key)

    if has_openai and has_tavily:
        return _real_providers(settings)

    if has_openai or has_tavily:
        log.warning(
            "providers.partial_keys_using_mock",
            openai=has_openai,
            tavily=has_tavily,
            detail="both OPENAI_API_KEY and TAVILY_API_KEY are required for live mode",
        )
    log.info("providers.mock")
    return Providers(
        llm=MockLLMClient(),
        search=MockSearchProvider(),
        embeddings=MockEmbeddingClient(),
        model_meta={"providers": "mock"},
    )


def _real_providers(settings: Settings) -> Providers:
    # Lazy imports: the mock path must not require the SDKs to be importable.
    from functools import partial

    from openai import AsyncOpenAI
    from tavily import AsyncTavilyClient

    from app.providers.embeddings import OpenAIEmbeddingClient
    from app.providers.openai_llm import LLMRouting, OpenAILLMClient, verify_models
    from app.providers.tavily_search import TavilySearchProvider

    openai_client = AsyncOpenAI(
        api_key=settings.openai_api_key, base_url=settings.openai_base_url
    )
    routing = LLMRouting.from_settings(settings)
    llm = OpenAILLMClient(openai_client, routing)
    embeddings = OpenAIEmbeddingClient(openai_client, settings.model_embedding)
    search = TavilySearchProvider(
        AsyncTavilyClient(api_key=settings.tavily_api_key),
        search_depth=settings.search_depth,
        enrich_with_fetch=settings.fetch_enrich,
        fetch_timeout=settings.fetch_timeout_seconds,
        fetch_max_bytes=settings.fetch_max_bytes,
        allow_private_fetch=settings.allow_private_fetch,
    )

    model_ids = routing.model_ids() | {settings.model_embedding}
    verify = (
        partial(verify_models, openai_client, model_ids)
        if settings.verify_models_on_boot
        else None
    )
    log.info("providers.openai_tavily", models=sorted(model_ids))
    return Providers(
        llm=llm,
        search=search,
        embeddings=embeddings,
        model_meta={"providers": "openai+tavily", "models": sorted(model_ids)},
        verify=verify,
    )
