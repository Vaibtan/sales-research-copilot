"""Real search provider — Tavily + optional SSRF-guarded site fetch (PRD §0, slice 7).

Implements the same ``SearchProvider.search`` surface as the mock so ``gather`` is
provider-agnostic. Tavily returns ranked results with a content snippet (and, when
asked, page ``raw_content``); when raw content is missing we best-effort fetch the
page ourselves through the SSRF guard. A fetch failure degrades to the snippet
rather than failing the search.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.logging import get_logger
from app.providers.base import CallSite, SearchHit
from app.providers.fetch import SsrfError, fetch_text

if TYPE_CHECKING:
    from tavily import AsyncTavilyClient

log = get_logger(__name__)


class TavilySearchProvider:
    def __init__(
        self,
        client: "AsyncTavilyClient",
        *,
        search_depth: str = "basic",
        enrich_with_fetch: bool = True,
        fetch_timeout: float = 10.0,
        fetch_max_bytes: int = 2_000_000,
        allow_private_fetch: bool = False,
    ) -> None:
        self._client = client
        self._search_depth = search_depth
        self._enrich = enrich_with_fetch
        self._fetch_timeout = fetch_timeout
        self._fetch_max_bytes = fetch_max_bytes
        self._allow_private_fetch = allow_private_fetch

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        call: CallSite | None = None,
    ) -> list[SearchHit]:
        response = await self._client.search(
            query,
            max_results=max_results,
            search_depth=self._search_depth,
            include_raw_content=True,
        )
        hits: list[SearchHit] = []
        for result in response.get("results", []):
            url = result.get("url")
            if not url:
                continue
            snippet = result.get("content") or ""
            content = result.get("raw_content")
            if not content and self._enrich:
                content = await self._maybe_fetch(url)
            hits.append(
                SearchHit(
                    url=url,
                    title=result.get("title") or url,
                    snippet=snippet,
                    content=content or snippet,
                )
            )
        return hits

    async def _maybe_fetch(self, url: str) -> str | None:
        """Best-effort page fetch through the SSRF guard; never fails the search."""
        try:
            return await fetch_text(
                url,
                timeout=self._fetch_timeout,
                max_bytes=self._fetch_max_bytes,
                allow_private=self._allow_private_fetch,
            )
        except SsrfError:
            log.warning("search.fetch_blocked_ssrf", url=url)
            return None
        except Exception:  # noqa: BLE001 — enrichment is best-effort
            log.warning("search.fetch_failed", url=url, exc_info=True)
            return None
