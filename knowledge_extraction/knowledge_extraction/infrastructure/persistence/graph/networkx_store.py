"""NetworkX-backed property graph with multi-format exports."""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import networkx as nx

from knowledge_extraction.domain import GraphEdge, GraphNode


class NetworkXGraphStore:
    def __init__(self) -> None:
        self.g: nx.MultiDiGraph = nx.MultiDiGraph()

    def add_nodes(self, nodes: Iterable[GraphNode]) -> None:
        for n in nodes:
            self.g.add_node(n.id, label=n.label, type=n.type, **n.properties)

    def add_edges(self, edges: Iterable[GraphEdge]) -> None:
        for e in edges:
            self.g.add_edge(e.source_id, e.target_id, key=e.id, type=e.type, **e.properties)

    # Exports

    def export_graphml(self, path: Path) -> None:
        from knowledge_extraction.infrastructure.telemetry.observability import wide_event

        path.parent.mkdir(parents=True, exist_ok=True)
        with wide_event("graph.export", format="graphml", path=str(path),
                        nodes=self.g.number_of_nodes(), edges=self.g.number_of_edges()) as ev:
            flat = nx.MultiDiGraph()
            for n, data in self.g.nodes(data=True):
                flat.add_node(n, **{k: _as_str(v) for k, v in data.items()})
            for u, v, data in self.g.edges(data=True):
                flat.add_edge(u, v, **{k: _as_str(val) for k, val in data.items()})
            nx.write_graphml(flat, path)
            ev["bytes"] = path.stat().st_size

    def export_jsonld(self, path: Path) -> None:
        from knowledge_extraction.infrastructure.telemetry.observability import wide_event

        path.parent.mkdir(parents=True, exist_ok=True)
        with wide_event("graph.export", format="jsonld", path=str(path),
                        nodes=self.g.number_of_nodes(), edges=self.g.number_of_edges()) as ev:
            doc = {
                "@context": {"@vocab": "https://example.org/ke#"},
                "@graph": [
                    {"@id": n, "@type": data.get("type", "Node"), **{k: v for k, v in data.items() if k != "type"}}
                    for n, data in self.g.nodes(data=True)
                ] + [
                    {"@type": data.get("type", "Edge"), "source": u, "target": v,
                     **{k: val for k, val in data.items() if k != "type"}}
                    for u, v, data in self.g.edges(data=True)
                ],
            }
            path.write_text(json.dumps(doc, default=str, indent=2), encoding="utf-8")
            ev["bytes"] = path.stat().st_size

    def export_cypher(self, path: Path) -> None:
        from knowledge_extraction.infrastructure.telemetry.observability import wide_event

        path.parent.mkdir(parents=True, exist_ok=True)
        with wide_event("graph.export", format="cypher", path=str(path),
                        nodes=self.g.number_of_nodes(), edges=self.g.number_of_edges()) as ev:
            lines: list[str] = []
            for n, data in self.g.nodes(data=True):
                label = data.get("type", "Node")
                props = _cypher_props({k: v for k, v in data.items() if k != "type"} | {"id": n})
                lines.append(f"MERGE (:`{label}` {props});")
            for u, v, data in self.g.edges(data=True):
                rel = data.get("type", "REL")
                props = _cypher_props({k: val for k, val in data.items() if k != "type"})
                lines.append(
                    f"MATCH (a {{id: {json.dumps(u)}}}),(b {{id: {json.dumps(v)}}}) "
                    f"MERGE (a)-[:`{rel}` {props}]->(b);"
                )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            ev["bytes"] = path.stat().st_size

    def stats(self) -> dict[str, int | float]:
        n = self.g.number_of_nodes()
        e = self.g.number_of_edges()
        avg_deg = (sum(d for _, d in self.g.degree()) / n) if n else 0.0
        try:
            comps = nx.number_weakly_connected_components(self.g)
        except Exception:
            comps = 0
        return {"nodes": n, "edges": e, "avg_degree": round(avg_deg, 3), "components": comps}


def _as_str(v: object) -> str:
    if isinstance(v, str | int | float | bool):
        return v if isinstance(v, str) else str(v)
    return json.dumps(v, default=str)


def _cypher_props(props: dict[str, object]) -> str:
    if not props:
        return ""
    body = ", ".join(f"{k}: {json.dumps(v, default=str)}" for k, v in props.items() if v is not None)
    return "{" + body + "}"
