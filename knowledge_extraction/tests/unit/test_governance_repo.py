from datetime import UTC, datetime

from knowledge_extraction.domain import (
    OntologyProposal,
    OntologyProposalSource,
    OntologyStatus,
    OntologyVersion,
)
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import (
    GovernanceRepository,
    make_engine,
    make_session_factory,
)


def test_governance_round_trip(tmp_path) -> None:
    engine = make_engine(tmp_path / "ke.db")
    sf = make_session_factory(engine)
    gov = GovernanceRepository(sf)

    v = OntologyVersion(
        version="1.0.0", status=OntologyStatus.APPROVED, schema_yaml="version: 1.0.0\n",
        created_at=datetime.now(UTC),
        approved_at=datetime.now(UTC), approved_by="test",
    )
    gov.upsert_version(v)
    fetched = gov.latest_approved()
    assert fetched is not None
    assert fetched.version == "1.0.0"

    p = OntologyProposal(
        base_version="1.0.0", source_mode=OntologyProposalSource.DISCOVERY,
        schema_yaml="version: 1.1.0\n", confidence=0.7,
        created_at=datetime.now(UTC),
    )
    saved = gov.add_proposal(p)
    assert saved.id is not None
    listed = gov.list_proposals()
    assert any(item.id == saved.id for item in listed)

    gov.add_alias("org:abcd", "OpenAI")
    assert gov.find_canonical("OpenAI") == "org:abcd"
