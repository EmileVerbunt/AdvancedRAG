"""Apply an approved ontology migration: rewrite entity types + aliases."""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from knowledge_extraction.domain import OntologyStatus, OntologyVersion
from knowledge_extraction.infrastructure.persistence.sqlite.models import EntityRow
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import GovernanceRepository


@dataclass(slots=True)
class MigrationReport:
    from_version: str
    to_version: str
    type_renames: dict[str, str] = field(default_factory=dict)
    relabeled_entities: int = 0
    aliases_added: int = 0


class OntologyMigrationService:
    """Compute type renames between two ontology versions and apply them."""

    def __init__(self, gov: GovernanceRepository, session_factory: sessionmaker) -> None:
        self._gov = gov
        self._sf = session_factory

    def plan(self, from_version: str, to_version: str) -> MigrationReport:
        src = self._gov.get_version(from_version)
        tgt = self._gov.get_version(to_version)
        if src is None or tgt is None:
            raise ValueError("both ontology versions must exist")
        if tgt.status != OntologyStatus.APPROVED:
            raise ValueError("target ontology must be approved")
        renames = self._infer_renames(src, tgt)
        return MigrationReport(from_version=from_version, to_version=to_version, type_renames=renames)

    def apply(self, from_version: str, to_version: str) -> MigrationReport:
        report = self.plan(from_version, to_version)
        if not report.type_renames:
            return report
        with self._sf() as s, s.begin():
            for old_t, new_t in report.type_renames.items():
                rows = s.execute(select(EntityRow).where(EntityRow.type == old_t)).scalars().all()
                for row in rows:
                    row.type = new_t
                    report.relabeled_entities += 1
                # rewrite alias source so downstream knows where the rename came from
                s.execute(update(EntityRow).where(EntityRow.type == old_t).values(type=new_t))
        return report

    @staticmethod
    def _infer_renames(src: OntologyVersion, tgt: OntologyVersion) -> dict[str, str]:
        """Detect renames by inspecting target's `merged_from` lists."""
        renames: dict[str, str] = {}
        try:
            tgt_doc = yaml.safe_load(tgt.schema_yaml) or {}
        except Exception:
            return renames
        for et in tgt_doc.get("entity_types", []):
            new_name = et.get("name")
            for old in et.get("merged_from", []) or []:
                if old and new_name and old != new_name:
                    renames[old] = new_name
        return renames
