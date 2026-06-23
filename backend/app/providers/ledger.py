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

from pydantic import BaseModel

from app.db.ledger import CallLedger, LedgerKey
from app.domain.identity import content_hash
from app.observability import span
from app.providers.base import CallSite, LLMClient, LLMTask, T
from app.providers.embeddings import EmbeddingClient


def _input_hash(
    task: LLMTask, context: BaseModel, output_type: type[BaseModel], model: str | None
) -> str:
    """Canonical fingerprint of everything that determines the response — so the
    same logical call re-derives the same ledger key, and any change (model, prompt
    inputs, output schema) is a distinct call."""
    return content_hash(
        "\x1f".join(
            (
                task.value,
                model or "",
                output_type.__name__,
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
