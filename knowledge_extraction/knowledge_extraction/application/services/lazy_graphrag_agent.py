"""LazyGraphRAG retrieval agent — JIT subgraph construction at query time.

LazyGraphRAG (Microsoft Research, Nov 2024) flips the GraphRAG cost curve: it
**skips index-time entity / relationship extraction entirely** and constructs a
question-specific micro-graph at ask time, only over the chunks relevant to
that question. Two LLM calls per ask:

1. **Subgraph extraction** — given the question and the top-K chunks, extract a
   focused subgraph (entities + relationships + claims, all chunk-cited).
2. **Synthesis** — answer the question using both the chunks and the just-built
   subgraph as evidence, with chunk-level inline citations.

This is a query-time-only agent: it depends on a populated ``chunks`` table in
the SQLite store (any normal ingest produces this) and **does not** depend on
the Microsoft GraphRAG index. It coexists with :class:`MsGraphRagAgent` (eager,
pre-computed) and :class:`MiniGraphRagAgent` (lexical baseline) — pick the
backend per workload via ``--backend lazy|ms|mini``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
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

EXTRACT_PROMPT = "lazy_subgraph_extract"
SYNTH_PROMPT = "lazy_synthesis"
PROMPT_VERSION = "v1"


@dataclass(slots=True)
class LazySubgraph:
    entities: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class LazyTokenUsage:
    extract_input: int = 0
    extract_output: int = 0
    synth_input: int = 0
    synth_output: int = 0

    @property
    def total(self) -> int:
        return self.extract_input + self.extract_output + self.synth_input + self.synth_output


@dataclass(slots=True)
class LazyGraphRagAnswer:
    """Question + final answer + the JIT artefacts used to produce it.

    Includes the retrieved chunks and the extracted subgraph so callers (eval
    harness, MCP wrappers, debugging UIs) can audit and cite the evidence.
    """
    question: str
    answer: str
    chunks: list[ChunkHit]
    subgraph: LazySubgraph
    duration_ms: int
    tokens: LazyTokenUsage
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
            "subgraph": asdict(self.subgraph),
        }


class LazyGraphRagAgent:
    """Query-time-only GraphRAG: retrieve chunks, JIT subgraph, synthesize.

    Depends only on a populated ``chunks`` table — no MS GraphRAG index, no
    pre-computed entities or community reports. The two LLM calls per ask are
    instrumented with ``wide_event`` so per-question token spend and wall
    clock time show up in the same telemetry stream as every other stage.
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

    def ask(
        self,
        question: str,
        *,
        top_k_chunks: int = 20,
        neighbour_window: int = 1,
        max_entities: int = 40,
        max_relationships: int = 40,
        max_claims: int = 20,
    ) -> LazyGraphRagAnswer:
        """Sync wrapper around :meth:`ask_async` for the CLI."""
        return asyncio.run(
            self.ask_async(
                question,
                top_k_chunks=top_k_chunks,
                neighbour_window=neighbour_window,
                max_entities=max_entities,
                max_relationships=max_relationships,
                max_claims=max_claims,
            )
        )

    async def ask_async(
        self,
        question: str,
        *,
        top_k_chunks: int = 20,
        neighbour_window: int = 1,
        max_entities: int = 40,
        max_relationships: int = 40,
        max_claims: int = 20,
    ) -> LazyGraphRagAnswer:
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        with wide_event(
            "lazy.ask",
            question=question[:160],
            top_k_chunks=top_k_chunks,
            model=self._model,
        ) as ev:
            chunks = self._retrieve(question, top_k=top_k_chunks, window=neighbour_window)
            ev.update(retrieved_chunks=len(chunks))
            if not chunks:
                return LazyGraphRagAnswer(
                    question=question,
                    answer="No relevant chunks were retrieved from the knowledge store.",
                    chunks=[],
                    subgraph=LazySubgraph(),
                    duration_ms=int((loop.time() - t0) * 1000),
                    tokens=LazyTokenUsage(),
                    model=self._model,
                )

            subgraph, extract_tokens = await self._extract_subgraph(
                question, chunks,
                max_entities=max_entities,
                max_relationships=max_relationships,
                max_claims=max_claims,
            )
            ev.update(
                entities=len(subgraph.entities),
                relationships=len(subgraph.relationships),
                claims=len(subgraph.claims),
            )

            answer, synth_tokens = await self._synthesize(question, chunks, subgraph)

            usage = LazyTokenUsage(
                extract_input=extract_tokens[0],
                extract_output=extract_tokens[1],
                synth_input=synth_tokens[0],
                synth_output=synth_tokens[1],
            )
            ev.update(total_tokens=usage.total)
            return LazyGraphRagAnswer(
                question=question,
                answer=answer,
                chunks=chunks,
                subgraph=subgraph,
                duration_ms=int((loop.time() - t0) * 1000),
                tokens=usage,
                model=self._model,
            )

    # ---------------------------------------------------------------- helpers

    def _retrieve(self, question: str, *, top_k: int, window: int) -> list[ChunkHit]:
        """BM25 top-K then dedup-merge with neighbours of each retrieved chunk."""
        primary = self._chunks.search(question, top_k=top_k)
        if window <= 0 or not primary:
            return primary
        seen = {c.id for c in primary}
        for hit in list(primary):
            for nb in self._chunks.neighbors(hit.id, window=window):
                if nb.id in seen:
                    continue
                seen.add(nb.id)
                primary.append(nb)
        return primary

    async def _extract_subgraph(
        self,
        question: str,
        chunks: list[ChunkHit],
        *,
        max_entities: int,
        max_relationships: int,
        max_claims: int,
    ) -> tuple[LazySubgraph, tuple[int, int]]:
        prompt = self._prompts.render(
            EXTRACT_PROMPT, PROMPT_VERSION,
            question=question,
            chunks=[_chunk_for_prompt(c) for c in chunks],
            max_entities=max_entities,
            max_relationships=max_relationships,
            max_claims=max_claims,
        )
        with wide_event("lazy.extract", model=self._model, n_chunks=len(chunks)) as ev:
            resp = await self._llm.complete_json(
                model=self._model, system=prompt.system, user=prompt.user, max_tokens=2048,
            )
            ev.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)
        data = _safe_json(resp.text)
        sub = LazySubgraph(
            entities=_listify(data.get("entities")),
            relationships=_listify(data.get("relationships")),
            claims=_listify(data.get("claims")),
        )
        return sub, (resp.input_tokens, resp.output_tokens)

    async def _synthesize(
        self,
        question: str,
        chunks: list[ChunkHit],
        subgraph: LazySubgraph,
    ) -> tuple[str, tuple[int, int]]:
        prompt = self._prompts.render(
            SYNTH_PROMPT, PROMPT_VERSION,
            question=question,
            entities=subgraph.entities,
            relationships=subgraph.relationships,
            claims=subgraph.claims,
            chunks=[_chunk_for_prompt(c) for c in chunks],
        )
        with wide_event("lazy.synthesize", model=self._model, n_chunks=len(chunks)) as ev:
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


def _listify(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def lazy_index_available(settings: Settings) -> bool:
    """True iff the SQLite store has a non-empty ``chunks`` table.

    Lazy mode's only hard prerequisite — checked by the CLI before instantiating
    the agent so we can give a clean error message instead of an empty answer.
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
    "LazyGraphRagAgent",
    "LazyGraphRagAnswer",
    "LazySubgraph",
    "LazyTokenUsage",
    "lazy_index_available",
]
