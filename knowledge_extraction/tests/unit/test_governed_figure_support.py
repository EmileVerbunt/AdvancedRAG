from __future__ import annotations

from knowledge_extraction.application.pipelines.stage_2a_extraction_governed import (
    _figure_context,
    _to_claims,
)
from knowledge_extraction.domain import Chunk, Figure


def test_to_claims_keeps_only_chunk_figure_refs() -> None:
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        section_id=None,
        text="claim text",
        page_start=1,
        page_end=1,
        figure_refs=["fig-a"],
    )
    claims = _to_claims(
        {
            "claims": [
                {"text": "valid", "supporting_figure_id": "fig-a"},
                {"text": "invalid", "supporting_figure_id": "fig-b"},
            ]
        },
        chunk,
        chunk.figure_refs,
        chunk.table_refs,
    )

    assert claims[0].supporting_figure_id == "fig-a"
    assert claims[1].supporting_figure_id is None


def test_to_claims_keeps_only_chunk_table_refs() -> None:
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        section_id=None,
        text="claim text",
        page_start=1,
        page_end=1,
        table_refs=["table-a"],
    )
    claims = _to_claims(
        {
            "claims": [
                {"text": "valid", "supporting_table_id": "table-a"},
                {"text": "invalid", "supporting_table_id": "table-b"},
            ]
        },
        chunk,
        chunk.figure_refs,
        chunk.table_refs,
    )

    assert claims[0].supporting_table_id == "table-a"
    assert claims[1].supporting_table_id is None


def test_to_claims_filters_table_and_figure_support_ids_independently() -> None:
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        section_id=None,
        text="claim text",
        page_start=1,
        page_end=1,
        figure_refs=["fig-a"],
        table_refs=["table-a"],
    )
    claims = _to_claims(
        {
            "claims": [
                {"text": "all valid", "supporting_figure_id": "fig-a", "supporting_table_id": "table-a"},
                {"text": "figure invalid", "supporting_figure_id": "fig-b", "supporting_table_id": "table-a"},
                {"text": "table invalid", "supporting_figure_id": "fig-a", "supporting_table_id": "table-b"},
            ]
        },
        chunk,
        chunk.figure_refs,
        chunk.table_refs,
    )

    assert claims[0].supporting_figure_id == "fig-a"
    assert claims[0].supporting_table_id == "table-a"
    assert claims[1].supporting_figure_id is None
    assert claims[1].supporting_table_id == "table-a"
    assert claims[2].supporting_figure_id == "fig-a"
    assert claims[2].supporting_table_id is None


def test_figure_context_contains_ref_and_caption() -> None:
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        section_id=None,
        text="claim text",
        page_start=1,
        page_end=1,
        figure_refs=["fig-a"],
    )
    figure = Figure(id="fig-a", document_id="doc-1", page=1, caption="Figure 1: Trend")

    context = _figure_context(chunk, {"fig-a": figure})

    assert "fig-a" in context
    assert "Figure 1: Trend" in context


def test_to_claims_drops_support_ids_when_chunk_has_no_refs() -> None:
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        section_id=None,
        text="claim text",
        page_start=1,
        page_end=1,
    )
    claims = _to_claims(
        {
            "claims": [
                {"text": "unsupported ids", "supporting_figure_id": "fig-a", "supporting_table_id": "table-a"},
            ]
        },
        chunk,
        chunk.figure_refs,
        chunk.table_refs,
    )

    assert claims[0].supporting_figure_id is None
    assert claims[0].supporting_table_id is None
