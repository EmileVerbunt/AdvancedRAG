"""Azure AI Document Intelligence adapter."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import orjson

from knowledge_extraction.config.settings import AzureAuthMode, get_settings
from knowledge_extraction.domain import (
    Document,
    Figure,
    LayoutBoundingRegion,
    LayoutSpan,
    Page,
    Table,
    TableCell,
)
from knowledge_extraction.infrastructure.ingestion.table_extraction import extract_tables_from_layout
from knowledge_extraction.infrastructure.telemetry.observability import wide_event

log = logging.getLogger(__name__)


class DocumentIntelligenceAdapter:
    name = "document_intelligence"

    async def ingest(self, pdf_path: Path, work_dir: Path) -> Document:
        work_dir.mkdir(parents=True, exist_ok=True)
        doc_id = _hash_file(pdf_path)
        layout_path = work_dir / "layout.json"
        markdown_path = work_dir / "doc.md"
        tables_path = work_dir / "tables.json"
        figures_path = work_dir / "figures.json"

        cached = _load_cached_document(
            doc_id=doc_id,
            source_path=pdf_path,
            layout_path=layout_path,
            markdown_path=markdown_path,
            tables_path=tables_path,
            figures_path=figures_path,
        )
        if cached is not None:
            log.info("ingest.resume pdf=%s doc_id=%s work_dir=%s", pdf_path.name, doc_id, work_dir)
            return cached

        s = get_settings()
        if not s.azure_document_intelligence_endpoint:
            raise RuntimeError("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is not configured")

        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.ai.documentintelligence.models import DocumentContentFormat

        if s.azure_auth_mode is AzureAuthMode.CREDENTIAL:
            from azure.identity import DefaultAzureCredential

            client = DocumentIntelligenceClient(
                endpoint=s.azure_document_intelligence_endpoint,
                credential=DefaultAzureCredential(),
            )
        else:
            from azure.core.credentials import AzureKeyCredential

            client = DocumentIntelligenceClient(
                endpoint=s.azure_document_intelligence_endpoint,
                credential=AzureKeyCredential(s.azure_document_intelligence_key),
            )

        with wide_event(
            "ingest.document_intelligence",
            pdf=pdf_path.name,
            model="prebuilt-layout",
            work_dir=str(work_dir),
        ) as ev:
            with pdf_path.open("rb") as f:
                poller = client.begin_analyze_document(
                    "prebuilt-layout",
                    f,
                    output_content_format=DocumentContentFormat.MARKDOWN,
                    output=["figures"],
                )
            ev["operation"] = "poller.result"
            result = poller.result()
            result_dict = _result_as_dict(result)
            ev["pages"] = len(result_dict.get("pages", []))
            ev["tables"] = len(result_dict.get("tables", []))
            ev["figures"] = len(result_dict.get("figures", []))

            layout_path.write_bytes(orjson.dumps(result_dict, default=str, option=orjson.OPT_INDENT_2))
            markdown_path.write_text(getattr(result, "content", "") or result_dict.get("content", "") or "", encoding="utf-8")

            pages = [Page(number=int(page.get("pageNumber", i + 1)), text="") for i, page in enumerate(result_dict.get("pages", []))]
            if not pages:
                pages = [Page(number=i + 1, text="") for i in range(len(result_dict.get("pages", [])) or 1)]

            tables = extract_tables_from_layout(result_dict, document_id=doc_id)
            figures = [
                _parse_figure(figure, document_id=doc_id)
                for figure in result_dict.get("figures", [])
            ]

            _write_json(tables_path, [table.model_dump(mode="json") for table in tables])
            _write_json(figures_path, [figure.model_dump(mode="json") for figure in figures])

            result_id = _result_id_from_poller(poller)
            if result_id:
                figures_dir = work_dir / "figures"
                figures_dir.mkdir(parents=True, exist_ok=True)
                for figure in figures:
                    try:
                        image_path = figures_dir / f"{figure.id}.png"
                        with image_path.open("wb") as f:
                            for block in client.get_analyze_result_figure("prebuilt-layout", result_id, figure.id):
                                f.write(block)
                        figure.image_path = image_path
                    except Exception as exc:  # best-effort artifact only
                        log.debug("figure.render.failed figure_id=%s error=%s", figure.id, exc)

            return Document(
                id=doc_id,
                title=pdf_path.stem,
                source_path=pdf_path,
                pages=pages,
                sections=[],
                tables=tables,
                figures=figures,
                markdown_path=markdown_path,
                layout_json_path=layout_path,
                tables_json_path=tables_path,
                figures_json_path=figures_path,
                layout_source="document_intelligence",
            )


def _load_cached_document(
    *,
    doc_id: str,
    source_path: Path,
    layout_path: Path,
    markdown_path: Path,
    tables_path: Path,
    figures_path: Path,
) -> Document | None:
    if not layout_path.exists() or not markdown_path.exists():
        return None

    layout_data = _read_json(layout_path, default={})
    pages = [Page(number=int(page.get("pageNumber", i + 1)), text="") for i, page in enumerate(layout_data.get("pages", []))]
    if not pages:
        pages = [Page(number=1, text="")]

    tables = [
        Table.model_validate(item)
        for item in _read_json(tables_path, default=[])
    ]
    figures = [
        Figure.model_validate(item)
        for item in _read_json(figures_path, default=[])
    ]

    return Document(
        id=doc_id,
        title=source_path.stem,
        source_path=source_path,
        pages=pages,
        sections=[],
        tables=tables,
        figures=figures,
        markdown_path=markdown_path,
        layout_json_path=layout_path,
        tables_json_path=tables_path if tables_path.exists() else None,
        figures_json_path=figures_path if figures_path.exists() else None,
        layout_source="document_intelligence",
    )


def _parse_table(data: dict[str, Any], *, document_id: str) -> Table:
    bounding_regions = [_parse_region(region) for region in data.get("boundingRegions", data.get("bounding_regions", []))]
    spans = [_parse_span(span) for span in data.get("spans", [])]
    cells = [_parse_table_cell(cell) for cell in data.get("cells", [])]
    page = _first_page(bounding_regions) or _first_page([region for cell in cells for region in cell.bounding_regions]) or 1
    caption = data.get("caption")
    caption_regions = [_parse_region(region) for region in caption.get("boundingRegions", [])] if isinstance(caption, dict) else []
    return Table(
        id=str(data.get("id") or f"table-{document_id}-{page}-{len(cells)}"),
        document_id=document_id,
        page=page,
        page_end=max((region.page for region in bounding_regions if region.page), default=page),
        caption=_text_value(caption),
        caption_page=_first_page(caption_regions),
        markdown=_text_value(data.get("markdown") or data.get("content")),
        cells=cells,
        bounding_regions=bounding_regions,
        spans=spans,
    )


def _parse_table_cell(data: dict[str, Any]) -> TableCell:
    bounding_regions = [_parse_region(region) for region in data.get("boundingRegions", data.get("bounding_regions", []))]
    spans = [_parse_span(span) for span in data.get("spans", [])]
    return TableCell(
        row=int(data.get("rowIndex", data.get("row", 0))),
        col=int(data.get("columnIndex", data.get("col", 0))),
        text=_text_value(data.get("content") or data.get("text")),
        kind=str(data.get("kind") or ""),
        row_span=int(data.get("rowSpan", data.get("row_span", 1)) or 1),
        col_span=int(data.get("columnSpan", data.get("col_span", 1)) or 1),
        page=_first_page(bounding_regions),
        span_start=spans[0].offset if spans else None,
        span_end=(spans[0].offset + spans[0].length) if spans else None,
        bounding_regions=bounding_regions,
        spans=spans,
    )


def _parse_figure(data: dict[str, Any], *, document_id: str) -> Figure:
    bounding_regions = [_parse_region(region) for region in data.get("boundingRegions", data.get("bounding_regions", []))]
    spans = [_parse_span(span) for span in data.get("spans", [])]
    page = _first_page(bounding_regions) or 1
    return Figure(
        id=str(data.get("id") or f"figure-{document_id}-{page}-{len(spans)}"),
        document_id=document_id,
        page=page,
        caption=_text_value(data.get("caption")),
        bounding_regions=bounding_regions,
        spans=spans,
        elements=[str(item) for item in data.get("elements", [])],
    )


def _parse_region(data: dict[str, Any]) -> LayoutBoundingRegion:
    return LayoutBoundingRegion(
        page=int(data.get("pageNumber", data.get("page", 0)) or 0),
        polygon=[float(v) for v in data.get("polygon", [])],
    )


def _parse_span(data: dict[str, Any]) -> LayoutSpan:
    return LayoutSpan(
        offset=int(data.get("offset", 0)),
        length=int(data.get("length", 0)),
    )


def _first_page(regions: list[LayoutBoundingRegion]) -> int | None:
    for region in regions:
        if region.page:
            return region.page
    return None


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("content") or value.get("text") or "")
    return str(value)


def _result_as_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "as_dict"):
        return result.as_dict()
    if isinstance(result, dict):
        return result
    return {}


def _result_id_from_poller(poller: Any) -> str | None:
    details = getattr(poller, "details", None)
    if isinstance(details, dict):
        op = details.get("operation-location") or details.get("operation_location")
        if isinstance(op, str) and "/analyzeResults/" in op:
            return op.rstrip("/").rsplit("/", 1)[-1]
    return None


def _load_json(path: Path) -> Any:
    return orjson.loads(path.read_bytes())


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return _load_json(path)
    except Exception as exc:
        log.debug("cache.read_failed path=%s error=%s", path, exc)
        return default


def _write_json(path: Path, data: Any) -> None:
    path.write_bytes(orjson.dumps(data, default=str, option=orjson.OPT_INDENT_2))


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]
