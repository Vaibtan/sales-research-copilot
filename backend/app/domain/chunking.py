"""Text chunking for the semantic layer (PRD §7, slice 9).

Splits the served report (per section) and each source into bounded chunks that
are embedded and stored in pgvector for chat retrieval (slice 10) and cross-session
memory (slice 11). Chunking is paragraph-then-size aware so a chunk stays a
coherent passage; ids are deterministic so re-persist upserts rather than
duplicating, and a shrink prunes the rest.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.identity import report_chunk_id, source_chunk_id
from app.domain.models import ResearchReport, Source

DEFAULT_MAX_CHARS = 800


def chunk_text(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split ``text`` into chunks of at most ``max_chars``, preferring paragraph
    boundaries and never splitting mid-paragraph unless a paragraph alone exceeds
    the cap (then it is hard-split). Returns ``[]`` for blank input."""
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()] or [text]

    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        if len(para) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            for i in range(0, len(para), max_chars):
                chunks.append(para[i : i + max_chars])
            continue
        candidate = f"{buffer}\n\n{para}" if buffer else para
        if len(candidate) > max_chars:
            chunks.append(buffer)
            buffer = para
        else:
            buffer = candidate
    if buffer:
        chunks.append(buffer)
    return chunks


@dataclass(frozen=True, slots=True)
class ChunkSpec:
    """A chunk to embed + store. ``id``/``owner`` route it to the right table:
    a report chunk's owner is its section; a source chunk's is its ``source_id``."""

    id: str
    kind: str  # "report" | "source"
    owner: str
    chunk_ix: int
    text: str


def report_chunk_specs(
    session_id: str, report: ResearchReport, *, max_chars: int = DEFAULT_MAX_CHARS
) -> list[ChunkSpec]:
    """One chunk stream per section (its items joined), plus Unknowns."""
    specs: list[ChunkSpec] = []
    blocks: list[tuple[str, str]] = []
    for section in report.sections:
        body = "\n\n".join(item.text for item in section.items)
        blocks.append((section.section.value, body))
    if report.unknowns:
        blocks.append(("Unknowns", "\n\n".join(report.unknowns)))

    for label, body in blocks:
        for ix, text in enumerate(chunk_text(body, max_chars=max_chars)):
            specs.append(
                ChunkSpec(
                    id=report_chunk_id(session_id, label, ix),
                    kind="report",
                    owner=label,
                    chunk_ix=ix,
                    text=text,
                )
            )
    return specs


def source_chunk_specs(
    sources: list[Source], *, max_chars: int = DEFAULT_MAX_CHARS
) -> list[ChunkSpec]:
    """Chunk each source's title + snippet (the durable text we retain)."""
    specs: list[ChunkSpec] = []
    for source in sources:
        body = f"{source.title}\n\n{source.snippet}".strip()
        for ix, text in enumerate(chunk_text(body, max_chars=max_chars)):
            specs.append(
                ChunkSpec(
                    id=source_chunk_id(source.id, ix),
                    kind="source",
                    owner=source.id,
                    chunk_ix=ix,
                    text=text,
                )
            )
    return specs
