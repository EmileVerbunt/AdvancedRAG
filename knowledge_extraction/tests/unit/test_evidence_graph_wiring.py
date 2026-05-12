from __future__ import annotations

from sqlalchemy import delete

from knowledge_extraction.application.pipelines.stage_5_graph import GraphBuildPipeline
from knowledge_extraction.domain import Chunk, Claim, ExtractionResult, Relationship
from knowledge_extraction.infrastructure.persistence.graph.networkx_store import NetworkXGraphStore
from knowledge_extraction.infrastructure.persistence.sqlite.models import RelationshipRow
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import (
    RelationalRepository,
    make_engine,
    make_session_factory,
)


def test_claim_support_ids_persisted_in_repository(tmp_path) -> None:
    engine = make_engine(tmp_path / "ke.db")
    sf = make_session_factory(engine)
    repo = RelationalRepository(sf)
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        section_id=None,
        text="chunk text",
        page_start=1,
        page_end=1,
    )
    result = ExtractionResult(
        chunk_id=chunk.id,
        claims=[
            Claim(
                id="claim-1",
                text="A table and figure support this claim.",
                supporting_table_id="table-1",
                supporting_figure_id="fig-1",
            )
        ],
    )

    repo.save_extraction(chunk, result)
    claims = repo.list_claims(chunk.id)

    assert len(claims) == 1
    assert claims[0].supporting_table_id == "table-1"
    assert claims[0].supporting_figure_id == "fig-1"


def test_graph_build_adds_claim_support_links() -> None:
    graph = NetworkXGraphStore()
    result = ExtractionResult(
        chunk_id="chunk-1",
        claims=[
            Claim(
                id="claim-1",
                text="A table and figure support this claim.",
                supporting_table_id="table-1",
                supporting_figure_id="fig-1",
            )
        ],
    )

    GraphBuildPipeline(graph).build([result])

    assert graph.g.nodes["claim-1"]["supporting_table_id"] == "table-1"
    assert graph.g.nodes["claim-1"]["supporting_figure_id"] == "fig-1"
    assert graph.g.nodes["table-1"]["type"] == "Table"
    assert graph.g.nodes["fig-1"]["type"] == "Figure"
    assert graph.g.has_edge("claim-1", "table-1")
    assert graph.g.has_edge("claim-1", "fig-1")


def test_chunk_extraction_checkpoint_detects_missing_relationship(tmp_path) -> None:
    engine = make_engine(tmp_path / "ke.db")
    sf = make_session_factory(engine)
    repo = RelationalRepository(sf)
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        section_id=None,
        text="chunk text",
        page_start=1,
        page_end=1,
    )
    result = ExtractionResult(
        chunk_id=chunk.id,
        relationships=[
            Relationship(
                id="rel-1",
                source_id="src-1",
                target_id="tgt-1",
                type="RELATED_TO",
            )
        ],
        claims=[Claim(id="claim-1", text="A claim.")],
    )

    repo.save_extraction(chunk, result)
    assert repo.needs_chunk_extraction(chunk.id) is False

    with sf() as session, session.begin():
        session.execute(delete(RelationshipRow).where(RelationshipRow.id == "rel-1"))

    assert repo.needs_chunk_extraction(chunk.id) is True


def test_load_extraction_for_chunk_rehydrates_claims_and_relationships(tmp_path) -> None:
    engine = make_engine(tmp_path / "ke.db")
    sf = make_session_factory(engine)
    repo = RelationalRepository(sf)
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        section_id=None,
        text="chunk text",
        page_start=1,
        page_end=1,
    )
    result = ExtractionResult(
        chunk_id=chunk.id,
        relationships=[
            Relationship(
                id="rel-1",
                source_id="src-1",
                target_id="tgt-1",
                type="RELATED_TO",
                confidence=0.9,
            )
        ],
        claims=[Claim(id="claim-1", text="A claim.", confidence=0.8)],
    )

    repo.save_extraction(chunk, result)
    hydrated = repo.load_extraction_for_chunk(chunk.id)

    assert len(hydrated.relationships) == 1
    assert hydrated.relationships[0].id == "rel-1"
    assert len(hydrated.claims) == 1
    assert hydrated.claims[0].id == "claim-1"
