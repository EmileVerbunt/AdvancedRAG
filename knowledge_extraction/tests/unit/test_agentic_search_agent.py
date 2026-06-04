"""Tests for AgenticSearchAgent — bounded agentic retrieval loop."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from knowledge_extraction.application.services.agentic_search_agent import (
    AgenticSearchAgent,
    AgenticSearchOptions,
    _merge_evidence,
    _safe_json,
    agentic_index_available,
)
from knowledge_extraction.application.services.graphrag_agent import MiniGraphRagAgent
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.config.settings import Settings

# --------------------------------------------------------------------------
# Helpers — in-memory SQLite DB
# --------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "knowledge.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE documents "
            "(id TEXT PRIMARY KEY, title TEXT, source_path TEXT, page_count INTEGER, created_at TEXT)"
        )
        con.executescript(
            """
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
            """
        )
        con.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            ("doc1", "AI Trends 2025", "/tmp/x.pdf", 50, "2025-01-01"),
        )
        for cid, page, text in [
            ("c1", 1, "Model sizes grew significantly in 2024, driven by scaling laws."),
            ("c2", 2, "Inference cost per token dropped 40% year-over-year in 2024."),
            ("c3", 3, "Benchmark performance on MMLU improved by 15 points compared to 2023."),
        ]:
            con.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, "doc1", None, text, page, page, None, None, len(text.split())),
            )
        con.commit()
    finally:
        con.close()
    return db


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
    """Pops responses in order; returns empty dict JSON when exhausted."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete_json(
        self, *, model: str, system: str, user: str, max_tokens: int = 4096, temperature: float = 0.0,
    ) -> _LLMResp:
        self.calls.append({"model": model, "max_tokens": max_tokens})
        if not self._responses:
            return _LLMResp(text="{}")
        return _LLMResp(text=self._responses.pop(0))


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.sqlite_path = _make_db(tmp_path)
    return s


@pytest.fixture
def prompts(settings: Settings) -> PromptRegistry:
    return PromptRegistry(settings.prompts_dir)


def _make_agent(settings: Settings, prompts: PromptRegistry, llm: _StubLLM) -> AgenticSearchAgent:
    mini_agent = MiniGraphRagAgent(settings.sqlite_path, settings.graph_storage_path)
    return AgenticSearchAgent(settings, mini_agent, llm, prompts)


# --------------------------------------------------------------------------
# Agent behaviour
# --------------------------------------------------------------------------


def test_one_round_successful_answer(settings: Settings, prompts: PromptRegistry) -> None:
    plan_resp = '{"subquestions": ["model size trends", "benchmark improvements"]}'
    critic_resp = '{"sufficient": true, "confidence": 0.9, "missing_information": [], "follow_up_queries": []}'
    synth_resp = '{"answer": "Model sizes grew and benchmarks improved in 2024 [C1] [C3]."}'
    llm = _StubLLM([plan_resp, critic_resp, synth_resp])

    agent = _make_agent(settings, prompts, llm)
    result = agent.ask("What are the key AI trends?", options=AgenticSearchOptions(max_rounds=1))

    assert len(llm.calls) == 3, "expect plan + critic + synthesis calls"
    assert "grew" in result.answer or "improved" in result.answer
    assert result.rounds == 1
    assert len(result.plan.subquestions) == 2
    assert result.critique.sufficient is True
    assert result.tokens.total > 0


def test_two_round_follow_up(settings: Settings, prompts: PromptRegistry) -> None:
    plan_resp = '{"subquestions": ["inference cost"]}'
    critic_r1 = '{"sufficient": false, "confidence": 0.4, "missing_information": ["benchmark data"], "follow_up_queries": ["MMLU benchmark 2024"]}'
    critic_r2 = '{"sufficient": true, "confidence": 0.85, "missing_information": [], "follow_up_queries": []}'
    synth_resp = '{"answer": "Inference costs dropped and benchmarks improved [C2] [C3]."}'
    llm = _StubLLM([plan_resp, critic_r1, critic_r2, synth_resp])

    agent = _make_agent(settings, prompts, llm)
    result = agent.ask("Summarize AI progress", options=AgenticSearchOptions(max_rounds=2))

    assert result.rounds == 2
    assert result.critique.sufficient is True
    # After round 1 critique said insufficient and gave follow-up query
    assert len(llm.calls) == 4, "plan + critic1 + critic2 + synthesis"


