"""``company_key`` — the cross-session join key for research memory (§16).

Default: the registrable domain (eTLD+1) of the website, e.g.
``https://www.bbc.co.uk/news`` → ``bbc.co.uk``. Falls back to the normalized
company name when no website / no public suffix is available.
"""

from __future__ import annotations

import tldextract

from app.domain.identity import normalize_company

# Use the bundled public-suffix snapshot (no network fetch at runtime).
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def company_key(*, website: str | None, company: str) -> str:
    if website:
        registrable = _extract(website).top_domain_under_public_suffix
        if registrable:
            return registrable.lower()
    return normalize_company(company)
