"""Qdrant vector store adapter (embedded local mode by default)."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


class QdrantVectorStore:
    def __init__(self, url: str | None, api_key: str | None, local_path: Path) -> None:
        from qdrant_client import QdrantClient

        if url:
            self._client = QdrantClient(url=url, api_key=api_key or None)
        else:
            local_path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(local_path))

    def _ensure_collection(self, name: str, dim: int) -> None:
        from qdrant_client.http.models import Distance, VectorParams

        existing = {c.name for c in self._client.get_collections().collections}
        if name not in existing:
            self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    async def upsert(
        self,
        collection: str,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
        payloads: Sequence[dict[str, object]],
    ) -> None:
        from qdrant_client.http.models import PointStruct

        if not vectors:
            return
        self._ensure_collection(collection, len(vectors[0]))
        points = [
            PointStruct(id=i, vector=list(v), payload=p)
            for i, v, p in zip(ids, vectors, payloads, strict=True)
        ]
        self._client.upsert(collection_name=collection, points=points)

    async def search(
        self, collection: str, vector: Sequence[float], top_k: int = 10,
    ) -> list[tuple[str, float, dict[str, object]]]:
        hits = self._client.search(collection_name=collection, query_vector=list(vector), limit=top_k)
        return [(str(h.id), float(h.score), dict(h.payload or {})) for h in hits]
