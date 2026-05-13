"""Pure chunk lookup over the SQLite knowledge store.

Retrieves the most lexically relevant chunks for a question — and *only* chunks.
No claims, no entities, no relationships. Used as the foundation for the lazy
GraphRAG agent (which must operate as if no knowledge graph exists) and shared
with :class:`MiniGraphRagAgent` for its chunk-candidate generation.

The score is a BM25-flavoured term-overlap score with a small bonus for chunks
whose retrieval text already contains the most-frequent query terms (so the
trimming function can anchor on a hit). It is deliberately simple — anything
fancier (vector search, FAISS) would couple this layer to model choices.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
        "into", "about", "what", "when", "where", "which", "have", "has", "had",
        "not", "you", "your", "our", "their", "can", "could", "should", "would",
        "how", "why", "who", "whom", "whose", "then", "than", "also", "using",
        "did", "come", "out",
    }
)


@dataclass(slots=True)
class ChunkHit:
    """One lexically-scored chunk plus the metadata a downstream agent needs.

    ``document_title`` and ``section_id`` come from a one-shot join with the
    ``documents`` table so the LLM can cite the source without a second query.
    """
    id: str
    text: str
    score: float
    page_start: int
    page_end: int
    document_id: str
    document_title: str
    section_id: str | None
    figure_refs_json: str | None
    table_refs_json: str | None


class ChunkRetriever:
    """Single-purpose BM25 chunk retriever over the SQLite knowledge store.

    The constructor takes a path so the retriever is cheap to build and can be
    instantiated per-request without holding a long-lived connection (matches
    the existing :class:`MiniGraphRagAgent` pattern).
    """

    def __init__(self, sqlite_path: Path) -> None:
        self._sqlite_path = sqlite_path

    def search(self, question: str, *, top_k: int = 20) -> list[ChunkHit]:
        """Return up to ``top_k`` chunks most lexically relevant to ``question``."""
        terms = _terms(question)
        if not terms or not self._sqlite_path.exists():
            return []
        con = sqlite3.connect(str(self._sqlite_path))
        con.row_factory = sqlite3.Row
        try:
            return self._score_chunks(con, terms, top_k=top_k)
        finally:
            con.close()

    def neighbors(self, chunk_id: str, *, window: int = 1) -> list[ChunkHit]:
        """Return the chunks immediately preceding and following ``chunk_id``.

        Same document, ordered by ``page_start`` then ``id``. Used by the lazy
        agent to give the LLM a small amount of surrounding context so the
        ad-hoc subgraph it extracts is not anchored on a single isolated span.
        """
        if window <= 0 or not self._sqlite_path.exists():
            return []
        con = sqlite3.connect(str(self._sqlite_path))
        con.row_factory = sqlite3.Row
        try:
            target = con.execute(
                "SELECT document_id, page_start FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            if target is None:
                return []
            rows = con.execute(
                """
                SELECT c.id, c.text, c.page_start, c.page_end, c.document_id,
                       c.section_id, c.figure_refs_json, c.table_refs_json,
                       COALESCE(d.title, '') AS document_title
                FROM chunks c
                LEFT JOIN documents d ON d.id = c.document_id
                WHERE c.document_id = ? AND c.id != ?
                ORDER BY ABS(c.page_start - ?), c.page_start, c.id
                LIMIT ?
                """,
                (target["document_id"], chunk_id, target["page_start"], 2 * window),
            ).fetchall()
            return [_row_to_hit(r, score=0.0) for r in rows]
        finally:
            con.close()

    def _score_chunks(
        self, con: sqlite3.Connection, terms: set[str], *, top_k: int
    ) -> list[ChunkHit]:
        rows = con.execute(
            """
            SELECT c.id, c.text, c.page_start, c.page_end, c.document_id,
                   c.section_id, c.figure_refs_json, c.table_refs_json,
                   COALESCE(d.title, '') AS document_title
            FROM chunks c
            LEFT JOIN documents d ON d.id = c.document_id
            """
        ).fetchall()
        scored: list[ChunkHit] = []
        for r in rows:
            score = _score(terms, str(r["text"] or ""))
            if score <= 0:
                continue
            scored.append(_row_to_hit(r, score=score))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[: max(1, top_k)]


def _row_to_hit(row: sqlite3.Row, *, score: float) -> ChunkHit:
    return ChunkHit(
        id=str(row["id"]),
        text=str(row["text"] or ""),
        score=score,
        page_start=int(row["page_start"]),
        page_end=int(row["page_end"]),
        document_id=str(row["document_id"]),
        document_title=str(row["document_title"] or ""),
        section_id=row["section_id"],
        figure_refs_json=row["figure_refs_json"],
        table_refs_json=row["table_refs_json"],
    )


def _terms(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _score(query_terms: set[str], text: str) -> float:
    if not text:
        return 0.0
    text_terms = _terms(text)
    if not text_terms:
        return 0.0
    overlap = float(len(query_terms & text_terms))
    if overlap <= 0:
        return 0.0
    return (overlap / max(1, len(query_terms))) + min(overlap, 3) * 0.08 + 0.45


__all__ = ["ChunkHit", "ChunkRetriever"]
