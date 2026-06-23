"""Slice 7 — real provider adapters, exercised without the network.

The OpenAI/Tavily *logic* (task→model routing, idempotency header, output parsing,
result mapping) is tested by injecting fake SDK-shaped clients. The SSRF guard is
tested against IP literals + localhost (no external DNS). The mock path stays the
default; live calls are never made here.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.providers import build_providers
from app.providers.base import CallSite, LLMTask
from app.providers.embeddings import EMBEDDING_DIM, MockEmbeddingClient, OpenAIEmbeddingClient
from app.providers.errors import ModelVerificationError, ProviderError, is_transient
from app.providers.fetch import SsrfError, assert_public_url
from app.providers.mock import MockLLMClient, MockSearchProvider
from app.providers.openai_llm import LLMRouting, OpenAILLMClient, verify_models
from app.providers.tavily_search import TavilySearchProvider
from app.settings import Settings
from pydantic import BaseModel


class _Out(BaseModel):
    value: int


# --- SSRF guard --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "http://127.0.0.1:8000/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://100.64.0.1/cgnat",  # RFC 6598 shared/CGNAT space
        "ftp://example.com/file",  # non-http scheme
        "file:///etc/passwd",
        "http:///nohost",
    ],
)
def test_ssrf_guard_blocks_unsafe_urls(url: str) -> None:
    with pytest.raises(SsrfError):
        assert_public_url(url)


async def test_fetch_text_blocks_redirect_to_internal_address() -> None:
    """SSRF guard must re-validate every redirect hop — a public url that 302s to a
    private/metadata address is blocked (auto-redirects would bypass the guard)."""
    from app.providers.fetch import fetch_text

    class _Resp:
        def __init__(self, redirect_to=None):
            self.is_redirect = redirect_to is not None
            self.headers = {"location": redirect_to} if redirect_to else {}
            self.url = httpx.URL("https://8.8.8.8/start")
            self.encoding = "utf-8"

        def raise_for_status(self):
            pass

        async def aiter_bytes(self):
            yield b"ok"

    class _Stream:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self):
            self.urls = []

        def stream(self, method, url):
            self.urls.append(url)
            # First hop 302s to the cloud-metadata address.
            return _Stream(_Resp(redirect_to="http://169.254.169.254/latest/meta-data/"))

        async def aclose(self):
            pass

    client = _Client()
    with pytest.raises(SsrfError):
        await fetch_text("https://8.8.8.8/start", client=client)
    # The internal redirect target was never actually fetched (blocked before connect).
    assert all("169.254" not in str(u) for u in client.urls)


def test_ssrf_guard_allows_public_ip_literal() -> None:
    assert_public_url("https://8.8.8.8/")  # public — no exception


def test_ssrf_guard_escape_hatch_allows_private() -> None:
    assert_public_url("http://10.0.0.5/", allow_private=True)  # explicit opt-in


# --- OpenAI LLM client (injected fake) ---------------------------------------


class _FakeResponses:
    def __init__(self, parsed) -> None:
        self.parsed = parsed
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed=self.parsed,
            usage=SimpleNamespace(input_tokens=12, output_tokens=7),
        )


class _FakeOpenAI:
    def __init__(self, parsed=None) -> None:
        self.responses = _FakeResponses(parsed)


def _routing() -> LLMRouting:
    return LLMRouting.from_settings(Settings(model_quality="gpt-q-test"))


async def test_openai_parse_routes_model_and_passes_idempotency_header() -> None:
    fake = _FakeOpenAI(parsed=_Out(value=1))
    client = OpenAILLMClient(fake, _routing())

    out = await client.parse(
        task=LLMTask.QUALITY,
        context=_Out(value=99),
        output_type=_Out,
        call=CallSite("s1", "quality_gate", 1),
        idempotency_key="idem-123",
    )

    assert out == _Out(value=1)
    sent = fake.responses.calls[0]
    assert sent["model"] == "gpt-q-test"  # routed by task
    assert sent["text_format"] is _Out
    assert sent["extra_headers"] == {"Idempotency-Key": "idem-123"}


async def test_openai_parse_raises_when_no_output() -> None:
    fake = _FakeOpenAI(parsed=None)  # refusal / unparseable
    client = OpenAILLMClient(fake, _routing())
    with pytest.raises(ProviderError):
        await client.parse(task=LLMTask.PLAN, context=_Out(value=1), output_type=_Out)


# --- model verification ------------------------------------------------------


class _FakeModels:
    def __init__(self, known: set[str]) -> None:
        self.known = known
        self.retrieved: list[str] = []

    async def retrieve(self, model_id: str):
        import openai

        self.retrieved.append(model_id)
        if model_id not in self.known:
            raise openai.NotFoundError(
                "missing",
                response=httpx.Response(404, request=httpx.Request("GET", "http://x")),
                body=None,
            )
        return SimpleNamespace(id=model_id)


async def test_verify_models_passes_when_all_known() -> None:
    fake = SimpleNamespace(models=_FakeModels({"a", "b"}))
    await verify_models(fake, {"a", "b"})
    assert set(fake.models.retrieved) == {"a", "b"}


async def test_verify_models_fails_fast_on_unknown_id() -> None:
    fake = SimpleNamespace(models=_FakeModels({"a"}))
    with pytest.raises(ModelVerificationError):
        await verify_models(fake, {"a", "ghost-model"})


# --- embeddings --------------------------------------------------------------


async def test_mock_embeddings_are_deterministic_unit_vectors() -> None:
    emb = MockEmbeddingClient()
    [v1] = await emb.embed(["hello"])
    [v2] = await emb.embed(["hello"])
    assert v1 == v2  # deterministic
    assert len(v1) == EMBEDDING_DIM
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-6  # unit norm


async def test_openai_embeddings_map_data_to_vectors() -> None:
    class _FakeEmbeddings:
        async def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2]) for _ in kwargs["input"]]
            )

    fake = SimpleNamespace(embeddings=_FakeEmbeddings())
    client = OpenAIEmbeddingClient(fake, "text-embedding-3-small")
    out = await client.embed(["a", "b"], idempotency_key="k")
    assert out == [[0.1, 0.2], [0.1, 0.2]]
    assert fake.embeddings.kwargs["extra_headers"] == {"Idempotency-Key": "k"}


# --- Tavily search (injected fake) -------------------------------------------


async def test_tavily_maps_results_to_hits() -> None:
    class _FakeTavily:
        async def search(self, query, **kwargs):
            return {
                "results": [
                    {
                        "url": "https://acme.com/about",
                        "title": "About Acme",
                        "content": "snippet",
                        "raw_content": "full page",
                    },
                    {"url": "", "title": "no url"},  # skipped
                ]
            }

    provider = TavilySearchProvider(_FakeTavily(), enrich_with_fetch=False)
    hits = await provider.search("acme overview", max_results=3)
    assert len(hits) == 1
    assert hits[0].url == "https://acme.com/about"
    assert hits[0].content == "full page"


async def test_tavily_falls_back_to_snippet_without_raw_content() -> None:
    class _FakeTavily:
        async def search(self, query, **kwargs):
            return {"results": [{"url": "https://x.com", "title": "X", "content": "snip"}]}

    provider = TavilySearchProvider(_FakeTavily(), enrich_with_fetch=False)
    hits = await provider.search("q")
    assert hits[0].content == "snip"  # no raw_content, enrich off → snippet


# --- transient classification + factory --------------------------------------


def test_is_transient_classification() -> None:
    import openai

    assert is_transient(httpx.ConnectError("boom"))
    assert is_transient(httpx.ReadTimeout("slow"))
    assert is_transient(
        openai.APIStatusError(
            "5xx",
            response=httpx.Response(503, request=httpx.Request("GET", "http://x")),
            body=None,
        )
    )
    assert not is_transient(
        openai.APIStatusError(
            "4xx",
            response=httpx.Response(400, request=httpx.Request("GET", "http://x")),
            body=None,
        )
    )
    assert not is_transient(ValueError("permanent"))


def test_build_providers_defaults_to_mock_without_keys() -> None:
    providers = build_providers(Settings())
    assert isinstance(providers.llm, MockLLMClient)
    assert isinstance(providers.search, MockSearchProvider)
    assert providers.model_meta == {"providers": "mock"}
    assert providers.verify is None


def test_build_providers_uses_real_when_both_keys_present() -> None:
    # Construction only — no network (verify is not invoked here).
    providers = build_providers(
        Settings(openai_api_key="sk-test", tavily_api_key="tvly-test123")
    )
    assert isinstance(providers.llm, OpenAILLMClient)
    assert isinstance(providers.search, TavilySearchProvider)
    assert providers.model_meta["providers"] == "openai+tavily"
    assert providers.verify is not None


def test_build_providers_falls_back_to_mock_on_partial_keys() -> None:
    providers = build_providers(Settings(openai_api_key="sk-test"))  # no tavily
    assert isinstance(providers.llm, MockLLMClient)
