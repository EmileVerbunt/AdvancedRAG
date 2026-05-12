from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import orjson
from PIL import Image, ImageDraw

from knowledge_extraction.application.pipelines.stage_2c_figure_interpretation import (
    FigureInterpretationPipeline,
)
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.config.settings import get_settings
from knowledge_extraction.domain import ChartInterpretation, Chunk, Document, Page
from knowledge_extraction.infrastructure.persistence.sqlite.models import ChunkRow, FigureRow
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import (
    RelationalRepository,
    make_engine,
    make_session_factory,
)


@dataclass
class _StubVision:
    async def interpret_figure(self, figure, prompt):
        return ChartInterpretation(
            figure_id=figure.id,
            title="Sample chart",
            chart_type="bar",
            interpretation="It shows a simple bar chart.",
            confidence=0.91,
        )


def test_figure_pipeline_crops_and_persists(tmp_path: Path) -> None:
    settings = get_settings()
    settings.sqlite_path = tmp_path / "ke.db"
    settings.ensure_dirs()

    engine = make_engine(settings.sqlite_path)
    sf = make_session_factory(engine)
    repo = RelationalRepository(sf)
    prompts = PromptRegistry(settings.prompts_dir)

    document = Document(
        id="doc-fig",
        title="figures",
        source_path=tmp_path / "doc.pdf",
        pages=[Page(number=1, text="Figure 1. A chart")],
        layout_json_path=tmp_path / "layout.json",
    )
    document.layout_json_path.write_text(
        json.dumps(
            {
                "pages": [{"pageNumber": 1, "width": 100, "height": 100, "unit": "pixel"}],
                "figures": [
                    {
                        "pageNumber": 1,
                        "caption": {"content": "Figure 1: Example chart"},
                        "spans": [{"offset": 11, "length": 7}],
                        "elements": ["#/paragraphs/2"],
                        "boundingRegions": [
                            {"pageNumber": 1, "polygon": [20, 20, 80, 20, 80, 80, 20, 80]}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    page_path = pages_dir / "page_0001.png"
    img = Image.new("RGB", (100, 100), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle((20, 20, 80, 80), outline="black", fill="gray")
    img.save(page_path)

    chunk = {
        "id": "chunk-1",
        "document_id": document.id,
        "section_id": None,
        "text": "Figure 1. A chart",
        "page_start": 1,
        "page_end": 1,
    }
    repo.save_document(document)
    repo.save_chunks([Chunk(**chunk)])

    pipeline = FigureInterpretationPipeline(
        vision=_StubVision(),
        prompts=prompts,
        repo=repo,
        model="vision-model",
    )
    figures = asyncio.run(pipeline.run(document, pages_dir, tmp_path / "figures"))

    assert len(figures) == 1
    assert figures[0].image_path is not None and figures[0].image_path.exists()
    with Image.open(figures[0].image_path) as cropped:
        assert cropped.size == (60, 60)

    with sf() as s:
        row = s.get(FigureRow, figures[0].id)
        assert row is not None
        assert row.interpretation_json is not None
        interpretation = orjson.loads(row.interpretation_json)
        assert interpretation["figure_id"] == figures[0].id
        assert row.interpretation_model == "vision-model"
        assert row.interpretation_title == "Sample chart"
        assert row.interpretation_chart_type == "bar"
        assert row.interpretation_confidence == 0.91
        assert row.bounding_regions_json is not None
        assert row.spans_json is not None
        assert row.elements_json is not None
        chunk_row = s.get(ChunkRow, "chunk-1")
        assert chunk_row is not None
        assert figures[0].id in orjson.loads(chunk_row.figure_refs_json or "[]")

    persisted = repo.list_figures(document.id)
    assert len(persisted) == 1
    assert tuple(int(v) for v in (persisted[0].crop_box or ())) == (20, 20, 80, 80)
    assert persisted[0].bounding_regions[0].page == 1
    assert persisted[0].spans[0].offset == 11
    assert persisted[0].elements == ["#/paragraphs/2"]
