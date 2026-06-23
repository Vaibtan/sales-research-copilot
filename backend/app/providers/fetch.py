"""SSRF-guarded website fetch (PRD §11, §15 risk row).

User-supplied websites and discovered source urls are fetched server-side, so an
attacker could point us at internal services (cloud metadata at 169.254.169.254,
``localhost``, RFC-1918, RFC-6598 CGNAT, …). :func:`assert_public_url` resolves the
host and **rejects any url that resolves to a non-public address**, and
:func:`fetch_text` re-applies that check on **every redirect hop** (auto-redirects
are disabled and followed manually) so a public url that 302s to an internal one is
blocked too. The guard is pure/synchronous and unit-testable without network access.

Known residual (low severity, documented): a DNS-rebinding host could in principle
return a public A record to :func:`assert_public_url` and a private one to httpx's
own resolution milliseconds later. Fully closing that requires pinning the
connection to the validated IP (a custom transport); it is out of scope here.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from app.logging import get_logger

log = get_logger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5

# Ranges the stdlib ipaddress flags don't mark, but which are non-public:
# RFC 6598 carrier-grade NAT / shared address space, and the benchmarking range.
_EXTRA_BLOCKED = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("198.18.0.0/15"),
)


class SsrfError(Exception):
    """A url was blocked because it is not safely public (SSRF guard)."""


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if any(addr in net for net in _EXTRA_BLOCKED):
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def assert_public_url(url: str, *, allow_private: bool = False) -> None:
    """Raise :class:`SsrfError` unless ``url`` is http(s) and every address its
    host resolves to is public. ``allow_private`` is a test-only escape hatch."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise SsrfError(f"blocked non-http(s) scheme: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise SsrfError("blocked url with no host")
    if allow_private:
        return

    # An IP literal is checked directly; a hostname is resolved to *all* its
    # addresses (a host that returns one internal A record is blocked because we
    # check every result).
    try:
        infos = socket.getaddrinfo(host, parts.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfError(f"could not resolve host {host!r}") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise SsrfError(f"host {host!r} resolved to no addresses")
    for ip in addresses:
        if not _is_public_ip(ip):
            raise SsrfError(f"blocked non-public address {ip} for host {host!r}")


async def fetch_text(
    url: str,
    *,
    timeout: float = 10.0,
    max_bytes: int = 2_000_000,
    allow_private: bool = False,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Fetch ``url`` as text, capping the body at ``max_bytes``. The SSRF check runs
    on the initial url **and every redirect hop** — redirects are not auto-followed
    (which would skip the guard) but followed manually, validating each ``Location``
    before connecting. Raises :class:`SsrfError` if blocked; network/HTTP errors
    propagate (classified by ``is_transient``)."""
    owns_client = client is None
    client = client or httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,  # guard each hop ourselves; auto-follow bypasses SSRF
        headers={"User-Agent": "sales-research-copilot/0.1"},
    )
    try:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            assert_public_url(current, allow_private=allow_private)
            async with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        resp.raise_for_status()
                        return ""
                    current = str(resp.url.join(location))  # resolve relative redirects
                    continue
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
                return b"".join(chunks)[:max_bytes].decode(
                    resp.encoding or "utf-8", "replace"
                )
        raise SsrfError(f"too many redirects fetching {url!r}")
    finally:
        if owns_client:
            await client.aclose()
