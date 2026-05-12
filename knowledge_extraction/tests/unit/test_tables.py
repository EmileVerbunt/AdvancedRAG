from knowledge_extraction.application.pipelines.stage_1_chunking import SemanticChunker
from knowledge_extraction.domain import Document, Page
from knowledge_extraction.infrastructure.ingestion.table_extraction import extract_tables_from_layout
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import (
    RelationalRepository,
    make_engine,
    make_session_factory,
)


def test_extract_tables_from_layout_and_chunk_refs() -> None:
    layout = {
        "tables": [
            {
                "boundingRegions": [{"pageNumber": 2}],
                "caption": {"content": "Table 1. Results", "boundingRegions": [{"pageNumber": 2}]},
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "content": "Model", "kind": "columnHeader", "boundingRegions": [{"pageNumber": 2}], "spans": [{"offset": 10, "length": 5}]},
                    {"rowIndex": 0, "columnIndex": 1, "content": "Score", "kind": "columnHeader", "boundingRegions": [{"pageNumber": 2}]},
                    {"rowIndex": 1, "columnIndex": 0, "content": "GPT-4", "boundingRegions": [{"pageNumber": 2}]},
                    {"rowIndex": 1, "columnIndex": 1, "content": "0.9", "boundingRegions": [{"pageNumber": 2}]},
                ],
            }
        ]
    }
    tables = extract_tables_from_layout(layout, document_id="doc1")
    assert len(tables) == 1
    table = tables[0]
    assert table.page == 2
    assert table.page_end == 2
    assert table.caption == "Table 1. Results"
    assert table.caption_page == 2
    assert "GPT-4" in table.markdown
    assert table.cells[0].page == 2
    assert table.cells[0].span_start == 10

    document = Document(
        id="doc1",
        title="doc",
        source_path="doc.pdf",  # type: ignore[arg-type]
        pages=[Page(number=1), Page(number=2)],
        tables=tables,
    )
    chunker = SemanticChunker(target_chars=64, max_chars=128)
    _, chunks = chunker.chunk(document, "# Heading\n\nBody text.\n")
    assert chunks
    assert tables[0].id in chunks[0].table_refs


def test_table_repository_round_trip(tmp_path) -> None:
    engine = make_engine(tmp_path / "ke.db")
    sf = make_session_factory(engine)
    repo = RelationalRepository(sf)

    layout = {
        "tables": [
            {
                "boundingRegions": [{"pageNumber": 1}],
                "caption": "Summary",
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "content": "Metric"},
                    {"rowIndex": 0, "columnIndex": 1, "content": "Value"},
                ],
            }
        ]
    }
    tables = extract_tables_from_layout(layout, document_id="doc2")
    repo.save_tables(tables)
    loaded = repo.list_tables("doc2")
    assert len(loaded) == 1
    assert loaded[0].caption == "Summary"
    assert loaded[0].cells[1].text == "Value"


def test_table_repository_round_trip_preserves_provenance(tmp_path) -> None:
    engine = make_engine(tmp_path / "ke.db")
    sf = make_session_factory(engine)
    repo = RelationalRepository(sf)
    repo.save_document(
        Document(
            id="doc3",
            title="doc",
            source_path="doc.pdf",  # type: ignore[arg-type]
            pages=[Page(number=1), Page(number=2)],
        )
    )

    layout = {
        "tables": [
            {
                "boundingRegions": [
                    {"pageNumber": 1, "polygon": [1, 2, 3, 4]},
                    {"pageNumber": 2, "polygon": [5, 6, 7, 8]},
                ],
                "caption": {"content": "Summary", "boundingRegions": [{"pageNumber": 2}]},
                "spans": [{"offset": 2, "length": 6}],
                "cells": [
                    {
                        "rowIndex": 0,
                        "columnIndex": 0,
                        "content": "Metric",
                        "kind": "columnHeader",
                        "boundingRegions": [{"pageNumber": 2, "polygon": [9, 10, 11, 12]}],
                        "spans": [{"offset": 10, "length": 5}],
                    },
                    {"rowIndex": 0, "columnIndex": 1, "content": "Value"},
                ],
            }
        ]
    }
    tables = extract_tables_from_layout(layout, document_id="doc3")
    repo.save_tables(tables)
    loaded = repo.list_tables("doc3")

    assert len(loaded) == 1
    table = loaded[0]
    assert table.page == 1
    assert table.page_end == 2
    assert table.caption_page == 2
    assert table.bounding_regions[1].page == 2
    assert table.spans[0].offset == 2
    assert "| Metric | Value |" in table.markdown
    assert table.cells[0].page == 2
    assert table.cells[0].span_start == 10
    assert table.cells[0].span_end == 15
    assert table.cells[0].bounding_regions[0].page == 2
    assert table.cells[0].spans[0].length == 5
