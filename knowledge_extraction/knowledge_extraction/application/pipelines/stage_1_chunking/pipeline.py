"""Section-aware semantic chunker with provenance preservation."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from knowledge_extraction.domain import Chunk, Document, Section

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


class SemanticChunker:
    """Split a Document's markdown into chunks bounded by headings.

    Falls back to fixed-size token windows when a section has no internal structure.
    Each chunk carries (document_id, section_id, page_start, page_end).
    """

    def __init__(self, target_chars: int = 2400, max_chars: int = 3600) -> None:
        self._target = target_chars
        self._max = max_chars

    def chunk(self, document: Document, markdown: str) -> tuple[list[Section], list[Chunk]]:
        sections, slices = self._extract_sections(markdown, document.id, document.page_count or 1)
        chunks: list[Chunk] = []
        for section, body in zip(sections, slices, strict=True):
            for chunk_text in self._split(body):
                if not chunk_text.strip():
                    continue
                cid = _hash(f"{document.id}::{section.id}::{chunk_text[:120]}")
                chunks.append(Chunk(
                    id=cid,
                    document_id=document.id,
                    section_id=section.id,
                    text=chunk_text.strip(),
                    page_start=section.page_start,
                    page_end=section.page_end,
                    token_estimate=max(1, len(chunk_text) // 4),
                ))
        return sections, chunks

    def _extract_sections(
        self, markdown: str, doc_id: str, page_count: int
    ) -> tuple[list[Section], list[str]]:
        matches = list(_HEADING_RE.finditer(markdown))
        if not matches:
            root = Section(
                id=_hash(doc_id + "root"), title="Document", level=1,
                page_start=1, page_end=page_count,
            )
            return [root], [markdown]
        sections: list[Section] = []
        slices: list[str] = []
        # Best-effort page mapping: split markdown evenly across pages.
        total_chars = max(1, len(markdown))
        for i, m in enumerate(matches):
            pos = m.start()
            page = min(page_count, max(1, 1 + (pos * page_count) // total_chars))
            sections.append(Section(
                id=_hash(doc_id + m.group(2) + str(pos)),
                title=m.group(2).strip(),
                level=len(m.group(1)),
                page_start=page,
                page_end=page,
            ))
            body_start = m.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
            slices.append(markdown[body_start:body_end])
        # Fill page_end as next-section start - 1.
        for i, s in enumerate(sections[:-1]):
            s.page_end = max(s.page_start, sections[i + 1].page_start - 1)
        sections[-1].page_end = page_count
        return sections, slices

    def _split(self, text: str) -> Iterable[str]:
        text = text.strip()
        if len(text) <= self._max:
            yield text
            return
        # Split by paragraph, accumulate up to target.
        buffer: list[str] = []
        size = 0
        for para in re.split(r"\n{2,}", text):
            p = para.strip()
            if not p:
                continue
            if size + len(p) > self._target and buffer:
                yield "\n\n".join(buffer)
                buffer, size = [p], len(p)
                continue
            buffer.append(p)
            size += len(p)
            if size >= self._max:
                yield "\n\n".join(buffer)
                buffer, size = [], 0
        if buffer:
            yield "\n\n".join(buffer)


def section_text(markdown: str, section: Section) -> str:
    """Return the slice of *markdown* belonging to *section*.

    Note: title-only lookup; ambiguous when sections share titles. Prefer
    ``SemanticChunker._extract_sections`` which slices by heading offsets.
    """
    pattern = re.compile(rf"^#{{1,6}}\s+{re.escape(section.title)}\s*$", re.MULTILINE)
    match = pattern.search(markdown)
    if not match:
        return ""
    start = match.end()
    next_match = _HEADING_RE.search(markdown, pos=start)
    end = next_match.start() if next_match else len(markdown)
    return markdown[start:end]


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]
