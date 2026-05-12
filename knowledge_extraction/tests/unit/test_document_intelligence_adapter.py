from __future__ import annotations

import orjson

from knowledge_extraction.domain import (
    Document,
    Figure,
    LayoutBoundingRegion,
    LayoutSpan,
    Page,
    Table,
    TableCell,
)
from knowledge_extraction.infrastructure.ingestion.document_intelligence_adapter import (
    _load_cached_document,
    _parse_figure,
    _parse_table,
)
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import (
    RelationalRepository,
    make_engine,
    make_session_factory,
)


def test_parse_layout_inventory_extracts_provenance() -> None:
    table = _parse_table(
        {
            "id": "table-1",
            "caption": {"content": "Table 1"},
            "boundingRegions": [{"pageNumber": 2, "polygon": [1, 2, 3, 4]}],
            "spans": [{"offset": 10, "length": 5}],
            "cells": [
                {
                    "rowIndex": 0,
                    "columnIndex": 0,
                    "content": "Header",
                    "kind": "columnHeader",
                    "boundingRegions": [{"pageNumber": 2, "polygon": [1, 2, 3, 4]}],
                    "spans": [{"offset": 10, "length": 6}],
                }
            ],
        },
        document_id="doc1",
    )
    figure = _parse_figure(
        {
            "id": "1.1",
            "caption": {"content": "Figure 1"},
            "boundingRegions": [{"pageNumber": 3, "polygon": [5, 6, 7, 8]}],
            "spans": [{"offset": 20, "length": 4}],
            "elements": ["#/paragraphs/0"],
        },
        document_id="doc1",
    )

    assert table.page == 2
    assert table.caption == "Table 1"
    assert table.bounding_regions[0].page == 2
    assert table.cells[0].kind == "columnHeader"
    assert table.cells[0].bounding_regions[0].page == 2
    assert figure.page == 3
    assert figure.caption == "Figure 1"
    assert figure.bounding_regions[0].page == 3
    assert figure.elements == ["#/paragraphs/0"]


def test_cached_document_round_trip(tmp_path) -> None:
    work_dir = tmp_path / "artifacts"
    work_dir.mkdir()
    (work_dir / "layout.json").write_bytes(orjson.dumps({"pages": [{"pageNumber": 1}], "tables": [], "figures": []}))
    (work_dir / "doc.md").write_text("# Demo", encoding="utf-8")
    (work_dir / "tables.json").write_bytes(
        orjson.dumps(
            [
                Table(
                    id="table-1",
                    document_id="doc1",
                    page=1,
                    caption="Table 1",
                    cells=[TableCell(row=0, col=0, text="A")],
                    bounding_regions=[LayoutBoundingRegion(page=1, polygon=[1, 2])],
                    spans=[LayoutSpan(offset=0, length=4)],
                ).model_dump(mode="json")
            ]
        )
    )
    (work_dir / "figures.json").write_bytes(
        orjson.dumps(
            [
                Figure(
                    id="figure-1",
                    document_id="doc1",
                    page=1,
                    caption="Figure 1",
                    bounding_regions=[LayoutBoundingRegion(page=1, polygon=[3, 4])],
                    spans=[LayoutSpan(offset=5, length=2)],
                ).model_dump(mode="json")
            ]
        )
    )

    document = _load_cached_document(
        doc_id="doc1",
        source_path=tmp_path / "sample.pdf",
        layout_path=work_dir / "layout.json",
        markdown_path=work_dir / "doc.md",
        tables_path=work_dir / "tables.json",
        figures_path=work_dir / "figures.json",
    )

    assert document is not None
    assert document.page_count == 1
    assert document.tables[0].caption == "Table 1"
    assert document.figures[0].caption == "Figure 1"


def test_save_document_persists_layout_inventory(tmp_path) -> None:
    engine = make_engine(tmp_path / "ke.db")
    repo = RelationalRepository(make_session_factory(engine))
    document = Document(
        id="doc1",
        title="sample",
        source_path=tmp_path / "sample.pdf",
        pages=[Page(number=1)],
        tables=[
            Table(
                id="table-1",
                document_id="doc1",
                page=1,
                caption="Table 1",
                cells=[TableCell(row=0, col=0, text="A")],
                bounding_regions=[LayoutBoundingRegion(page=1, polygon=[1, 2])],
                spans=[LayoutSpan(offset=0, length=4)],
            )
        ],
        figures=[
            Figure(
                id="figure-1",
                document_id="doc1",
                page=1,
                caption="Figure 1",
                bounding_regions=[LayoutBoundingRegion(page=1, polygon=[3, 4])],
                spans=[LayoutSpan(offset=5, length=2)],
            )
        ],
    )

    repo.save_document(document)
    stats = repo.stats()

    assert stats["documents"] == 1
    assert stats["tables"] == 1
    assert stats["table_cells"] == 1
    assert stats["figures"] == 1
    assert repo.list_tables("doc1")[0].caption == "Table 1"
    assert repo.list_figures("doc1")[0].caption == "Figure 1"
