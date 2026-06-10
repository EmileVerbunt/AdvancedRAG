"""Dense RAG agent — evidence retrieval then synthesis, with no graph at all.

The simplest member of the backend family: retrieve the top-K most relevant
chunks (the *evidence*) and synthesize an answer in a single LLM call. There is
no just-in-time subgraph extraction (cf. :class:`LazyGraphRagAgent`), no
pre-computed entity/community index (cf. :class:`MsGraphRagAgent`) and no
agentic plan→critique loop (cf. :class:`AgenticSearchAgent`).

This exists to isolate the value of the graph layer: running ``dense`` next to
``lazy``/``ms`` over the *same* retrieved evidence shows exactly what the graph
adds on top of plain retrieve-and-read RAG.

A note on the name: "dense" denotes the classic *dense-passage RAG* shape
(retrieve passages → stuff into context → generate). The current evidence
retriever (:class:`ChunkRetriever`) scores chunks lexically (BM25-style); this
agent is agnostic to the scoring method, so a future swap to a vector retriever
needs no change here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

import orjson

from knowledge_extraction.application.ports import LLMPort
from knowledge_extraction.application.services.chunk_retriever import (
    ChunkHit,
    ChunkRetriever,
)
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.config.settings import Settings
from knowledge_extraction.infrastructure.telemetry.observability import wide_event

log = logging.getLogger(__name__)

SYNTH_PROMPT = "dense_synthesis"
PROMPT_VERSION = "v1"


@dataclass(slots=True)
class DenseTokenUsage:
    synth_input: int = 0
    synth_output: int = 0

    @property
    def total(self) -> int:
        return self.synth_input + self.synth_output


@dataclass(slots=True)
class DenseRagAnswer:
    """Question + final answer + the retrieved chunks used to produce it.

    Includes the retrieved chunks so callers (eval harness, MCP wrappers,
    debugging UIs) can audit and cite the evidence.
    """
    question: str
    answer: str
    chunks: list[ChunkHit]
    duration_ms: int
    tokens: DenseTokenUsage
    model: str

    def to_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "answer": self.answer,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "tokens": asdict(self.tokens),
            "chunks": [
                {
                    "id": c.id, "score": c.score, "page_start": c.page_start,
                    "page_end": c.page_end, "document_title": c.document_title,
                }
                for c in self.chunks
            ],
        }


class DenseRagAgent:
    """Plain retrieve-and-read RAG over the SQLite chunk store.

    Depends only on a populated ``chunks`` table — no MS GraphRAG index, no
    pre-computed entities or community reports. The single synthesis LLM call
    is instrumented with ``wide_event`` so per-question token spend and wall
    clock time show up in the same telemetry stream as every other backend.
    """

    def __init__(
        self,
        settings: Settings,
        chunk_retriever: ChunkRetriever,
        llm: LLMPort,
        prompts: PromptRegistry,
        *,
        model: str | None = None,
    ) -> None:
        self._settings = settings
        self._chunks = chunk_retriever
        self._llm = llm
        self._prompts = prompts
        self._model = model or settings.azure_openai_extraction_model

    # ---------------------------------------------------------------- queries

    def ask(self, question: str, *, top_k_chunks: int = 20) -> DenseRagAnswer:
        """Sync wrapper around :meth:`ask_async` for the CLI."""
        return asyncio.run(self.ask_async(question, top_k_chunks=top_k_chunks))

    async def ask_async(self, question: str, *, top_k_chunks: int = 20) -> DenseRagAnswer:
        t0 = time.perf_counter()
        with wide_event(
            "dense.ask",
            question=question[:160],
            top_k_chunks=top_k_chunks,
            model=self._model,
        ) as ev:
            chunks = self._chunks.search(question, top_k=top_k_chunks)
            ev.update(retrieved_chunks=len(chunks))
            if not chunks:
                return DenseRagAnswer(
                    question=question,
                    answer="No relevant chunks were retrieved from the knowledge store.",
                    chunks=[],
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    tokens=DenseTokenUsage(),
                    model=self._model,
                )

            answer, synth_tokens = await self._synthesize(question, chunks)
            usage = DenseTokenUsage(synth_input=synth_tokens[0], synth_output=synth_tokens[1])
            ev.update(total_tokens=usage.total)
            return DenseRagAnswer(
                question=question,
                answer=answer,
                chunks=chunks,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                tokens=usage,
                model=self._model,
            )

    # ---------------------------------------------------------------- helpers

    async def _synthesize(
        self, question: str, chunks: list[ChunkHit],
    ) -> tuple[str, tuple[int, int]]:
        prompt = self._prompts.render(
            SYNTH_PROMPT, PROMPT_VERSION,
            question=question,
            chunks=[_chunk_for_prompt(c) for c in chunks],
        )
        with wide_event("dense.synthesize", model=self._model, n_chunks=len(chunks)) as ev:
            resp = await self._llm.complete_json(
                model=self._model, system=prompt.system, user=prompt.user, max_tokens=2048,
            )
            ev.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)
        # Synthesis prompt asks for prose; the LLM may return it directly OR
        # wrap it in {"answer": "..."}. Tolerate both, fall back to raw text.
        data = _safe_json(resp.text)
        answer = ""
        if isinstance(data, dict):
            for key in ("answer", "response", "result", "text"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    answer = value.strip()
                    break
        if not answer:
            answer = resp.text.strip()
        return answer, (resp.input_tokens, resp.output_tokens)


def _chunk_for_prompt(c: ChunkHit) -> dict[str, object]:
    """Slim chunk view passed into Jinja templates (avoids leaking ChunkHit shape)."""
    return {
        "id": c.id,
        "text": c.text,
        "page_start": c.page_start,
        "page_end": c.page_end,
        "document_title": c.document_title,
    }


def _safe_json(text: str) -> dict[str, Any]:
    try:
        loaded = orjson.loads(text)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def dense_index_available(settings: Settings) -> bool:
    """True iff the SQLite store has a non-empty ``chunks`` table.

    Dense mode's only hard prerequisite — checked by the CLI before
    instantiating the agent so we can give a clean error message instead of an
    empty answer.
    """
    import sqlite3
    if not settings.sqlite_path.exists():
        return False
    try:
        con = sqlite3.connect(str(settings.sqlite_path))
        try:
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks' LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            count = con.execute("SELECT COUNT(*) FROM chunks").fetchone()
            return bool(count and count[0])
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False


__all__ = [
    "DenseRagAgent",
    "DenseRagAnswer",
    "DenseTokenUsage",
    "dense_index_available",
]
