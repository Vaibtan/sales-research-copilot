"""Grounded follow-up chat orchestration (PRD §8, §8.1).

Persists the user turn, runs the ``retrieve → answer`` chat graph, snapshots the
cited sources at answer time, persists the assistant turn, and streams the answer
to the client token-by-token over SSE. History + citation durability live in the
relational ``messages`` table (slice 10).
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from langgraph.graph.state import CompiledStateGraph

from app.db import repository
from app.domain.models import ChatMessage, ChatTurn
from app.infra.database import Database
from app.logging import get_logger

log = get_logger(__name__)

_HISTORY_TURNS = 8


class ChatService:
    def __init__(self, db: Database, chat_graph: CompiledStateGraph) -> None:
        self._db = db
        self._graph = chat_graph

    async def history(self, session_id: str) -> list[ChatMessage]:
        return await repository.list_messages(self._db, session_id)

    async def answer_stream(
        self, *, session_id: str, report_version: int | None, message: str
    ) -> AsyncIterator[str]:
        """Yield SSE frames: ``token`` deltas for the answer, then a ``done`` frame
        carrying the persisted assistant message (id, citations, stale, version)."""
        history = [
            ChatTurn(**t)
            for t in await repository.recent_turns(
                self._db, session_id, limit=_HISTORY_TURNS
            )
        ]
        await repository.insert_message(
            self._db,
            session_id=session_id,
            role="user",
            content=message,
            citations=[],
            report_version=report_version,
        )

        state = await self._graph.ainvoke(
            {
                "session_id": session_id,
                "question": message,
                "history": history,
                "passages": [],
                "answer": None,
            }
        )
        answer = state["answer"]
        citations = await repository.resolve_citations(
            self._db, session_id=session_id, source_ids=answer.source_ids
        )
        assistant = await repository.insert_message(
            self._db,
            session_id=session_id,
            role="assistant",
            content=answer.answer,
            citations=citations,
            report_version=report_version,
        )
        log.info(
            "chat.answered",
            session_id=session_id,
            citations=len(citations),
            grounded=bool(citations),
        )

        for token in _tokenize(answer.answer):
            yield _sse("token", json.dumps({"text": token}))
        yield _sse(
            "done",
            json.dumps(
                {
                    "message_id": assistant.id,
                    "citations": [c.model_dump() for c in citations],
                    "report_version": report_version,
                    "stale": assistant.stale,
                }
            ),
        )


def _tokenize(text: str) -> list[str]:
    """Word-with-trailing-space tokens so the client can append deltas verbatim."""
    return [f"{word} " for word in text.split()]


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"
