"""Filesystem-based checkpoint store."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson

from knowledge_extraction.domain import CheckpointRecord, StageStatus


class FilesystemCheckpointStore:
    """Stores per-(document, stage) marker files under the checkpoint root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _stage_dir(self, document_id: str, stage: str) -> Path:
        d = self.root / document_id / stage
        d.mkdir(parents=True, exist_ok=True)
        return d

    def artifact_dir(self, document_id: str, stage: str) -> Path:
        return self._stage_dir(document_id, stage)

    def is_complete(self, document_id: str, stage: str) -> bool:
        return (self._stage_dir(document_id, stage) / ".done").exists()

    def mark_complete(self, document_id: str, stage: str, artifact_path: Path | None = None) -> None:
        record = CheckpointRecord(
            document_id=document_id,
            stage=stage,
            status=StageStatus.DONE,
            artifact_path=str(artifact_path) if artifact_path else None,
            updated_at=datetime.now(UTC),
        )
        d = self._stage_dir(document_id, stage)
        (d / "checkpoint.json").write_bytes(orjson.dumps(record.model_dump(mode="json")))
        (d / ".done").touch()

    def mark_failed(self, document_id: str, stage: str, error: str) -> None:
        record = CheckpointRecord(
            document_id=document_id,
            stage=stage,
            status=StageStatus.FAILED,
            error=error,
            updated_at=datetime.now(UTC),
        )
        d = self._stage_dir(document_id, stage)
        (d / "checkpoint.json").write_bytes(orjson.dumps(record.model_dump(mode="json")))
