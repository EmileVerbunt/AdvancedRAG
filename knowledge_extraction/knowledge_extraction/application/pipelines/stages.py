"""The pipeline at a glance — single source of truth for stage names + order.

Every stage in the extraction pipeline is declared here so the rest of the
codebase can refer to a typed catalog instead of magic strings. Reordering or
introducing a new stage is a one-line change in :data:`PIPELINE`.

Pipeline:
    render  ─►  figures  ─►  extract  ─►  graph

`graph` is governed-mode only; discovery mode stops after `extract`.
"""
from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    """Canonical pipeline stages. Values double as on-disk checkpoint folders."""

    RENDER = "render"
    FIGURES = "figures"
    EXTRACT = "extract"
    GRAPH = "graph"


# Discovery mode stops after EXTRACT; GRAPH is governed-only.
PIPELINE_DISCOVERY: tuple[Stage, ...] = (Stage.RENDER, Stage.FIGURES, Stage.EXTRACT)
PIPELINE_GOVERNED: tuple[Stage, ...] = (*PIPELINE_DISCOVERY, Stage.GRAPH)


def parse_stage(value: str) -> Stage:
    """Coerce a CLI-provided string to a :class:`Stage`, or raise ValueError."""
    try:
        return Stage(value)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in Stage)
        raise ValueError(f"unknown stage '{value}'; expected one of: {allowed}") from exc
