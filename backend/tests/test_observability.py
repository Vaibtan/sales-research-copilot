"""Slice 14 — observability: structured per-node logs + OTEL tracing seam.

Tracing is a no-op without Langfuse configured (core dev has no hard dependency);
the per-node instrumentation binds session_id/node to every log line and emits a
duration on completion.
"""

from __future__ import annotations

import structlog

import app.graph.build as build_mod
from app.observability import configure_tracing, span
from app.observability.tracing import set_current_attributes
from app.settings import Settings


# --- tracing no-op safety ----------------------------------------------------


def test_tracing_disabled_without_config() -> None:
    # Default settings → tracing stays the no-op provider (no Langfuse dependency).
    assert configure_tracing(Settings()) is False


def test_span_and_attrs_are_safe_without_provider() -> None:
    # Creating spans / setting attributes must never raise on the no-op path.
    with span("test.span", node="x", session_id="s", n=3):
        set_current_attributes(**{"llm.input_tokens": 10})


def test_configure_tracing_gate_requires_all_langfuse_settings() -> None:
    # otel_enabled alone (no host/keys) is not enough → stays disabled.
    assert configure_tracing(Settings(otel_enabled=True)) is False


# --- per-node instrumentation ------------------------------------------------


async def test_instrument_emits_node_log_with_session_and_duration(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    class _FakeLog:
        def info(self, event, **kw):
            calls.append((event, kw))

    monkeypatch.setattr(build_mod, "log", _FakeLog())

    async def fake_node(state):
        return {"status": "ok"}

    wrapped = build_mod._instrument("plan", fake_node)
    result = await wrapped({"session_id": "sess-123"})

    assert result == {"status": "ok"}
    node_logs = [kw for ev, kw in calls if ev == "graph.node"]
    assert len(node_logs) == 1
    assert node_logs[0]["node"] == "plan"
    assert node_logs[0]["session_id"] == "sess-123"
    assert isinstance(node_logs[0]["duration_ms"], (int, float))


async def test_instrument_binds_contextvars_for_inner_logs() -> None:
    seen: dict = {}

    async def fake_node(state):
        # Any log emitted inside the node inherits these via merge_contextvars.
        seen.update(structlog.contextvars.get_contextvars())
        return {}

    wrapped = build_mod._instrument("gather", fake_node)
    await wrapped({"session_id": "sess-xyz"})

    assert seen.get("node") == "gather"
    assert seen.get("session_id") == "sess-xyz"
    # Bindings are unbound after the node (no leak into the next).
    assert "node" not in structlog.contextvars.get_contextvars()


async def test_instrument_logs_duration_even_on_failure(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        build_mod, "log", type("L", (), {"info": lambda self, e, **k: calls.append((e, k))})()
    )

    async def boom(state):
        raise RuntimeError("node failed")

    wrapped = build_mod._instrument("extract", boom)
    try:
        await wrapped({"session_id": "s"})
    except RuntimeError:
        pass
    assert any(ev == "graph.node" and kw["node"] == "extract" for ev, kw in calls)
