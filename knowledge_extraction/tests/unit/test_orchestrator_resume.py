from __future__ import annotations

import asyncio

from knowledge_extraction.application.pipelines.orchestrator import Orchestrator
from knowledge_extraction.infrastructure.checkpointing.filesystem_checkpoint_store import (
    FilesystemCheckpointStore,
)


def test_orchestrator_resume_skips_checkpointed_render_and_figures(tmp_path) -> None:
    checkpoints = FilesystemCheckpointStore(tmp_path / "ck")
    document_id = "doc-1"
    checkpoints.mark_complete(document_id, "render")
    checkpoints.mark_complete(document_id, "figures")
    calls: list[str] = []

    async def stage(name: str) -> None:
        calls.append(name)

    orchestrator = Orchestrator(document_id, checkpoints)
    orchestrator.add("render", lambda: stage("render"))
    orchestrator.add("figures", lambda: stage("figures"), deps=["render"])
    orchestrator.add("extract", lambda: stage("extract"), deps=["render", "figures"])
    orchestrator.add("graph", lambda: stage("graph"), deps=["extract"])

    asyncio.run(orchestrator.run(resume=True))

    assert calls == ["extract", "graph"]
    assert checkpoints.is_complete(document_id, "extract")
    assert checkpoints.is_complete(document_id, "graph")


def test_orchestrator_without_resume_reruns_checkpointed_stages(tmp_path) -> None:
    checkpoints = FilesystemCheckpointStore(tmp_path / "ck")
    document_id = "doc-1"
    checkpoints.mark_complete(document_id, "render")
    checkpoints.mark_complete(document_id, "figures")
    calls: list[str] = []

    async def stage(name: str) -> None:
        calls.append(name)

    orchestrator = Orchestrator(document_id, checkpoints)
    orchestrator.add("render", lambda: stage("render"))
    orchestrator.add("figures", lambda: stage("figures"), deps=["render"])
    orchestrator.add("extract", lambda: stage("extract"), deps=["render", "figures"])

    asyncio.run(orchestrator.run(resume=False))

    assert calls == ["render", "figures", "extract"]


def test_checkpoint_store_clear_removes_done_marker(tmp_path) -> None:
    checkpoints = FilesystemCheckpointStore(tmp_path / "ck")
    document_id = "doc-1"
    checkpoints.mark_complete(document_id, "extract")
    assert checkpoints.is_complete(document_id, "extract")

    checkpoints.clear(document_id, "extract")

    assert checkpoints.is_complete(document_id, "extract") is False
