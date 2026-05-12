from __future__ import annotations

from knowledge_extraction.infrastructure.checkpointing.filesystem_checkpoint_store import (
    FilesystemCheckpointStore,
)


def test_clear_from_clears_each_stage_in_order(tmp_path) -> None:
    store = FilesystemCheckpointStore(tmp_path / "ck")
    document_id = "doc-1"
    for stage in ("render", "figures", "extract", "graph"):
        store.mark_complete(document_id, stage)
        assert store.is_complete(document_id, stage)

    # Cascade redo: clear extract + graph, leave render + figures intact.
    store.clear_from(document_id, ["extract", "graph"])

    assert store.is_complete(document_id, "render")
    assert store.is_complete(document_id, "figures")
    assert not store.is_complete(document_id, "extract")
    assert not store.is_complete(document_id, "graph")


def test_clear_from_is_idempotent_for_missing_stages(tmp_path) -> None:
    store = FilesystemCheckpointStore(tmp_path / "ck")
    # No checkpoints exist yet — clear_from should be a no-op, not raise.
    store.clear_from("doc-1", ["render", "figures"])
