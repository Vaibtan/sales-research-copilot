"""Local dev entrypoint: ``uv run python -m app``.

Exists mainly so Windows uses a SelectorEventLoop (psycopg3's async pool is
incompatible with the default ProactorEventLoop). In Docker/Linux the container
runs ``uvicorn app.main:app`` directly — uvloop there is already compatible.
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from app.settings import get_settings


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.environment == "development",
        log_config=None,  # logging is configured by the app (structlog)
    )


if __name__ == "__main__":
    main()
