"""Application ports (Protocols) — adapter seams for dependency injection."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from knowledge_extraction.domain import (
    ChartInterpretation,
    Chunk,
    Document,
    ExtractionResult,
    Figure,
    GraphEdge,
    GraphNode,
)


class LLMResponse(Protocol):
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


@runtime_checkable
class IngestionPort(Protocol):
    name: str

    async def ingest(self, pdf_path: Path, work_dir: Path) -> Document: ...


@runtime_checkable
class PageRendererPort(Protocol):
    async def render(self, pdf_path: Path, out_dir: Path, dpi: int = 150) -> list[Path]: ...


@runtime_checkable
class LLMPort(Protocol):
    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse: ...


@runtime_checkable
class VisionPort(Protocol):
    async def interpret_figure(self, figure: Figure, prompt: str) -> ChartInterpretation: ...


@runtime_checkable
class EmbeddingPort(Protocol):
    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]: ...


@runtime_checkable
class VectorStorePort(Protocol):
    async def upsert(
        self,
        collection: str,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
        payloads: Sequence[dict[str, object]],
    ) -> None: ...

    async def search(
        self, collection: str, vector: Sequence[float], top_k: int = 10,
    ) -> list[tuple[str, float, dict[str, object]]]: ...


@runtime_checkable
class GraphStorePort(Protocol):
    def add_nodes(self, nodes: Iterable[GraphNode]) -> None: ...
    def add_edges(self, edges: Iterable[GraphEdge]) -> None: ...
    def export_graphml(self, path: Path) -> None: ...
    def export_jsonld(self, path: Path) -> None: ...
    def export_cypher(self, path: Path) -> None: ...
    def stats(self) -> dict[str, int | float]: ...


@runtime_checkable
class RelationalStorePort(Protocol):
    def save_extraction(self, chunk: Chunk, result: ExtractionResult) -> None: ...
    def save_chunks(self, chunks: Iterable[Chunk]) -> None: ...
    def save_document(self, document: Document) -> None: ...


@runtime_checkable
class CheckpointPort(Protocol):
    def is_complete(self, document_id: str, stage: str) -> bool: ...
    def mark_complete(self, document_id: str, stage: str, artifact_path: Path | None = None) -> None: ...
    def mark_failed(self, document_id: str, stage: str, error: str) -> None: ...
    def artifact_dir(self, document_id: str, stage: str) -> Path: ...
