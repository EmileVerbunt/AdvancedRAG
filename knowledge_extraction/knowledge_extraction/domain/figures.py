"""Domain model: figures, tables, vision interpretations."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class TableCell(BaseModel):
    row: int
    col: int
    text: str


class Table(BaseModel):
    id: str
    document_id: str
    page: int
    caption: str = ""
    markdown: str = ""
    cells: list[TableCell] = Field(default_factory=list)


class Figure(BaseModel):
    id: str
    document_id: str
    page: int
    caption: str = ""
    image_path: Path | None = None


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
