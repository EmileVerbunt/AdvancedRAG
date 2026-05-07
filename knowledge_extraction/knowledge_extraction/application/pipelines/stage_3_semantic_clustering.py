"""Embedding-based clustering of discovered entities for ontology proposal."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from knowledge_extraction.application.ports import EmbeddingPort


@dataclass(slots=True)
class ClusterSummary:
    id: int
    members: list[str] = field(default_factory=list)
    centroid_norm: float = 0.0
    summary: str = ""


class SemanticClusterer:
    """Simple cosine-similarity agglomerative clustering for entity names."""

    def __init__(self, embeddings: EmbeddingPort, model: str, threshold: float = 0.78) -> None:
        self._embeddings = embeddings
        self._model = model
        self._threshold = threshold

    async def cluster(self, names: list[str]) -> list[ClusterSummary]:
        names = [n for n in dict.fromkeys(names) if n.strip()]
        if not names:
            return []
        vectors = await self._embeddings.embed(names, model=self._model)
        clusters: list[list[int]] = []
        centroids: list[list[float]] = []
        for idx, vec in enumerate(vectors):
            assigned = False
            for ci, c in enumerate(centroids):
                if _cosine(vec, c) >= self._threshold:
                    clusters[ci].append(idx)
                    centroids[ci] = _avg(centroids[ci], vec, len(clusters[ci]))
                    assigned = True
                    break
            if not assigned:
                clusters.append([idx])
                centroids.append(list(vec))
        return [
            ClusterSummary(id=i, members=[names[j] for j in c], centroid_norm=_norm(centroids[i]))
            for i, c in enumerate(clusters)
        ]


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=True))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(x * x for x in b)) or 1.0
    return num / (da * db)


def _avg(centroid: list[float], v: list[float], n: int) -> list[float]:
    return [(c * (n - 1) + x) / n for c, x in zip(centroid, v, strict=True)]


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))
