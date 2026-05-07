"""Load YAML ontology files into domain models."""
from __future__ import annotations

from pathlib import Path

import yaml

from knowledge_extraction.domain.ontology import (
    EntityTypeDef,
    OntologySchema,
    RelationTypeDef,
)


def load_ontology(path: Path) -> OntologySchema:
    """Parse an ontology YAML file into an :class:`OntologySchema`."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entity_types = [EntityTypeDef(**et) for et in raw.get("entity_types", [])]
    relationship_types = [RelationTypeDef(**rt) for rt in raw.get("relationship_types", [])]
    return OntologySchema(
        version=str(raw.get("version", "0.0.0")),
        description=raw.get("description", ""),
        entity_types=entity_types,
        relationship_types=relationship_types,
    )


def dump_ontology(schema: OntologySchema, path: Path) -> None:
    """Serialize a schema back to YAML at *path*."""
    payload = {
        "version": schema.version,
        "description": schema.description,
        "entity_types": [et.model_dump(exclude_none=True) for et in schema.entity_types],
        "relationship_types": [rt.model_dump(exclude_none=True) for rt in schema.relationship_types],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
