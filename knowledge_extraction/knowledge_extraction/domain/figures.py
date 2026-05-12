"""Domain model: figures, tables, vision interpretations."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class LayoutSpan(BaseModel):
    offset: int
    length: int


class LayoutBoundingRegion(BaseModel):
    page: int
    polygon: list[float] = Field(default_factory=list)


class TableCell(BaseModel):
    row: int
    col: int
    text: str
    kind: str = ""
    row_span: int = 1
    col_span: int = 1
    page: int | None = None
    span_start: int | None = None
    span_end: int | None = None
    bounding_regions: list[LayoutBoundingRegion] = Field(default_factory=list)
    spans: list[LayoutSpan] = Field(default_factory=list)


class Table(BaseModel):
    id: str
    document_id: str
    page: int
    page_end: int | None = None
    caption: str = ""
    caption_page: int | None = None
    markdown: str = ""
    cells: list[TableCell] = Field(default_factory=list)
    bounding_regions: list[LayoutBoundingRegion] = Field(default_factory=list)
    spans: list[LayoutSpan] = Field(default_factory=list)


class Figure(BaseModel):
    id: str
    document_id: str
    page: int
    caption: str = ""
    image_path: Path | None = None
    crop_box: tuple[float, float, float, float] | None = None
    bounding_regions: list[LayoutBoundingRegion] = Field(default_factory=list)
    spans: list[LayoutSpan] = Field(default_factory=list)
    elements: list[str] = Field(default_factory=list)


class ChartAxis(BaseModel):
    name: str
    unit: str | None = None


class ChartMetric(BaseModel):
    name: str
    value: float | str | None = None
    unit: str | None = None


class ChartTrend(BaseModel):
    description: str
    direction: str  # "up" | "down" | "flat"


class ChartInterpretation(BaseModel):
    figure_id: str
    title: str = ""
    chart_type: str = ""
    axes: list[ChartAxis] = Field(default_factory=list)
    legends: list[str] = Field(default_factory=list)
    metrics: list[ChartMetric] = Field(default_factory=list)
    trends: list[ChartTrend] = Field(default_factory=list)
    interpretation: str = ""
    confidence: float = 0.0
