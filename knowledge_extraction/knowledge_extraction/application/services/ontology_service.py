"""Ontology service: load YAML, expose active schema."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from knowledge_extraction.config.ontology_loader import load_ontology
from knowledge_extraction.domain import OntologySchema, OntologyStatus, OntologyVersion
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import GovernanceRepository


class OntologyService:
    """Resolves the active ontology version and seeds v1.0.0 from YAML on first run."""

    def __init__(self, gov: GovernanceRepository, seed_yaml_path: Path) -> None:
        self._gov = gov
        self._seed_yaml_path = seed_yaml_path

    def bootstrap(self) -> OntologyVersion:
        latest = self._gov.latest_approved()
        if latest is not None:
            return latest
        seed = load_ontology(self._seed_yaml_path)
        version = OntologyVersion(
            version=seed.version or "1.0.0",
            status=OntologyStatus.APPROVED,
            schema_yaml=self._seed_yaml_path.read_text(encoding="utf-8"),
            created_at=datetime.now(UTC),
            approved_at=datetime.now(UTC),
            approved_by="bootstrap",
        )
        return self._gov.upsert_version(version)

    def active(self, version: str | None = None) -> tuple[OntologyVersion, OntologySchema]:
        if version:
            v = self._gov.get_version(version)
            if v is None:
                raise ValueError(f"Ontology version {version} not found")
        else:
            v = self._gov.latest_approved() or self.bootstrap()
        schema = self._parse(v.schema_yaml)
        return v, schema

    @staticmethod
    def _parse(yaml_text: str) -> OntologySchema:
        from knowledge_extraction.domain.ontology import EntityTypeDef, RelationTypeDef

        raw = yaml.safe_load(yaml_text)
        return OntologySchema(
            version=str(raw.get("version", "0.0.0")),
            description=raw.get("description", ""),
            entity_types=[EntityTypeDef(**e) for e in raw.get("entity_types", [])],
            relationship_types=[RelationTypeDef(**r) for r in raw.get("relationship_types", [])],
        )
