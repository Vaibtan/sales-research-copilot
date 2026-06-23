"""OpenTelemetry tracing, exported to Langfuse (PRD §0, §11, §14).

The whole app is instrumented with the OTEL **API** (``get_tracer().start_as_
current_span``). When tracing is not configured (the default, and all of CI) the
global provider is a no-op, so every span is a cheap no-op and there is **no hard
dependency on Langfuse** — core dev runs with the obs profile off. When
``otel_enabled`` is set with Langfuse keys, ``configure_tracing`` installs a real
``TracerProvider`` that batches spans to Langfuse's OTLP endpoint, so a completed
run shows up as a trace with per-node / per-LLM-call spans (token/cost/latency).
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from typing import Iterator

from opentelemetry import trace
from opentelemetry.trace import Span

from app.logging import get_logger

log = get_logger(__name__)

_SERVICE = "sales-research-copilot"
_configured = False


def configure_tracing(settings) -> bool:
    """Install the OTLP→Langfuse tracer provider when enabled. Idempotent; returns
    True if real tracing was configured, False if left as the no-op default."""
    global _configured
    if _configured:
        return True
    if not (
        settings.otel_enabled
        and settings.langfuse_host
        and settings.langfuse_public_key
        and settings.langfuse_secret_key
    ):
        log.info("tracing.disabled")
        return False

    # Imported lazily so the SDK isn't loaded on the no-op path.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    exporter = OTLPSpanExporter(
        endpoint=f"{settings.langfuse_host.rstrip('/')}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {auth}"},
    )
    provider = TracerProvider(resource=Resource.create({"service.name": _SERVICE}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _configured = True
    log.info("tracing.enabled", endpoint=settings.langfuse_host)
    return True


def get_tracer():
    return trace.get_tracer(_SERVICE)


@contextmanager
def span(name: str, **attributes) -> Iterator[Span]:
    """Start a current span with string-coerced attributes (no-op without a
    provider). Attribute values are stringified so arbitrary metadata is safe."""
    with get_tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value if isinstance(value, (int, float, bool)) else str(value))
        yield current


def set_current_attributes(**attributes) -> None:
    """Annotate the current span (e.g. token counts from inside the LLM client)."""
    current = trace.get_current_span()
    for key, value in attributes.items():
        if value is not None:
            current.set_attribute(key, value if isinstance(value, (int, float, bool)) else str(value))
