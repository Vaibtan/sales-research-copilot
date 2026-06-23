"""Provider error taxonomy + the transient/retryable predicate (PRD §9, §11).

``is_transient`` is the ``retry_on`` for the network-bound nodes' ``RetryPolicy``
(slice 7) and the input to the error taxonomy → HTTP mapping (slice 8). Imports of
``openai``/``httpx`` are lazy so this module is cheap to import and works even when
the real providers aren't in use.
"""

from __future__ import annotations


class ProviderError(Exception):
    """A provider call failed in a way the caller should surface (not retry)."""


class ModelVerificationError(ProviderError):
    """A configured model id is not available on the live API (fail-fast at boot)."""


def is_transient(exc: BaseException) -> bool:
    """True for errors worth retrying: network blips, timeouts, rate limits, 5xx.

    A 4xx (other than 429) is a permanent client error — not retried. Kept import-
    light: the provider SDKs are only imported if they're installed.
    """
    import httpx

    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True

    try:
        import openai
    except ImportError:  # pragma: no cover - openai is a runtime dep
        openai = None  # type: ignore[assignment]

    if openai is not None:
        if isinstance(
            exc,
            (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError),
        ):
            return True
        if isinstance(exc, openai.APIStatusError):
            return exc.status_code >= 500

    return False
