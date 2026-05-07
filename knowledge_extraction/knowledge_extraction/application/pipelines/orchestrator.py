"""Pipeline orchestrator: stage DAG + checkpoint-aware resume."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from knowledge_extraction.application.ports import CheckpointPort
from knowledge_extraction.infrastructure.telemetry.observability import bound, stage_event
from knowledge_extraction.infrastructure.telemetry.otel_setup import span
from knowledge_extraction.tui.events import EventBus, PipelineEvent

log = logging.getLogger(__name__)

StageFn = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class Stage:
    name: str
    fn: StageFn
    deps: list[str] = field(default_factory=list)


class Orchestrator:
    """Runs ordered stages, skipping those already checkpointed."""

    def __init__(self, document_id: str, checkpoints: CheckpointPort, bus: EventBus | None = None) -> None:
        self._document_id = document_id
        self._checkpoints = checkpoints
        self._stages: list[Stage] = []
        self._bus = bus or EventBus()

    def add(self, name: str, fn: StageFn, *, deps: list[str] | None = None) -> None:
        self._stages.append(Stage(name=name, fn=fn, deps=deps or []))

    async def run(self, *, resume: bool = True) -> None:
        completed: set[str] = set()
        with bound(document_id=self._document_id):
            for stage in self._stages:
                missing = [d for d in stage.deps if d not in completed]
                if missing:
                    log.warning("stage waiting on dependencies",
                                extra={"stage": stage.name, "missing": missing})

                if resume and self._checkpoints.is_complete(self._document_id, stage.name):
                    log.info("stage skipped (checkpointed)", extra={"stage": stage.name, "skipped": True})
                    self._bus.publish(PipelineEvent(stage.name, "end", {"resumed": True}))
                    completed.add(stage.name)
                    continue

                self._bus.publish(PipelineEvent(stage.name, "start"))
                try:
                    with stage_event(stage.name) as ev, span(f"stage.{stage.name}",
                                                              document_id=self._document_id):
                        await stage.fn()
                        ev["completed"] = True
                    self._checkpoints.mark_complete(self._document_id, stage.name)
                    self._bus.publish(PipelineEvent(stage.name, "end"))
                    completed.add(stage.name)
                except Exception as exc:
                    self._checkpoints.mark_failed(self._document_id, stage.name, str(exc))
                    self._bus.publish(PipelineEvent(stage.name, "failure", {"error": str(exc)}))
                    raise
