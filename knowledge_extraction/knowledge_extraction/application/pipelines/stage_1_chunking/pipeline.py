"""Section-aware semantic chunker with provenance preservation."""
from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections.abc import Iterable

from knowledge_extraction.domain import Chunk, Document, Section

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_PAGE_BREAK_RE = re.compile(r"<!--\s*PageBreak\s*-->")


class SemanticChunker:
    """Split a Document's markdown into chunks bounded by headings.

    Falls back to fixed-size token windows when a section has no internal structure.
    Each chunk carries (document_id, section_id, page_start, page_end).
    """

    def __init__(self, target_chars: int = 2400, max_chars: int = 3600) -> None:
        self._target = target_chars
        self._max = max_chars

    def chunk(self, document: Document, markdown: str) -> tuple[list[Section], list[Chunk]]:
        page_count = max(1, document.page_count or 1)
        page_break_offsets = self._page_break_offsets(markdown)
        sections, slices, body_starts = self._extract_sections(
            markdown, document.id, page_count, page_break_offsets,
        )
        chunks: list[Chunk] = []
        for section, body, body_start in zip(sections, slices, body_starts, strict=True):
            for chunk_text, rel_start, rel_end in self._split_with_offsets(body):
                if not chunk_text.strip():
                    continue
                cid = _hash(f"{document.id}::{section.id}::{chunk_text[:120]}")
                abs_start = body_start + rel_start
                abs_end = body_start + max(rel_start, rel_end - 1)
                chunk_page_start = self._page_for_offset(
                    abs_start, page_count=page_count, page_break_offsets=page_break_offsets, total_chars=len(markdown),
                )
                chunk_page_end = self._page_for_offset(
                    abs_end, page_count=page_count, page_break_offsets=page_break_offsets, total_chars=len(markdown),
                )
                table_refs = self._tables_for_range(document, chunk_page_start, chunk_page_end)
                chunks.append(Chunk(
                    id=cid,
                    document_id=document.id,
                    section_id=section.id,
                    text=chunk_text.strip(),
                    page_start=chunk_page_start,
                    page_end=chunk_page_end,
                    table_refs=table_refs,
                    token_estimate=max(1, len(chunk_text) // 4),
                ))
        return sections, chunks

    def _extract_sections(
        self,
        markdown: str,
        doc_id: str,
        page_count: int,
        page_break_offsets: list[int],
    ) -> tuple[list[Section], list[str], list[int]]:
        matches = list(_HEADING_RE.finditer(markdown))
        if not matches:
            root = Section(
                id=_hash(doc_id + "root"), title="Document", level=1,
                page_start=1, page_end=page_count,
            )
            return [root], [markdown], [0]
        sections: list[Section] = []
        slices: list[str] = []
        body_starts: list[int] = []
        for i, m in enumerate(matches):
            section_start = m.start()
            body_start = m.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
            page = self._page_for_offset(
                section_start,
                page_count=page_count,
                page_break_offsets=page_break_offsets,
                total_chars=len(markdown),
            )
            sections.append(Section(
                id=_hash(doc_id + m.group(2) + str(section_start)),
                title=m.group(2).strip(),
                level=len(m.group(1)),
                page_start=page,
                page_end=page,
            ))
            slices.append(markdown[body_start:body_end])
            body_starts.append(body_start)

        for idx, section in enumerate(sections):
            body_start = body_starts[idx]
            body_end = body_start + len(slices[idx])
            end_offset = max(body_start, body_end - 1)
            section.page_end = self._page_for_offset(
                end_offset,
                page_count=page_count,
                page_break_offsets=page_break_offsets,
                total_chars=len(markdown),
            )
            section.page_end = max(section.page_start, section.page_end)
        return sections, slices, body_starts

    def _split_with_offsets(self, text: str) -> Iterable[tuple[str, int, int]]:
        text = text.strip()
        if len(text) <= self._max:
            yield text, 0, len(text)
            return
        # Split by paragraph, accumulate up to target.
        buffer: list[str] = []
        buffer_start = 0
        cursor = 0
        size = 0
        for para in re.split(r"\n{2,}", text):
            p = para.strip()
            para_start = text.find(para, cursor)
            if para_start < 0:
                para_start = cursor
            para_end = para_start + len(para)
            cursor = max(cursor, para_end + 2)
            if not p:
                continue
            if size + len(p) > self._target and buffer:
                merged = "\n\n".join(buffer)
                yield merged, buffer_start, buffer_start + len(merged)
                buffer, size = [p], len(p)
                buffer_start = para_start
                continue
            if not buffer:
                buffer_start = para_start
            buffer.append(p)
            size += len(p)
            if size >= self._max:
                merged = "\n\n".join(buffer)
                yield merged, buffer_start, buffer_start + len(merged)
                buffer, size = [], 0
        if buffer:
            merged = "\n\n".join(buffer)
            yield merged, buffer_start, buffer_start + len(merged)

    @staticmethod
    def _page_break_offsets(markdown: str) -> list[int]:
        offsets = [0]
        for match in _PAGE_BREAK_RE.finditer(markdown):
            offsets.append(match.end())
        return offsets

    @staticmethod
    def _page_for_offset(
        offset: int,
        *,
        page_count: int,
        page_break_offsets: list[int],
        total_chars: int,
    ) -> int:
        if len(page_break_offsets) > 1:
            idx = bisect_right(page_break_offsets, max(0, offset)) - 1
            return max(1, min(idx + 1, len(page_break_offsets)))
        # Fallback when markdown has no explicit page breaks.
        total_chars = max(1, total_chars)
        return min(page_count, max(1, 1 + (max(0, offset) * page_count) // total_chars))

    @staticmethod
    def _tables_for_range(document: Document, page_start: int, page_end: int) -> list[str]:
        refs: list[str] = []
        for table in document.tables:
            table_end = table.page_end or table.page
            if table.page > page_end or table_end < page_start:
                continue
            refs.append(table.id)
        return refs


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
