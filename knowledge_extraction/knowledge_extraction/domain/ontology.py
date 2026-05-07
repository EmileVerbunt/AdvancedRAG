"""Domain model: ontology schema and governance value objects."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class EntityTypeDef(BaseModel):
    name: str
    description: str = ""


class RelationTypeDef(BaseModel):
    name: str
    description: str = ""
    allowed_source: list[str] = Field(default_factory=list)
    allowed_target: list[str] = Field(default_factory=list)


class OntologySchema(BaseModel):
    version: str
    description: str = ""
    entity_types: list[EntityTypeDef] = Field(default_factory=list)
    relationship_types: list[RelationTypeDef] = Field(default_factory=list)

    def entity_names(self) -> set[str]:
        return {et.name for et in self.entity_types}

    def relation_names(self) -> set[str]:
        return {rt.name for rt in self.relationship_types}

    def relation(self, name: str) -> RelationTypeDef | None:
        return next((rt for rt in self.relationship_types if rt.name == name), None)


class OntologyStatus(StrEnum):
    PROPOSED = "proposed"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"


class OntologyVersion(BaseModel):
    id: int | None = None
    version: str
    status: OntologyStatus
    schema_yaml: str
    created_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None


class OntologyProposalSource(StrEnum):
    DISCOVERY = "discovery"
    GOVERNED_REFINEMENT = "governed_refinement"


class OntologyProposal(BaseModel):
    id: int | None = None
    base_version: str | None
    source_mode: OntologyProposalSource
    schema_yaml: str
    diff_json: str | None = None
    confidence: float = 0.0
    status: OntologyStatus = OntologyStatus.PROPOSED
    created_at: datetime


class AliasMapping(BaseModel):
    canonical_id: str
    alias: str
    source: str  # "discovery" | "governed" | "manual"
    confidence: float = 1.0


class MergeRecord(BaseModel):
    surviving_id: str
    merged_id: str
    reason: str
    created_at: datetime


class DriftKind(StrEnum):
    UNKNOWN_RATE = "unknown_rate"
    OFF_SCHEMA_RELATION = "off_schema_relation"
    CLUSTERED_UNKNOWN = "clustered_unknown"
    NEW_TYPE_PRESSURE = "new_type_pressure"


class DriftEvent(BaseModel):
    id: int | None = None
    version: str
    kind: DriftKind
    detail: dict[str, object] = Field(default_factory=dict)
    observed_at: datetime
