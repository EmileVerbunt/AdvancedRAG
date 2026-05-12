"""Tests for the Microsoft GraphRAG retrieval agent adapter."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from knowledge_extraction.application.services.ms_graphrag_agent import (
    IndexNotFoundError,
    MsGraphRagAgent,
    _extract_answer,
    _route_method,
    graphrag_index_available,
)


def _settings_with_workdir(tmp_path: Path):
    from knowledge_extraction.config.settings import Settings
    s = Settings()
    s.graphrag_workdir = tmp_path / "graphrag"
    return s


def _populate_index(workdir: Path, version: str = "v1") -> Path:
    out = workdir / version / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "communities.parquet").write_bytes(b"placeholder-parquet")
    return workdir / version


# -------------------------------------------------------------- routing


@pytest.mark.parametrize(
    "question,expected",
    [
        ("When was Qwen2 released?", "local"),
        ("Who funded the Stanford HAI report?", "local"),
        ("What is the inference cost in 2024?", "local"),
        ("Compare US and China private AI investment", "global"),
        ("What does the report say overall about AI talent migration?", "global"),
        ("Tell me a joke", "local"),  # non-factoid leader, no synth keyword → local default
    ],
)
def test_route_method_picks_expected_search_kind(question: str, expected: str) -> None:
    assert _route_method(question) == expected


# -------------------------------------------------------------- output parsing


def test_extract_answer_strips_success_banner() -> None:
    raw = "INFO: starting...\nSUCCESS: Local Search Response:\nThe answer is X.\nMore detail."
    assert _extract_answer(raw) == "The answer is X.\nMore detail."


def test_extract_answer_returns_full_output_when_no_banner() -> None:
    assert _extract_answer("plain text") == "plain text"


# -------------------------------------------------------------- index discovery


def test_graphrag_index_available_false_when_dir_missing(tmp_path: Path) -> None:
    s = _settings_with_workdir(tmp_path)
    assert graphrag_index_available(s) is False


def test_graphrag_index_available_true_when_parquet_present(tmp_path: Path) -> None:
    s = _settings_with_workdir(tmp_path)
    s.graphrag_workdir.mkdir(parents=True)
    _populate_index(s.graphrag_workdir, "v1")
    assert graphrag_index_available(s) is True


def test_agent_raises_if_no_index(tmp_path: Path) -> None:
    s = _settings_with_workdir(tmp_path)
    agent = MsGraphRagAgent(s, executable="dummy-graphrag")
    with pytest.raises(IndexNotFoundError):
        agent.ask("anything")


def test_agent_picks_latest_index(tmp_path: Path) -> None:
    s = _settings_with_workdir(tmp_path)
    s.graphrag_workdir.mkdir(parents=True)
    older = _populate_index(s.graphrag_workdir, "v1")
    newer = _populate_index(s.graphrag_workdir, "v2")
    now = time.time()
    os.utime(older, (now - 60, now - 60))
    os.utime(newer, (now, now))
    agent = MsGraphRagAgent(s, executable="dummy-graphrag")
    assert agent._latest_workdir().name == "v2"


# -------------------------------------------------------------- subprocess mock


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", rc: int = 0) -> None:
        self._out, self._err, self.returncode = stdout, stderr, rc

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, self._err


def test_agent_invokes_subprocess_and_returns_answer(tmp_path: Path, monkeypatch) -> None:
    s = _settings_with_workdir(tmp_path)
    s.graphrag_workdir.mkdir(parents=True)
    _populate_index(s.graphrag_workdir, "v1")

    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["argv"] = args
        return _FakeProc(b"SUCCESS: Local Search Response:\nFinal answer here.", b"", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    agent = MsGraphRagAgent(s, executable="dummy-graphrag")
    answer = agent.ask("When was Qwen2 released?", method="local")
    assert answer.method == "local"
    assert answer.answer == "Final answer here."
    assert answer.exit_code == 0
    assert "--method" in captured["argv"]
    assert "local" in captured["argv"]


def test_agent_propagates_subprocess_failure(tmp_path: Path, monkeypatch) -> None:
    s = _settings_with_workdir(tmp_path)
    s.graphrag_workdir.mkdir(parents=True)
    _populate_index(s.graphrag_workdir, "v1")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc(b"", b"boom: missing config", rc=2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    agent = MsGraphRagAgent(s, executable="dummy-graphrag")
    with pytest.raises(RuntimeError, match="exited 2"):
        agent.ask("anything", method="local")
