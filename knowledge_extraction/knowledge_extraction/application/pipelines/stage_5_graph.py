"""Build a NetworkX property graph from extracted entities and relationships."""
from __future__ import annotations

from collections.abc import Iterable

from knowledge_extraction.application.ports import GraphStorePort
from knowledge_extraction.domain import ExtractionResult, GraphEdge, GraphNode


class GraphBuildPipeline:
    def __init__(self, store: GraphStorePort) -> None:
        self._store = store

    def build(self, results: Iterable[ExtractionResult]) -> dict[str, int | float]:
        seen_nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        for r in results:
            for e in r.entities:
                if e.id in seen_nodes:
                    continue
                seen_nodes[e.id] = GraphNode(
                    id=e.id, label=e.name, type=e.type,
                    properties={"confidence": e.confidence, "aliases": e.aliases},
                )
            for cl in r.claims:
                seen_nodes[cl.id] = GraphNode(
                    id=cl.id, label=cl.text[:80], type="Claim",
                    properties={"confidence": cl.confidence, "text": cl.text},
                )
            for rel in r.relationships:
                edges.append(GraphEdge(
                    id=rel.id, source_id=rel.source_id, target_id=rel.target_id,
                    type=rel.type, properties={"confidence": rel.confidence},
                ))
        self._store.add_nodes(seen_nodes.values())
        self._store.add_edges(edges)
        return self._store.stats()
