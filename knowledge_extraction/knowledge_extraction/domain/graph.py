"""Domain model: graph nodes, edges, communities."""
from __future__ import annotations

from pydantic import BaseModel, Field


class GraphNode(BaseModel):
    id: str
    label: str
    type: str
    properties: dict[str, object] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    source_id: str
    target_id: str
    type: str
    properties: dict[str, object] = Field(default_factory=dict)


class Community(BaseModel):
    id: str
    level: int = 0
    member_ids: list[str] = Field(default_factory=list)
    summary: str = ""
