"""Deterministic identity & normalization helpers.

Every persisted entity derives its id from its natural key (PRD §10) so that
re-research / resume converge instead of duplicating rows. ``session_id`` is the
one exception — a session is genuinely new each run, so it gets a random id.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

_SEP = "\x1f"  # unit separator — unlikely to appear in inputs


def _digest(*parts: str) -> str:
    """128-bit hex id from the joined natural-key parts."""
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()[:32]


def new_session_id() -> str:
    return uuid4().hex


def normalize_url(url: str) -> str:
    """Canonicalize a URL for dedup: lowercase scheme/host, drop default ports,
    strip trailing slash and fragment, keep the query (it identifies the page)."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = parts.hostname or ""
    netloc = host.lower()
    if parts.port and not (
        (scheme == "http" and parts.port == 80)
        or (scheme == "https" and parts.port == 443)
    ):
        netloc = f"{netloc}:{parts.port}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def normalize_claim(text: str) -> str:
    """Lowercase + collapse whitespace so reworded-identical claims dedupe."""
    return re.sub(r"\s+", " ", text.strip().lower())


def normalize_company(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def source_id(session_id: str, url: str) -> str:
    return _digest(session_id, normalize_url(url))


def fact_id(session_id: str, question_id: str, source_id_: str, claim_norm: str) -> str:
    return _digest(session_id, question_id, source_id_, claim_norm)


def question_id(session_id: str, index: int, text: str) -> str:
    return _digest(session_id, str(index), normalize_claim(text))


def report_id(session_id: str) -> str:
    """One report per session (§10)."""
    return _digest(session_id)


def report_chunk_id(session_id: str, section: str, chunk_ix: int) -> str:
    return _digest(session_id, "report", section, str(chunk_ix))


def source_chunk_id(source_id_: str, chunk_ix: int) -> str:
    return _digest("source", source_id_, str(chunk_ix))


def content_hash(text: str) -> str:
    return _digest(text)
