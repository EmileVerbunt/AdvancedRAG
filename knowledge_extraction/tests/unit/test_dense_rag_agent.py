"""Tests for DenseRagAgent — plain retrieve-and-read RAG (evidence + synthesis)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from knowledge_extraction.application.services.chunk_retriever import ChunkRetriever
from knowledge_extraction.application.services.dense_rag_agent import (
    DenseRagAgent,
    dense_index_available,
)
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.config.settings import Settings

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
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
            ("doc1", "AI Index 2025", "/tmp/x.pdf", 100, "2024-01-01"),
        )
        for cid, page, text in [
            ("c1", 1, "Anthropic released Claude Opus on March 4 2024."),
            ("c2", 2, "OpenAI announced GPT-4o with multimodal capabilities in May 2024."),
            ("c3", 3, "Industry investment in AI surged in 2024."),
        ]:
            con.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, "doc1", None, text, page, page, None, None, len(text.split())),
            )
        con.commit()
    finally:
        con.close()
    return db


@dataclass
class _LLMResp:
    text: str
    input_tokens: int = 100
    output_tokens: int = 50
    latency_ms: int = 5


class _StubLLM:
    """Records each call and replays queued responses (one synthesis call per ask)."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete_json(
        self, *, model: str, system: str, user: str, max_tokens: int = 4096, temperature: float = 0.0,
    ) -> _LLMResp:
        self.calls.append(
            {"model": model, "max_tokens": max_tokens, "system_len": len(system), "user_len": len(user)}
        )
        if not self._responses:
            return _LLMResp(text="{}")
        return _LLMResp(text=self._responses.pop(0))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.sqlite_path = _make_db(tmp_path)
    return s


@pytest.fixture
def prompts(settings: Settings) -> PromptRegistry:
    return PromptRegistry(settings.prompts_dir)


# --------------------------------------------------------------------------
# Agent behaviour
# --------------------------------------------------------------------------


def test_ask_makes_single_synthesis_call_and_returns_answer(
    settings: Settings, prompts: PromptRegistry,
) -> None:
    synth_response = '{"answer": "Claude Opus was released on March 4 2024 [c1]."}'
    llm = _StubLLM([synth_response])
    agent = DenseRagAgent(settings, ChunkRetriever(settings.sqlite_path), llm, prompts)

    result = agent.ask("When was Claude Opus released?", top_k_chunks=3)

    assert len(llm.calls) == 1, "dense RAG performs exactly one synthesis call (no subgraph step)"
    assert "Claude Opus" in result.answer and "[c1]" in result.answer
    assert result.chunks and any(c.id == "c1" for c in result.chunks)
    assert result.tokens.total > 0
    assert result.duration_ms >= 0


def test_ask_falls_back_to_raw_text_when_synth_returns_plain_prose(
    settings: Settings, prompts: PromptRegistry,
) -> None:
    """The synthesis prompt asks for prose; the LLM may not wrap it in JSON."""
    llm = _StubLLM(["Claude Opus was released on March 4 2024 [c1]."])
    agent = DenseRagAgent(settings, ChunkRetriever(settings.sqlite_path), llm, prompts)

    result = agent.ask("Claude Opus release", top_k_chunks=3)
    assert "Claude Opus" in result.answer


def test_ask_returns_empty_answer_when_no_chunks_match(
    settings: Settings, prompts: PromptRegistry,
) -> None:
    llm = _StubLLM([])
    agent = DenseRagAgent(settings, ChunkRetriever(settings.sqlite_path), llm, prompts)
    result = agent.ask("xyzzy plover quokka", top_k_chunks=3)
    assert result.chunks == []
    assert "No relevant chunks" in result.answer
    assert llm.calls == [], "no LLM calls when retrieval yields nothing"


def test_ask_to_dict_round_trip_is_json_serialisable(
    settings: Settings, prompts: PromptRegistry,
) -> None:
    import json
    llm = _StubLLM(['{"answer": "ok"}'])
    agent = DenseRagAgent(settings, ChunkRetriever(settings.sqlite_path), llm, prompts)
    result = agent.ask("Claude?", top_k_chunks=3)
    payload = result.to_dict()
    json.dumps(payload, ensure_ascii=True)  # must not raise


def test_ask_uses_extraction_model_from_settings(
    settings: Settings, prompts: PromptRegistry,
) -> None:
    settings.azure_openai_extraction_model = "custom-model-x"
    llm = _StubLLM(['{"answer": "ok"}'])
    agent = DenseRagAgent(settings, ChunkRetriever(settings.sqlite_path), llm, prompts)
    agent.ask("Claude?", top_k_chunks=3)
    assert llm.calls[0]["model"] == "custom-model-x"


# --------------------------------------------------------------------------
# dense_index_available
# --------------------------------------------------------------------------


def test_dense_index_available_true_when_chunks_table_populated(settings: Settings) -> None:
    assert dense_index_available(settings) is True


def test_dense_index_available_false_when_db_missing(tmp_path: Path) -> None:
    s = Settings()
    s.sqlite_path = tmp_path / "missing.db"
    assert dense_index_available(s) is False


def test_dense_index_available_false_when_chunks_table_empty(tmp_path: Path) -> None:
    db = tmp_path / "empty_chunks.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE chunks (id TEXT)")
    con.commit()
    con.close()
    s = Settings()
    s.sqlite_path = db
    assert dense_index_available(s) is False
