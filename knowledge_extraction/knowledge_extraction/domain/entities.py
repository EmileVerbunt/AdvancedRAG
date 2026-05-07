"""Domain model: extracted entities, relationships, claims."""
from __future__ import annotations

from pydantic import BaseModel, Field

UNKNOWN_TYPE = "UNKNOWN"


class Evidence(BaseModel):
    chunk_id: str
    page: int
    span: str  # raw text excerpt supporting this fact


class Entity(BaseModel):
    id: str  # canonical id
    name: str
    type: str  # ontology entity type or "UNKNOWN"
    aliases: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    evidence: list[Evidence] = Field(default_factory=list)


class Relationship(BaseModel):
    id: str
    source_id: str
    target_id: str
    type: str  # ontology relation type or "UNKNOWN"
    confidence: float = 0.0
    evidence: list[Evidence] = Field(default_factory=list)


class Claim(BaseModel):
    id: str
    text: str
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    confidence: float = 0.0
    evidence: list[Evidence] = Field(default_factory=list)
    supporting_figure_id: str | None = None
    supporting_table_id: str | None = None


class RefinementSuggestion(BaseModel):
    kind: str  # "entity" | "relationship"
    name: str
    rationale: str


class ExtractionResult(BaseModel):
    chunk_id: str
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    refinement_suggestions: list[RefinementSuggestion] = Field(default_factory=list)
    raw_response: str = ""
    prompt_version: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
