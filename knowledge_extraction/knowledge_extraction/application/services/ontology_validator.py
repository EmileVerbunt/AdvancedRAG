"""Validate extraction results against the active ontology schema."""
from __future__ import annotations

from dataclasses import dataclass, field

from knowledge_extraction.domain import (
    UNKNOWN_TYPE,
    ExtractionResult,
    OntologySchema,
)


@dataclass(slots=True)
class ValidationReport:
    accepted_entities: list[str] = field(default_factory=list)
    accepted_relationships: list[str] = field(default_factory=list)
    unknown_entities: list[str] = field(default_factory=list)
    unknown_relationships: list[str] = field(default_factory=list)
    off_schema_relationships: list[tuple[str, str, str]] = field(default_factory=list)
    edge_constraint_violations: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def unknown_rate(self) -> float:
        total = (len(self.accepted_entities) + len(self.accepted_relationships)
                 + len(self.unknown_entities) + len(self.unknown_relationships))
        return ((len(self.unknown_entities) + len(self.unknown_relationships)) / total) if total else 0.0


class OntologyValidator:
    """Schema enforcement for governed extraction."""

    def __init__(self, schema: OntologySchema) -> None:
        self._schema = schema
        self._entity_names = schema.entity_names()
        self._relation_names = schema.relation_names()

    def validate(self, result: ExtractionResult) -> ValidationReport:
        report = ValidationReport()
        type_by_id: dict[str, str] = {}

        for e in result.entities:
            type_by_id[e.id] = e.type
            if e.type == UNKNOWN_TYPE or e.type not in self._entity_names:
                e.type = UNKNOWN_TYPE
                report.unknown_entities.append(e.id)
            else:
                report.accepted_entities.append(e.id)

        for r in result.relationships:
            if r.type == UNKNOWN_TYPE or r.type not in self._relation_names:
                report.off_schema_relationships.append((r.source_id, r.target_id, r.type))
                r.type = UNKNOWN_TYPE
                report.unknown_relationships.append(r.id)
                continue
            rel_def = self._schema.relation(r.type)
            if rel_def is None:
                report.unknown_relationships.append(r.id)
                continue
            src_t = type_by_id.get(r.source_id)
            tgt_t = type_by_id.get(r.target_id)
            if (rel_def.allowed_source and src_t and src_t not in rel_def.allowed_source) or \
               (rel_def.allowed_target and tgt_t and tgt_t not in rel_def.allowed_target):
                report.edge_constraint_violations.append((r.source_id, r.target_id, r.type))
                r.type = UNKNOWN_TYPE
                report.unknown_relationships.append(r.id)
            else:
                report.accepted_relationships.append(r.id)

        return report
