"""The grounded follow-up chat graph (PRD §8) — a small ``retrieve → answer``
LangGraph, separate from the research graph.

``retrieve`` embeds the question and pulls the top-k pgvector chunks scoped to the
session; ``answer`` calls the cheap model with *only* those passages + the recent
turns and must cite ``source_id``s (out-of-scope → "not in the report"). Compiled
without a checkpointer — each turn is independent and history lives in the
relational ``messages`` table, not a graph checkpoint.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.db.repository import retrieve_chunks
from app.domain.models import ChatAnswer, ChatPassage, ChatTurn
from app.infra.database import Database
from app.providers.base import LLMClient, LLMTask
from app.providers.contexts import ChatContext
from app.providers.embeddings import EmbeddingClient


class ChatState(TypedDict):
    session_id: str
    question: str
    history: list[ChatTurn]
    passages: list[ChatPassage]
    answer: ChatAnswer | None


def make_retrieve_node(db: Database, embeddings: EmbeddingClient, *, top_k: int):
    async def retrieve(state: ChatState) -> dict:
        [vector] = await embeddings.embed([state["question"]])
        passages = await retrieve_chunks(
            db, session_id=state["session_id"], query_vector=vector, top_k=top_k
        )
        return {"passages": passages}

    return retrieve


def make_answer_node(llm: LLMClient):
    async def answer(state: ChatState) -> dict:
        ctx = ChatContext(
            question=state["question"],
            passages=state["passages"],
            history=state["history"],
        )
        result = await llm.parse(
            task=LLMTask.CHAT, context=ctx, output_type=ChatAnswer
        )
        return {"answer": result}

    return answer


def build_chat_graph(
    *,
    llm: LLMClient,
    embeddings: EmbeddingClient,
    db: Database,
    top_k: int = 6,
) -> CompiledStateGraph:
    builder = StateGraph(ChatState)
    builder.add_node("retrieve", make_retrieve_node(db, embeddings, top_k=top_k))
    builder.add_node("answer", make_answer_node(llm))
    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "answer")
    builder.add_edge("answer", END)
    return builder.compile()
