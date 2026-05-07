"""In-process event bus for pipeline → TUI communication."""
from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PipelineEvent:
    stage: str
    kind: str  # "start" | "progress" | "end" | "metric" | "failure"
    detail: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Callable[[PipelineEvent], None]] = []

    def subscribe(self, handler: Callable[[PipelineEvent], None]) -> None:
        self._subscribers.append(handler)

    def publish(self, event: PipelineEvent) -> None:
        for h in self._subscribers:
            with contextlib.suppress(Exception):
                h(event)