def test_max_rounds_stops_loop(settings: Settings, prompts: PromptRegistry) -> None:
    plan_resp = '{"subquestions": ["model size"]}'
    critic_insufficient = '{"sufficient": false, "confidence": 0.2, "missing_information": ["cost"], "follow_up_queries": ["inference cost 2024"]}'
    synth_resp = '{"answer": "Evidence is insufficient for a complete answer."}'
    # Two critic calls (max_rounds=2) then synthesis
    llm = _StubLLM([plan_resp, critic_insufficient, critic_insufficient, synth_resp])

    agent = _make_agent(settings, prompts, llm)
    opts = AgenticSearchOptions(max_rounds=2)
    result = agent.ask("Everything about AI in 2024", options=opts)

    assert result.rounds == 2
    # Final critique should still be the last one returned (insufficient)
    assert result.critique.sufficient is False


def test_no_chunks_returns_empty_evidence(tmp_path: Path, prompts: PromptRegistry) -> None:
    """When there are no matching chunks, evidence list is empty but plan still runs."""
    db = tmp_path / "empty.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, source_path TEXT, page_count INTEGER, created_at TEXT);
        CREATE TABLE chunks (id TEXT PRIMARY KEY, document_id TEXT, section_id TEXT, text TEXT,
            page_start INTEGER, page_end INTEGER, figure_refs_json TEXT, table_refs_json TEXT, token_estimate INTEGER);
        CREATE TABLE claims (id TEXT PRIMARY KEY, text TEXT, confidence REAL,
            supporting_figure_id TEXT, supporting_table_id TEXT);
        CREATE TABLE tables (id TEXT PRIMARY KEY, caption TEXT, page INTEGER, page_end INTEGER);
        CREATE TABLE table_cells (table_id TEXT, row_index INTEGER, column_index INTEGER, text TEXT);
        CREATE TABLE figures (id TEXT PRIMARY KEY, page INTEGER, caption TEXT,
            interpretation_title TEXT, interpretation_chart_type TEXT, interpretation_confidence REAL);
        CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT, type TEXT, confidence REAL);
        """
    )
    con.commit()
    con.close()

    s = Settings()
    s.sqlite_path = db

    plan_resp = '{"subquestions": ["quantum computing 2025"]}'
    critic_resp = '{"sufficient": false, "confidence": 0.0, "missing_information": ["no evidence found"], "follow_up_queries": []}'
    synth_resp = '{"answer": "No relevant evidence was found in the knowledge base."}'
    llm = _StubLLM([plan_resp, critic_resp, synth_resp])
    mini_agent = MiniGraphRagAgent(s.sqlite_path, s.graph_storage_path)
    agent = AgenticSearchAgent(s, mini_agent, llm, prompts)

    result = agent.ask("quantum computing 2025", options=AgenticSearchOptions(max_rounds=1))
    assert result.evidence == []
    assert "No relevant" in result.answer or result.answer  # whatever the synth returns


def test_invalid_json_in_plan_falls_back_to_original_question(
    settings: Settings, prompts: PromptRegistry,
) -> None:
    plan_resp = "this is not JSON"
    critic_resp = '{"sufficient": true, "confidence": 0.8, "missing_information": [], "follow_up_queries": []}'
    synth_resp = '{"answer": "Trends include scaling and cost reduction [C1]."}'
    llm = _StubLLM([plan_resp, critic_resp, synth_resp])

    agent = _make_agent(settings, prompts, llm)
    result = agent.ask("What are the trends?", options=AgenticSearchOptions(max_rounds=1))

    # Plan parsing falls back to original question as sole subquestion
    assert result.plan.subquestions == ["What are the trends?"]
    assert result.answer


def test_synthesis_extracts_prose_from_json_wrapper(
    settings: Settings, prompts: PromptRegistry,
) -> None:
    plan_resp = '{"subquestions": ["inference cost"]}'
    critic_resp = '{"sufficient": true, "confidence": 0.9, "missing_information": [], "follow_up_queries": []}'
    synth_resp = '{"answer": "Inference costs fell 40% [C2]."}'
    llm = _StubLLM([plan_resp, critic_resp, synth_resp])

    agent = _make_agent(settings, prompts, llm)
    result = agent.ask("inference cost?", options=AgenticSearchOptions(max_rounds=1))

    assert result.answer == "Inference costs fell 40% [C2]."


def test_to_dict_is_json_serializable(settings: Settings, prompts: PromptRegistry) -> None:
    plan_resp = '{"subquestions": ["model size"]}'
    critic_resp = '{"sufficient": true, "confidence": 0.9, "missing_information": [], "follow_up_queries": []}'
    synth_resp = '{"answer": "Model sizes grew [C1]."}'
    llm = _StubLLM([plan_resp, critic_resp, synth_resp])

    agent = _make_agent(settings, prompts, llm)
    result = agent.ask("model size?", options=AgenticSearchOptions(max_rounds=1))
    payload = result.to_dict()
    json.dumps(payload, ensure_ascii=True)  # must not raise


def test_uses_planner_model_from_settings(settings: Settings, prompts: PromptRegistry) -> None:
    settings.azure_openai_reasoning_model = "custom-planner-x"
    plan_resp = '{"subquestions": ["model size"]}'
    critic_resp = '{"sufficient": true, "confidence": 0.9, "missing_information": [], "follow_up_queries": []}'
    synth_resp = '{"answer": "ok [C1]."}'
    llm = _StubLLM([plan_resp, critic_resp, synth_resp])

    agent = _make_agent(settings, prompts, llm)
    agent.ask("model size?", options=AgenticSearchOptions(max_rounds=1))

    # First call is the planner call
    assert llm.calls[0]["model"] == "custom-planner-x"


def test_token_usage_accumulates_across_rounds(settings: Settings, prompts: PromptRegistry) -> None:
    plan_resp = '{"subquestions": ["inference cost"]}'
    critic_r1 = '{"sufficient": false, "confidence": 0.3, "missing_information": ["benchmark"], "follow_up_queries": ["benchmark 2024"]}'
    critic_r2 = '{"sufficient": true, "confidence": 0.9, "missing_information": [], "follow_up_queries": []}'
    synth_resp = '{"answer": "ok [C2]."}'
    llm = _StubLLM([plan_resp, critic_r1, critic_r2, synth_resp])

    agent = _make_agent(settings, prompts, llm)
    result = agent.ask("trends?", options=AgenticSearchOptions(max_rounds=2))

    # Two critic calls, each contributing 100 input + 50 output tokens (stub)
    assert result.tokens.critic_input == 200, "should accumulate both critic calls"
    assert result.tokens.critic_output == 100


# --------------------------------------------------------------------------
# Unit helpers
# --------------------------------------------------------------------------


def test_safe_json_returns_dict_on_valid_json() -> None:
    assert _safe_json('{"a": 1}') == {"a": 1}


def test_safe_json_returns_empty_on_invalid() -> None:
    assert _safe_json("not json") == {}
    assert _safe_json("[1, 2, 3]") == {}  # list, not dict


def test_merge_evidence_deduplicates() -> None:
    from knowledge_extraction.application.services.agentic_search_agent import EvidenceItem

    a = EvidenceItem(kind="chunk", id="1", text="a", score=0.9, citation_label="[C1]")
    b = EvidenceItem(kind="chunk", id="2", text="b", score=0.8, citation_label="[C2]")
    duplicate_a = EvidenceItem(kind="chunk", id="1", text="a again", score=0.7, citation_label="[C3]")

    merged = _merge_evidence([a], [b, duplicate_a], max_items=10)
    assert len(merged) == 2
    assert merged[0].id == "1"
    assert merged[1].id == "2"


def test_merge_evidence_respects_max_items() -> None:
    from knowledge_extraction.application.services.agentic_search_agent import EvidenceItem

    existing = [EvidenceItem(kind="chunk", id=str(i), text="x", score=1.0, citation_label=f"[C{i}]") for i in range(3)]
    new = [EvidenceItem(kind="chunk", id=str(i + 10), text="y", score=0.5, citation_label=f"[C{i+10}]") for i in range(5)]

    merged = _merge_evidence(existing, new, max_items=5)
    assert len(merged) == 5


def test_merge_evidence_uses_composite_key() -> None:
    """Same numeric ID but different kind must not be treated as duplicate."""
    from knowledge_extraction.application.services.agentic_search_agent import EvidenceItem

    chunk = EvidenceItem(kind="chunk", id="1", text="chunk text", score=0.9, citation_label="[C1]")
    claim = EvidenceItem(kind="claim", id="1", text="claim text", score=0.8, citation_label="[L1]")

    merged = _merge_evidence([chunk], [claim], max_items=10)
    assert len(merged) == 2


# --------------------------------------------------------------------------
# agentic_index_available
# --------------------------------------------------------------------------


def test_agentic_index_available_true_when_chunks_present(settings: Settings) -> None:
    assert agentic_index_available(settings) is True


def test_agentic_index_available_false_when_db_missing(tmp_path: Path) -> None:
    s = Settings()
    s.sqlite_path = tmp_path / "missing.db"
    assert agentic_index_available(s) is False


def test_agentic_index_available_false_when_no_chunks_table(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE other (id TEXT)")
    con.commit()
    con.close()
    s = Settings()
    s.sqlite_path = db
    assert agentic_index_available(s) is False
