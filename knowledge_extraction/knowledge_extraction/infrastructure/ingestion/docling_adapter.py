"""Docling-based ingestion adapter.

Falls back to a minimal pypdfium2 text extraction if Docling is unavailable
(e.g. native deps missing). The fallback still produces a valid Document so
the pipeline can run end-to-end in dev environments.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import pypdfium2 as pdfium

from knowledge_extraction.domain import Document, Page, Section

log = logging.getLogger(__name__)


class DoclingIngestionAdapter:
    name = "docling"

    async def ingest(self, pdf_path: Path, work_dir: Path) -> Document:
        work_dir.mkdir(parents=True, exist_ok=True)
        doc_id = _hash_file(pdf_path)
        markdown_path = work_dir / "doc.md"
        layout_path = work_dir / "layout.json"

        log.info("ingest.start pdf=%s doc_id=%s work_dir=%s", pdf_path.name, doc_id, work_dir)
        markdown, pages, sections = self._with_docling(pdf_path)
        if markdown is None:
            log.info("ingest.fallback pdf=%s reason=docling_unavailable", pdf_path.name)
            markdown, pages, sections = self._fallback(pdf_path)

        markdown_path.write_text(markdown, encoding="utf-8")
        layout_path.write_text("{}", encoding="utf-8")  # full layout JSON populated by DI adapter
        log.info(
            "ingest.complete pdf=%s pages=%d md_chars=%d md=%s",
            pdf_path.name, len(pages), len(markdown), markdown_path,
        )

        return Document(
            id=doc_id,
            title=pdf_path.stem,
            source_path=pdf_path,
            pages=pages,
            sections=sections,
            markdown_path=markdown_path,
            layout_json_path=layout_path,
        )

    def _with_docling(self, pdf_path: Path) -> tuple[str | None, list[Page], list[Section]]:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]
        except Exception as exc:
            log.info("docling.unavailable reason=%s", exc)
            return None, [], []
        try:
            log.info("docling.convert.start pdf=%s", pdf_path.name)
            t0 = time.perf_counter()
            converter = DocumentConverter()
            result = converter.convert(str(pdf_path))
            md = result.document.export_to_markdown()
            num_pages = getattr(result.document, "num_pages", 0) or 1
            pages = [Page(number=i + 1, text="") for i in range(num_pages)]
            log.info(
                "docling.convert.complete pdf=%s pages=%d md_chars=%d duration_ms=%.0f",
                pdf_path.name, num_pages, len(md), (time.perf_counter() - t0) * 1000,
            )
            return md, pages, []
        except Exception as exc:
            log.warning("docling.convert.failed pdf=%s error=%s", pdf_path.name, exc)
            return None, [], []

    def _fallback(self, pdf_path: Path) -> tuple[str, list[Page], list[Section]]:
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
        )

        pdf = pdfium.PdfDocument(str(pdf_path))
        pages: list[Page] = []
        md_parts: list[str] = []
        try:
            total = len(pdf)
            log.info("fallback.extract.start pdf=%s pages=%d", pdf_path.name, total)
            t0 = time.perf_counter()
            with Progress(
                TextColumn("[bold blue]extracting text"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                transient=True,
            ) as progress:
                task = progress.add_task("fallback", total=total)
                for i, page in enumerate(pdf, start=1):
                    tp = page.get_textpage()
                    text = tp.get_text_range()
                    pages.append(Page(number=i, text=text))
                    md_parts.append(f"\n\n## Page {i}\n\n{text}")
                    progress.advance(task)
            log.info(
                "fallback.extract.complete pdf=%s pages=%d duration_ms=%.0f",
                pdf_path.name, total, (time.perf_counter() - t0) * 1000,
            )
        finally:
            pdf.close()
        return "".join(md_parts), pages, []


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]
