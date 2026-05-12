from __future__ import annotations

import sqlite3
from pathlib import Path

import networkx as nx

from knowledge_extraction.application.services.graphrag_agent import MiniGraphRagAgent


def _seed_db(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE claims (
            id TEXT PRIMARY KEY,
            text TEXT,
            confidence REAL,
            supporting_figure_id TEXT,
            supporting_table_id TEXT
        );
        CREATE TABLE tables (
            id TEXT PRIMARY KEY,
            caption TEXT,
            page INTEGER,
            page_end INTEGER
        );
        CREATE TABLE table_cells (
            table_id TEXT,
            row_index INTEGER,
            column_index INTEGER,
            text TEXT
        );
        CREATE TABLE figures (
            id TEXT PRIMARY KEY,
            page INTEGER,
            caption TEXT,
            interpretation_title TEXT,
            interpretation_chart_type TEXT,
            interpretation_confidence REAL
        );
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            name TEXT,
            type TEXT,
            confidence REAL
        );
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY,
            document_id TEXT,
            section_id TEXT,
            text TEXT,
            page_start INTEGER,
            page_end INTEGER,
            figure_refs_json TEXT,
            table_refs_json TEXT,
            token_estimate INTEGER
        );
        """
    )
    cur.execute(
        "INSERT INTO claims VALUES (?, ?, ?, ?, ?)",
        ("c1", "GPT-4 outperforms baseline on benchmark table.", 0.9, None, "t1"),
    )
    cur.execute("INSERT INTO tables VALUES (?, ?, ?, ?)", ("t1", "Model performance results", 88, 88))
    cur.executemany(
        "INSERT INTO table_cells VALUES (?, ?, ?, ?)",
        [
            ("t1", 0, 0, "Model"),
            ("t1", 0, 1, "Score"),
            ("t1", 1, 0, "GPT-4"),
            ("t1", 1, 1, "92.1"),
        ],
    )
    cur.execute(
        "INSERT INTO entities VALUES (?, ?, ?, ?)",
        ("e1", "GPT-4", "Model", 0.98),
    )
    cur.execute(
        "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "k1",
            "d1",
            "s1",
            "SuperGLUE benchmark performance improved and now exceeds human baseline.",
            95,
            95,
            "[]",
            "[\"t1\"]",
            42,
        ),
    )
    con.commit()
    con.close()


def _seed_graph(graph_dir: Path) -> None:
    graph_dir.mkdir(parents=True, exist_ok=True)
    g = nx.Graph()
    g.add_node("c1", label="Claim c1", type="Claim")
    g.add_node("t1", label="Table t1", type="Table")
    g.add_edge("c1", "t1", type="SUPPORTED_BY_TABLE")
    nx.write_graphml(g, graph_dir / "sample.graphml")


def test_mini_graphrag_agent_returns_hybrid_hits_and_graph_context(tmp_path: Path) -> None:
    db_path = tmp_path / "ke.db"
    graph_dir = tmp_path / "graph"
    _seed_db(db_path)
    _seed_graph(graph_dir)

    agent = MiniGraphRagAgent(db_path, graph_dir)
    result = agent.ask("Which table has GPT-4 performance score?", top_k=5, include_graph=True)

    assert result.hits
    kinds = {h.kind for h in result.hits}
    assert "table" in kinds or "claim" in kinds
    assert any(h.id in {"c1", "t1"} for h in result.hits)
    assert result.graph_context
    node_ids = {ctx.node_id for ctx in result.graph_context}
    assert "t1" in node_ids or "c1" in node_ids


def test_mini_graphrag_agent_uses_markdown_fallback_for_typo_query(tmp_path: Path) -> None:
    db_path = tmp_path / "ke.db"
    graph_dir = tmp_path / "graph"
    md_path = tmp_path / "doc.md"
    _seed_db(db_path)
    _seed_graph(graph_dir)
    md_path.write_text(
        "\n".join(
            [
                "<tr>",
                "<td>Aug 12, 2024</td>",
                "<td>Falcon Mamba</td>",
                "<td>A powerful new 7B parameter model built on the Mamba State Space Language Model (SSLM).</td>",
                "</tr>",
            ]
        ),
        encoding="utf-8",
    )

    agent = MiniGraphRagAgent(db_path, graph_dir, source_markdown=md_path)
    result = agent.ask("when did Falcom Mambo come out", top_k=5, include_graph=False)

    assert result.hits
    assert result.hits[0].kind == "markdown"
    assert "Aug 12, 2024" in result.hits[0].text
