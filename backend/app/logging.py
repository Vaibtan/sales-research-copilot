"""Structured logging scaffold.

structlog is wired into the stdlib ``logging`` root so that *every* log line —
app, uvicorn, third-party — is rendered through one pipeline (JSON in
production, a colorised console renderer in development). Per-node workflow
fields (``session_id``, ``node``) are bound via ``contextvars`` and merged here;
the full per-node instrumentation lands in slice 14.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_PreChain = list[Any]


def _shared_processors() -> _PreChain:
    """Processors applied to both structlog and foreign (stdlib) records."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging(*, json_logs: bool, level: str = "INFO") -> None:
    """Idempotently configure structlog + stdlib logging for the process."""
    shared = _shared_processors()

    structlog.configure(
        processors=[
            *shared,
            # Hand off to the stdlib formatter so foreign logs share rendering.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    # Route uvicorn through the root handler instead of its own formatters.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers[:] = []
        uv_logger.propagate = True


def get_logger(*args: Any, **kwargs: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(*args, **kwargs)
