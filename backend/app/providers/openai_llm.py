"""Real LLM provider — OpenAI Responses API structured outputs (PRD §0, §6, slice 7).

Mirrors the mock's surface (``parse(task, context, output_type) -> Model``) so the
nodes are provider-agnostic. Each task maps to a config-driven model + token cap
(``analyze`` can step up to a stronger model, §15) and a task-specific instruction
that encodes the grounding/citation contract. The ledger key is passed through as
the provider ``Idempotency-Key`` header (§6.5) so a re-sent call after a crash is
de-duped by OpenAI, not re-charged.

Construction of the SDK client lives in ``build_providers`` (only when a key is
configured); this module accepts an injected client so the mapping logic is
unit-testable without the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.logging import get_logger
from app.providers.base import CallSite, LLMTask, T
from app.providers.errors import ModelVerificationError, ProviderError

if TYPE_CHECKING:
    from openai import AsyncOpenAI

log = get_logger(__name__)


# Task instructions — the system-level contract. The typed context is sent as JSON
# `input`; these tell the model what to produce and, crucially, the grounding rules
# (cite sources, never fabricate, route the unsupported to Unknowns).
_INSTRUCTIONS: dict[LLMTask, str] = {
    LLMTask.PLAN: (
        "You are a B2B sales research planner. Given a company, optional website, "
        "and the seller's objective, produce focused research questions — one or "
        "more per provided section — that, once answered, yield a grounded sales "
        "briefing. Questions must be specific and answerable from public web "
        "sources. Return only the structured plan."
    ),
    LLMTask.EXTRACT: (
        "You extract factual claims that answer a single research question from a "
        "single source. Use ONLY the provided source text — never add outside "
        "knowledge. Each claim must be directly supported by the source; assign a "
        "calibrated confidence in [0,1]. If the source does not address the "
        "question, return no claims."
    ),
    LLMTask.ANALYZE: (
        "You are a sales analyst. Synthesize the provided facts into the briefing's "
        "content sections, citing the source id(s) backing each item. Do not invent "
        "facts: anything not supported by a provided fact goes to Unknowns. Every "
        "section item must carry at least one source id from the provided facts."
    ),
    LLMTask.QUALITY: (
        "You are a strict reviewer. Score the draft briefing in [0,1] for "
        "grounding, completeness, and usefulness for the objective. List concrete "
        "gaps and name the question ids (from the provided plan) whose evidence is "
        "weak or missing, so they can be re-researched. Be conservative."
    ),
    LLMTask.CHAT: (
        "You answer questions strictly from the provided report excerpts and "
        "sources. Cite the source id(s) for every claim. If the answer is not in "
        "the provided context, say it is not in the report — never fabricate."
    ),
}


@dataclass(frozen=True, slots=True)
class ModelSpec:
    model: str
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class LLMRouting:
    """Per-task model + token-cap routing, built from settings (§16 placeholders)."""

    specs: dict[LLMTask, ModelSpec]

    def model_ids(self) -> set[str]:
        return {spec.model for spec in self.specs.values()}

    @classmethod
    def from_settings(cls, s) -> "LLMRouting":
        return cls(
            specs={
                LLMTask.PLAN: ModelSpec(s.model_plan, s.max_output_tokens_plan),
                LLMTask.EXTRACT: ModelSpec(s.model_extract, s.max_output_tokens_extract),
                LLMTask.ANALYZE: ModelSpec(s.model_analyze, s.max_output_tokens_analyze),
                LLMTask.QUALITY: ModelSpec(s.model_quality, s.max_output_tokens_quality),
                LLMTask.CHAT: ModelSpec(s.model_chat, s.max_output_tokens_chat),
            }
        )


class OpenAILLMClient:
    """An :class:`LLMClient` backed by the OpenAI Responses ``parse`` API."""

    def __init__(self, client: "AsyncOpenAI", routing: LLMRouting) -> None:
        self._client = client
        self._routing = routing

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
        spec = self._routing.specs[task]
        model_id = model or spec.model
        extra_headers = (
            {"Idempotency-Key": idempotency_key} if idempotency_key else None
        )

        response = await self._client.responses.parse(
            model=model_id,
            instructions=_INSTRUCTIONS[task],
            input=context.model_dump_json(),
            text_format=output_type,
            max_output_tokens=spec.max_output_tokens,
            extra_headers=extra_headers,
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", None)
            output_tokens = getattr(usage, "output_tokens", None)
            # Cost/latency observability (slice 14): log + annotate the active span.
            log.info(
                "llm.usage",
                task=task.value,
                model=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            from app.observability.tracing import set_current_attributes

            set_current_attributes(
                **{
                    "llm.model": model_id,
                    "llm.input_tokens": input_tokens,
                    "llm.output_tokens": output_tokens,
                }
            )

        parsed = response.output_parsed
        if parsed is None:
            raise ProviderError(
                f"OpenAI returned no parseable {output_type.__name__} for {task.value}"
            )
        return parsed


async def verify_models(client: "AsyncOpenAI", model_ids: set[str]) -> None:
    """Fail fast at boot if a configured model id is not available on the live API
    (PRD §16). Retrieves each id; a 404 → :class:`ModelVerificationError`."""
    import openai

    for model_id in sorted(model_ids):
        try:
            await client.models.retrieve(model_id)
        except openai.NotFoundError as exc:
            raise ModelVerificationError(
                f"configured model id {model_id!r} is not available on the API"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — surface a clear, attributable error
            # A transient/auth error at the verify gate would otherwise propagate an
            # opaque traceback out of app startup; wrap it so the failure is clear
            # (which model, that it was verification-time) for the operator.
            raise ModelVerificationError(
                f"could not verify model id {model_id!r} at boot: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    log.info("openai.models_verified", models=sorted(model_ids))
