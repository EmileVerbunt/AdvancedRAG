"""Tests for ChunkRetriever (BM25 over the chunks table)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from knowledge_extraction.application.services.chunk_retriever import (
    ChunkHit,
    ChunkRetriever,
)


def _make_db(tmp_path: Path, chunks: list[dict]) -> Path:
    """Build a minimal SQLite store with documents + chunks tables."""
    db = tmp_path / "knowledge.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, source_path TEXT, page_count INTEGER, created_at TEXT)"
        )
        con.execute(
            """CREATE TABLE chunks (
                id TEXT PRIMARY KEY,
                document_id TEXT,
                section_id TEXT,
                text TEXT,
                page_start INTEGER,
                page_end INTEGER,
                figure_refs_json TEXT,
                table_refs_json TEXT,
                token_estimate INTEGER
            )"""
        )
        con.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            ("doc1", "AI Index Report", "/tmp/x.pdf", 100, "2024-01-01"),
        )
        for c in chunks:
            con.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    c["id"], c.get("document_id", "doc1"), c.get("section_id"),
                    c["text"], c["page_start"], c["page_end"],
                    None, None, len(c["text"].split()),
                ),
            )
        con.commit()
    finally:
        con.close()
    return db


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    return _make_db(
        tmp_path,
        [
            {"id": "c1", "text": "Anthropic released Claude Opus in March 2024.", "page_start": 1, "page_end": 1},
            {"id": "c2", "text": "OpenAI launched GPT-4o in May 2024 with multimodal support.", "page_start": 2, "page_end": 2},
            {"id": "c3", "text": "Industry investment in AI has surged year over year.", "page_start": 3, "page_end": 3},
            {"id": "c4", "text": "Total funding for generative AI startups reached new highs.", "page_start": 4, "page_end": 4},
        ],
    )


def test_search_returns_chunks_ranked_by_term_overlap(populated_db: Path) -> None:
    r = ChunkRetriever(populated_db)
    hits = r.search("When was Claude Opus released?", top_k=3)
    assert hits, "expected at least one hit"
    assert hits[0].id == "c1"
    assert all(isinstance(h, ChunkHit) for h in hits)


def test_search_includes_document_metadata(populated_db: Path) -> None:
    r = ChunkRetriever(populated_db)
    hits = r.search("Claude Opus", top_k=1)
    assert hits[0].document_id == "doc1"
    assert hits[0].document_title == "AI Index Report"
    assert hits[0].page_start == 1


def test_search_returns_empty_when_no_terms_match(populated_db: Path) -> None:
    r = ChunkRetriever(populated_db)
    assert r.search("xyzzy plover quokka", top_k=3) == []


def test_search_returns_empty_when_db_missing(tmp_path: Path) -> None:
    r = ChunkRetriever(tmp_path / "does-not-exist.db")
    assert r.search("anything", top_k=3) == []


def test_search_respects_top_k_cap(populated_db: Path) -> None:
    r = ChunkRetriever(populated_db)
    hits = r.search("AI", top_k=2)
    assert len(hits) <= 2


def test_search_drops_stopwords_and_short_tokens(populated_db: Path) -> None:
    r = ChunkRetriever(populated_db)
    # 'the', 'and', 'was' are all stopwords/<3 chars; only 'claude' should drive scoring
    hits = r.search("the was and Claude", top_k=2)
    assert hits and hits[0].id == "c1"


def test_neighbors_returns_adjacent_chunks_in_same_doc(populated_db: Path) -> None:
    r = ChunkRetriever(populated_db)
    neighbours = r.neighbors("c2", window=2)
    ids = {n.id for n in neighbours}
    # c1 (page 1) and c3 (page 3) are closest to c2 (page 2); c2 itself excluded
    assert "c2" not in ids
    assert "c1" in ids or "c3" in ids


def test_neighbors_returns_empty_for_unknown_chunk(populated_db: Path) -> None:
    r = ChunkRetriever(populated_db)
    assert r.neighbors("nonexistent", window=2) == []


def test_neighbors_returns_empty_for_zero_window(populated_db: Path) -> None:
    r = ChunkRetriever(populated_db)
    assert r.neighbors("c1", window=0) == []
