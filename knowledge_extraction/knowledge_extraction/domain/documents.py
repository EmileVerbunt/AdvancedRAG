"""Domain model: documents, sections, chunks."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class Page(BaseModel):
    number: int
    text: str = ""
    image_path: Path | None = None


class Section(BaseModel):
    id: str
    title: str
    level: int = 1
    page_start: int
    page_end: int
    parent_id: str | None = None


class Chunk(BaseModel):
    id: str
    document_id: str
    section_id: str | None
    text: str
    page_start: int
    page_end: int
    figure_refs: list[str] = Field(default_factory=list)
    table_refs: list[str] = Field(default_factory=list)
    token_estimate: int = 0


class Document(BaseModel):
    id: str  # sha256(file)
    title: str
    source_path: Path
    pages: list[Page] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    markdown_path: Path | None = None
    layout_json_path: Path | None = None

    @property
    def page_count(self) -> int:
        return len(self.pages)
