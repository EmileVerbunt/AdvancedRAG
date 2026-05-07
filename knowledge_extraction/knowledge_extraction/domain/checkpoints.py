"""Domain model: pipeline checkpoints."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class CheckpointRecord(BaseModel):
    document_id: str
    stage: str
    status: StageStatus
    artifact_path: str | None = None
    error: str | None = None
    updated_at: datetime
