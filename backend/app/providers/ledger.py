"""Ledger-backed LLM client wrapper (slice 6, PRD §6.5).

Wraps an inner :class:`~app.providers.base.LLMClient` so every ``parse`` is routed
through the durable :class:`~app.db.ledger.CallLedger`: a completed call is
returned from the ledger without touching the provider, and the actual provider
call is sent with the ledger's idempotency key. Nodes are unaware of any of this —
they just pass a ``call=CallSite(...)`` and the wrapper does the rest. Without a
``call`` (e.g. a unit test using a provider directly) it is a transparent
pass-through.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from app.db.ledger import CallLedger, LedgerKey
from app.domain.identity import content_hash
from app.observability import span
from app.providers.base import CallSite, LLMClient, LLMTask, SearchHit, SearchProvider, T
from app.providers.embeddings import EmbeddingClient

# Bump this to invalidate every cached ledger row on an incompatible change that the
# hash below can't see on its own — e.g. a reworded task instruction template or a
# changed generation setting (token caps) in the OpenAI client. The output *schema*
# is fingerprinted automatically, so only prompt/generation changes need a bump.
_LEDGER_INPUT_VERSION = "1"


def _input_hash(
    task: LLMTask, context: BaseModel, output_type: type[BaseModel], model: str | None
) -> str:
    """Canonical fingerprint of everything that determines the response — so the
    same logical call re-derives the same ledger key, and any change (model, prompt
    inputs, output schema) is a distinct call.

    The output is keyed on the full JSON **schema** (not just the class name): a
    changed field set behind the same name is a distinct call, so a resume after a
    deploy never replays output shaped for the old schema. ``_LEDGER_INPUT_VERSION``
    covers prompt/generation changes the schema can't reflect (Codex finding)."""
    return content_hash(
        "\x1f".join(
            (
                _LEDGER_INPUT_VERSION,
                task.value,
                model or "",
                json.dumps(output_type.model_json_schema(), sort_keys=True),
                context.model_dump_json(),
            )
        )
    )


class LedgerLLMClient:
    """An :class:`LLMClient` that de-duplicates calls through the durable ledger."""

    def __init__(self, inner: LLMClient, ledger: CallLedger) -> None:
        self._inner = inner
        self._ledger = ledger

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
        if call is None:
            # No call-site → can't key the ledger; pass straight through.
            return await self._inner.parse(
                task=task, context=context, output_type=output_type, model=model
            )

        key = LedgerKey(
            session_id=call.session_id,
            node=call.node,
            attempt=call.attempt,
            input_hash=_input_hash(task, context, output_type, model),
        )

        async def produce(idem: str) -> dict:
            result = await self._inner.parse(
                task=task,
                context=context,
                output_type=output_type,
                model=model,
                idempotency_key=idem,
            )
            return result.model_dump(mode="json")

        with span(
            f"llm.{task.value}",
            node=call.node,
            session_id=call.session_id,
            attempt=call.attempt,
            model=model,
        ):
            cached = await self._ledger.run(key, produce)
        return output_type.model_validate(cached)


class LedgerEmbeddingClient:
    """An :class:`EmbeddingClient` that de-duplicates embedding calls through the
    durable ledger (slice 9) — so a resume re-embeds nothing already committed."""

    def __init__(self, inner: EmbeddingClient, ledger: CallLedger) -> None:
        self._inner = inner
        self._ledger = ledger

    async def embed(
        self,
        texts: list[str],
        *,
        call: CallSite | None = None,
        idempotency_key: str | None = None,
    ) -> list[list[float]]:
        if call is None or not texts:
            return await self._inner.embed(texts)

        key = LedgerKey(
            session_id=call.session_id,
            node=call.node,
            attempt=call.attempt,
            input_hash=content_hash("\x1f".join(texts)),
        )

        async def produce(idem: str) -> dict:
            vectors = await self._inner.embed(texts, idempotency_key=idem)
            return {"vectors": vectors}

        with span(
            "embed",
            node=call.node,
            session_id=call.session_id,
            count=len(texts),
        ):
            cached = await self._ledger.run(key, produce)
        return cached["vectors"]


class LedgerSearchProvider:
    """A :class:`SearchProvider` that de-duplicates search calls through the durable
    ledger — so a crash/resume reads the committed hits instead of re-paying the
    external search, and concurrent duplicate queries single-flight.

    Unlike the LLM/embedding wrappers, a search backend can't honour a server-side
    idempotency key (Tavily has no such header), so the ledger's ``done``
    short-circuit is the protection — not provider-side dedup. The narrow window of
    a crash *after* the search returns but *before* commit can still re-charge once
    on takeover; everything else (every ordinary resume) is covered."""

    def __init__(self, inner: SearchProvider, ledger: CallLedger) -> None:
        self._inner = inner
        self._ledger = ledger

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        call: CallSite | None = None,
    ) -> list[SearchHit]:
        if call is None:
            # No call-site → can't key the ledger; pass straight through.
            return await self._inner.search(query, max_results=max_results)

        key = LedgerKey(
            session_id=call.session_id,
            node=call.node,
            attempt=call.attempt,
            input_hash=content_hash("\x1f".join((query, str(max_results)))),
        )

        async def produce(idem: str) -> dict:
            hits = await self._inner.search(query, max_results=max_results)
            return {"hits": [h.model_dump(mode="json") for h in hits]}

        with span(
            "search",
            node=call.node,
            session_id=call.session_id,
            attempt=call.attempt,
        ):
            cached = await self._ledger.run(key, produce)
        return [SearchHit.model_validate(h) for h in cached["hits"]]
