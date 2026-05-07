from pathlib import Path

from knowledge_extraction.config.ontology_loader import load_ontology


def test_seed_ontology_loads() -> None:
    schema = load_ontology(Path(__file__).resolve().parents[2] / "config" / "ontology.yaml")
    assert schema.version
    names = schema.entity_names()
    assert "Organization" in names
    assert "Model" in names
    rel_names = schema.relation_names()
    assert "OUTPERFORMS" in rel_names
    out = schema.relation("OUTPERFORMS")
    assert out is not None
    assert "Model" in out.allowed_source
    assert "Model" in out.allowed_target
