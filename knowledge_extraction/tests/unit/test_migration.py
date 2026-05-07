from datetime import UTC, datetime

from knowledge_extraction.application.services.ontology_migration import OntologyMigrationService
from knowledge_extraction.domain import OntologyStatus, OntologyVersion
from knowledge_extraction.infrastructure.persistence.sqlite.models import EntityRow
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import (
    GovernanceRepository,
    make_engine,
    make_session_factory,
)


def test_migration_renames_entities(tmp_path) -> None:
    engine = make_engine(tmp_path / "ke.db")
    sf = make_session_factory(engine)
    gov = GovernanceRepository(sf)

    v1_yaml = """version: 1.0.0
entity_types:
  - name: LLM
    description: large language model
relationship_types: []
"""
    v2_yaml = """version: 1.1.0
entity_types:
  - name: Model
    description: AI model
    merged_from: [LLM]
relationship_types: []
"""
    now = datetime.now(UTC)
    gov.upsert_version(OntologyVersion(version="1.0.0", status=OntologyStatus.APPROVED,
                                       schema_yaml=v1_yaml, created_at=now, approved_at=now))
    gov.upsert_version(OntologyVersion(version="1.1.0", status=OntologyStatus.APPROVED,
                                       schema_yaml=v2_yaml, created_at=now, approved_at=now))

    with sf() as s, s.begin():
        s.add(EntityRow(id="e1", name="GPT-4", type="LLM", confidence=0.9))

    svc = OntologyMigrationService(gov, sf)
    report = svc.apply("1.0.0", "1.1.0")
    assert report.type_renames == {"LLM": "Model"}
    with sf() as s:
        row = s.get(EntityRow, "e1")
        assert row is not None and row.type == "Model"
