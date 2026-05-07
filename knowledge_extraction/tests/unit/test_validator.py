from knowledge_extraction.application.services.ontology_validator import OntologyValidator
from knowledge_extraction.domain import (
    Entity,
    EntityTypeDef,
    ExtractionResult,
    OntologySchema,
    Relationship,
    RelationTypeDef,
)


def _schema() -> OntologySchema:
    return OntologySchema(
        version="1.0.0",
        entity_types=[EntityTypeDef(name="Model"), EntityTypeDef(name="Organization")],
        relationship_types=[RelationTypeDef(
            name="RELEASED_BY", allowed_source=["Model"], allowed_target=["Organization"],
        )],
    )


def test_validator_accepts_valid_edge() -> None:
    s = _schema()
    v = OntologyValidator(s)
    e1 = Entity(id="m1", name="GPT-4", type="Model")
    e2 = Entity(id="o1", name="OpenAI", type="Organization")
    r = Relationship(id="r1", source_id="m1", target_id="o1", type="RELEASED_BY")
    report = v.validate(ExtractionResult(chunk_id="c", entities=[e1, e2], relationships=[r]))
    assert report.unknown_relationships == []
    assert "r1" in report.accepted_relationships


def test_validator_rejects_off_schema_relation() -> None:
    s = _schema()
    v = OntologyValidator(s)
    e1 = Entity(id="o1", name="OpenAI", type="Organization")
    e2 = Entity(id="m1", name="GPT-4", type="Model")
    bad = Relationship(id="r1", source_id="o1", target_id="m1", type="RELEASED_BY")  # source must be Model
    report = v.validate(ExtractionResult(chunk_id="c", entities=[e1, e2], relationships=[bad]))
    assert "r1" in report.unknown_relationships
    assert report.edge_constraint_violations
