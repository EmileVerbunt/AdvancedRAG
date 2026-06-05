"""Tests for the Agentic Navigator (``nav``) backend: navigator tools + agent loop."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from knowledge_extraction.application.services.agentic_nav_agent import (
    AgenticNavAgent,
    AgenticNavOptions,
    _canonical_call,
    _safe_json,
)
from knowledge_extraction.application.services.document_navigator import (
    DocumentNavigator,
    nav_index_available,
)
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.config.settings import Settings

# --------------------------------------------------------------------------
# DB builders
# --------------------------------------------------------------------------


def _make_full_db(tmp_path: Path) -> Path:
    """A DB with document_id/markdown/image_path columns present."""
    db = tmp_path / "knowledge.db"
    con = sqlite3.connect(str(db))
    try:
        con.executescript(
            """
            CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, source_path TEXT,
                page_count INTEGER, created_at TEXT);
            CREATE TABLE chunks (id TEXT PRIMARY KEY, document_id TEXT, section_id TEXT,
                text TEXT, page_start INTEGER, page_end INTEGER, figure_refs_json TEXT,
                table_refs_json TEXT, token_estimate INTEGER);
            CREATE TABLE tables (id TEXT PRIMARY KEY, document_id TEXT, page INTEGER,
                page_end INTEGER, caption TEXT, markdown TEXT);
            CREATE TABLE figures (id TEXT PRIMARY KEY, document_id TEXT, page INTEGER,
                caption TEXT, image_path TEXT, interpretation_title TEXT,
                interpretation_chart_type TEXT, interpretation_confidence REAL);
            """
        )
        con.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            ("doc1", "AI Trends 2025", "/data/ai_trends.pdf", 50, "2025-01-01"),
        )
        con.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            ("doc2", "Climate Report", "/data/climate.pdf", 80, "2025-01-02"),
        )
        for cid, doc, page, text in [
            ("c1", "doc1", 1, "Model sizes grew significantly in 2024, driven by scaling laws."),
            ("c2", "doc1", 2, "Inference cost per token dropped 40% year-over-year in 2024."),
            ("c3", "doc1", 3, "Benchmark performance on MMLU improved by 15 points over 2023."),
            ("c4", "doc2", 1, "Global temperatures rose 1.2 degrees over pre-industrial levels."),
        ]:
            con.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, doc, None, text, page, page, None, None, len(text.split())),
            )
        con.execute(
            "INSERT INTO tables VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "doc1", 2, 2, "Inference cost by model", "| model | cost |\n| a | 1 |"),
        )
        con.execute(
            "INSERT INTO figures VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("f1", "doc1", 3, "MMLU over time", "/img/f1.png", "MMLU trend", "line", 0.9),
        )
        con.commit()
    finally:
        con.close()
    return db


def _make_minimal_db(tmp_path: Path) -> Path:
    """A reduced schema: tables/figures without document_id, markdown, or image_path."""
    db = tmp_path / "minimal.db"
    con = sqlite3.connect(str(db))
    try:
        con.executescript(
            """
            CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, source_path TEXT,
                page_count INTEGER, created_at TEXT);
            CREATE TABLE chunks (id TEXT PRIMARY KEY, document_id TEXT, section_id TEXT,
                text TEXT, page_start INTEGER, page_end INTEGER, figure_refs_json TEXT,
                table_refs_json TEXT, token_estimate INTEGER);
            CREATE TABLE tables (id TEXT PRIMARY KEY, caption TEXT, page INTEGER, page_end INTEGER);
            CREATE TABLE figures (id TEXT PRIMARY KEY, page INTEGER, caption TEXT,
                interpretation_title TEXT, interpretation_chart_type TEXT,
                interpretation_confidence REAL);
            """
        )
        con.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            ("doc1", "AI Trends 2025", "/data/ai_trends.pdf", 50, "2025-01-01"),
        )
        con.execute(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("c1", "doc1", "intro", "Scaling laws drove model growth in 2024.", 1, 1, None, None, 7),
        )
        con.execute("INSERT INTO tables VALUES (?, ?, ?, ?)", ("t1", "Cost table", 2, 2))
        con.execute(
            "INSERT INTO figures VALUES (?, ?, ?, ?, ?, ?)",
            ("f1", 3, "MMLU chart", "MMLU trend", "line", 0.9),
        )
        con.commit()
    finally:
        con.close()
    return db


def _write_doc_md(artifact_path: Path, source_path: str, body: str) -> None:
    stem = Path(source_path).stem
    doc_dir = artifact_path / stem
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "doc.md").write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------
# Stub LLM
# --------------------------------------------------------------------------


@dataclass
class _LLMResp:
    text: str
    input_tokens: int = 100
    output_tokens: int = 50
    latency_ms: int = 5


class _StubLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete_json(
        self, *, model: str, system: str, user: str, max_tokens: int = 4096, temperature: float = 0.0,
    ) -> _LLMResp:
        self.calls.append({"model": model, "system": system, "user": user})
        if not self._responses:
            return _LLMResp(text="{}")
        return _LLMResp(text=self._responses.pop(0))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.sqlite_path = _make_full_db(tmp_path)
    s.artifact_path = tmp_path / "artifacts"
    return s


@pytest.fixture
def prompts(settings: Settings) -> PromptRegistry:
    return PromptRegistry(settings.prompts_dir)


def _navigator(settings: Settings) -> DocumentNavigator:
    return DocumentNavigator(settings.sqlite_path, settings.artifact_path, max_chars=2000)


def _agent(settings: Settings, prompts: PromptRegistry, llm: _StubLLM) -> AgenticNavAgent:
    return AgenticNavAgent(settings, _navigator(settings), llm, prompts)


# --------------------------------------------------------------------------
# nav_index_available
# --------------------------------------------------------------------------


def test_nav_index_available_true(settings: Settings) -> None:
    assert nav_index_available(settings.sqlite_path) is True


def test_nav_index_available_false_when_missing(tmp_path: Path) -> None:
    assert nav_index_available(tmp_path / "nope.db") is False


def test_nav_index_available_false_when_no_documents_table(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE other (id TEXT)")
    con.commit()
    con.close()
    assert nav_index_available(db) is False


# --------------------------------------------------------------------------
# DocumentNavigator — catalog
# --------------------------------------------------------------------------


def test_catalog_lists_all_documents(settings: Settings) -> None:
    cat = _navigator(settings).catalog()
    ids = {m.document_id for m in cat}
    assert ids == {"doc1", "doc2"}
    doc1 = next(m for m in cat if m.document_id == "doc1")
    assert doc1.title == "AI Trends 2025"
    assert doc1.page_count == 50
    assert doc1.n_chunks == 3
    assert doc1.n_tables == 1
    assert doc1.n_figures == 1
    assert "Inference cost by model" in doc1.table_captions
    assert "MMLU over time" in doc1.figure_captions
    assert "Model sizes grew" in doc1.preview  # chunk-based preview (no doc.md)


def test_catalog_preview_uses_doc_md_when_present(settings: Settings) -> None:
    _write_doc_md(settings.artifact_path, "/data/ai_trends.pdf", "# Title\n\nOpening paragraph about scaling.")
    cat = _navigator(settings).catalog()
    doc1 = next(m for m in cat if m.document_id == "doc1")
    assert "Opening paragraph about scaling" in doc1.preview


# --------------------------------------------------------------------------
# DocumentNavigator — tools (chunk fallback, no doc.md)
# --------------------------------------------------------------------------


def test_open_document_chunk_outline_fallback(settings: Settings) -> None:
    out = _navigator(settings).open_document("doc1")
    assert "Outline" in out


def test_open_document_unknown_id(settings: Settings) -> None:
    assert "unknown document_id" in _navigator(settings).open_document("nope")


def test_search_document_chunk_fallback(settings: Settings) -> None:
    out = _navigator(settings).search_document("doc1", "inference cost per token")
    assert "40%" in out


def test_read_section_chunk_fallback(settings: Settings) -> None:
    out = _navigator(settings).read_section("doc1", "benchmark MMLU")
    assert "MMLU" in out


def test_get_table_by_id(settings: Settings) -> None:
    out = _navigator(settings).get_table("doc1", "t1")
    assert "Inference cost by model" in out
    assert "model" in out


def test_get_table_by_index(settings: Settings) -> None:
    out = _navigator(settings).get_table("doc1", "1")
    assert "Table t1" in out


def test_get_figure_by_id(settings: Settings) -> None:
    out = _navigator(settings).get_figure("doc1", "f1")
    assert "MMLU over time" in out
    assert "/img/f1.png" in out


def test_get_table_unknown_document(settings: Settings) -> None:
    assert "unknown document_id" in _navigator(settings).get_table("nope", "t1")


# --------------------------------------------------------------------------
# DocumentNavigator — doc.md heading slicing
# --------------------------------------------------------------------------


def test_read_section_uses_doc_md_headings(settings: Settings) -> None:
    body = (
        "# Intro\n\nThe intro text.\n\n"
        "## Inference Costs\n\nCosts fell sharply.\n\n"
        "### Details\n\nPer-token numbers here.\n\n"
        "## Benchmarks\n\nMMLU rose 15 points.\n"
    )
    _write_doc_md(settings.artifact_path, "/data/ai_trends.pdf", body)
    out = _navigator(settings).read_section("doc1", "inference costs")
    # The "## Inference Costs" section keeps its "### Details" subsection (level > 2)
    # but stops before the next level-2 "## Benchmarks" heading.
    assert "Costs fell sharply" in out
    assert "Per-token numbers here" in out
    assert "MMLU rose 15 points" not in out


def test_search_document_uses_doc_md(settings: Settings) -> None:
    body = "# A\n\nApples and oranges.\n\n# B\n\nInference latency dropped in 2024.\n"
    _write_doc_md(settings.artifact_path, "/data/ai_trends.pdf", body)
    out = _navigator(settings).search_document("doc1", "inference latency")
    assert "latency dropped" in out


# --------------------------------------------------------------------------
# DocumentNavigator — schema tolerance (minimal DB)
# --------------------------------------------------------------------------


def test_navigator_tolerates_minimal_schema(tmp_path: Path) -> None:
    nav = DocumentNavigator(_make_minimal_db(tmp_path), tmp_path / "artifacts")
    cat = nav.catalog()
    assert len(cat) == 1
    assert cat[0].n_tables == 1
    assert cat[0].n_figures == 1
    assert "Cost table" in cat[0].table_captions
    assert "Cost table" in nav.get_table("doc1", "t1")
    assert "MMLU chart" in nav.get_figure("doc1", "1")
    assert "scaling laws" in nav.search_document("doc1", "scaling laws model").lower()


# --------------------------------------------------------------------------
# AgenticNavAgent — full loop
# --------------------------------------------------------------------------


def test_full_navigation_loop(settings: Settings, prompts: PromptRegistry) -> None:
    route = '{"document_ids": ["doc1"], "reasoning": "AI trends doc is relevant"}'
    step1 = '{"thought": "search", "tool": "search_document", "args": {"document_id": "doc1", "query": "inference cost"}}'
    step2 = '{"thought": "done", "tool": "finish", "args": {}}'
    synth = '{"answer": "Inference cost dropped 40% in 2024 [doc: doc1]."}'
    llm = _StubLLM([route, step1, step2, synth])

    agent = _agent(settings, prompts, llm)
    result = agent.ask("How did inference cost change?", options=AgenticNavOptions(max_steps=4))

    assert result.selected_documents == ["doc1"]
    assert result.steps == 1  # finish is not recorded as a transcript step
    assert "40%" in result.answer
    assert result.tokens.total > 0
    assert result.transcript[0].tool == "search_document"
    assert "40%" in result.transcript[0].observation


def test_route_falls_back_to_first_docs_on_invalid_ids(
    settings: Settings, prompts: PromptRegistry
) -> None:
    route = '{"document_ids": ["does-not-exist"], "reasoning": "guess"}'
    step1 = '{"tool": "finish", "args": {}}'
    synth = '{"answer": "No specific evidence."}'
    llm = _StubLLM([route, step1, synth])

    agent = _agent(settings, prompts, llm)
    result = agent.ask("anything", options=AgenticNavOptions(max_docs=1, max_steps=2))
    # Invalid id dropped → fall back to first cataloged doc.
    assert result.selected_documents == ["doc1"]


def test_invalid_tool_streak_breaks_loop(settings: Settings, prompts: PromptRegistry) -> None:
    route = '{"document_ids": ["doc1"]}'
    bad = '{"tool": "frobnicate", "args": {}}'
    synth = '{"answer": "stopped"}'
    # Three invalid tool calls should break before exhausting max_steps=10.
    llm = _StubLLM([route, bad, bad, bad, bad, bad, synth])

    agent = _agent(settings, prompts, llm)
    result = agent.ask("q", options=AgenticNavOptions(max_steps=10))
    assert result.steps == 3  # broke after _MAX_INVALID_STREAK invalid actions
    assert all(s.observation.startswith("error: unknown tool") for s in result.transcript)


def test_empty_step_response_gets_no_tool_nudge(
    settings: Settings, prompts: PromptRegistry
) -> None:
    # A reasoning model that exhausts its token budget returns empty content ("{}"),
    # which parses to no tool. The loop should nudge with a distinct message and
    # break after the invalid streak rather than silently spinning.
    route = '{"document_ids": ["doc1"]}'
    empty = "{}"
    synth = '{"answer": "stopped"}'
    llm = _StubLLM([route, empty, empty, empty, empty, synth])

    agent = _agent(settings, prompts, llm)
    result = agent.ask("q", options=AgenticNavOptions(max_steps=10))
    assert result.steps == 3
    assert all(s.tool == "(none)" for s in result.transcript)
    assert all(s.observation.startswith("error: no tool was returned") for s in result.transcript)


def test_unknown_document_id_is_rejected(settings: Settings, prompts: PromptRegistry) -> None:
    route = '{"document_ids": ["doc1"]}'
    bad_doc = '{"tool": "read_section", "args": {"document_id": "doc2", "query": "x"}}'
    finish = '{"tool": "finish", "args": {}}'
    synth = '{"answer": "ok"}'
    llm = _StubLLM([route, bad_doc, finish, synth])

    agent = _agent(settings, prompts, llm)
    result = agent.ask("q", options=AgenticNavOptions(max_docs=1, max_steps=4))
    assert "not one of the selected documents" in result.transcript[0].observation


def test_repeated_call_is_deduplicated(settings: Settings, prompts: PromptRegistry) -> None:
    route = '{"document_ids": ["doc1"]}'
    same = '{"tool": "search_document", "args": {"document_id": "doc1", "query": "cost"}}'
    finish = '{"tool": "finish", "args": {}}'
    synth = '{"answer": "ok"}'
    llm = _StubLLM([route, same, same, finish, synth])

    agent = _agent(settings, prompts, llm)
    result = agent.ask("q", options=AgenticNavOptions(max_steps=5))
    assert result.transcript[1].observation.startswith("(already retrieved")


def test_to_dict_is_json_serializable(settings: Settings, prompts: PromptRegistry) -> None:
    route = '{"document_ids": ["doc1"]}'
    step1 = '{"tool": "get_figure", "args": {"document_id": "doc1", "figure": "f1"}}'
    finish = '{"tool": "finish", "args": {}}'
    synth = '{"answer": "answer [doc: doc1]."}'
    llm = _StubLLM([route, step1, finish, synth])

    agent = _agent(settings, prompts, llm)
    result = agent.ask("q", options=AgenticNavOptions(max_steps=4))
    json.dumps(result.to_dict(), ensure_ascii=True)  # must not raise


def test_token_accounting_across_phases(settings: Settings, prompts: PromptRegistry) -> None:
    route = '{"document_ids": ["doc1"]}'
    step1 = '{"tool": "search_document", "args": {"document_id": "doc1", "query": "cost"}}'
    finish = '{"tool": "finish", "args": {}}'
    synth = '{"answer": "ok"}'
    llm = _StubLLM([route, step1, finish, synth])

    agent = _agent(settings, prompts, llm)
    result = agent.ask("q", options=AgenticNavOptions(max_steps=4))
    t = result.tokens
    assert t.route_input == 100 and t.route_output == 50
    # Two navigate calls (step1 + finish) at 100/50 each.
    assert t.nav_input == 200 and t.nav_output == 100
    assert t.synth_input == 100 and t.synth_output == 50


def test_uses_router_model_from_settings(settings: Settings, prompts: PromptRegistry) -> None:
    settings.azure_openai_reasoning_model = "custom-router-x"
    llm = _StubLLM(['{"document_ids": ["doc1"]}', '{"tool": "finish", "args": {}}', '{"answer": "ok"}'])
    agent = _agent(settings, prompts, llm)
    agent.ask("q", options=AgenticNavOptions(max_steps=2))
    assert llm.calls[0]["model"] == "custom-router-x"


# --------------------------------------------------------------------------
# Unit helpers
# --------------------------------------------------------------------------


def test_safe_json() -> None:
    assert _safe_json('{"a": 1}') == {"a": 1}
    assert _safe_json("not json") == {}
    assert _safe_json("[1, 2]") == {}


def test_canonical_call_is_order_and_case_insensitive() -> None:
    a = _canonical_call("search_document", {"document_id": "doc1", "query": "Cost"})
    b = _canonical_call("search_document", {"query": "cost", "document_id": "doc1"})
    assert a == b
