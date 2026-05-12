"""Best-effort structured table extraction from Document Intelligence layout JSON."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping

from knowledge_extraction.domain import LayoutBoundingRegion, LayoutSpan, Table, TableCell


def extract_tables_from_layout(layout: Mapping[str, object], *, document_id: str) -> list[Table]:
    tables: list[Table] = []
    for index, raw_table in enumerate(layout.get("tables", []) or []):
        if not isinstance(raw_table, Mapping):
            continue
        tables.append(_to_table(document_id=document_id, index=index, raw_table=raw_table))
    return tables


def _to_table(*, document_id: str, index: int, raw_table: Mapping[str, object]) -> Table:
    table_regions = _regions(raw_table.get("boundingRegions") or raw_table.get("bounding_regions"))
    pages = [r.page for r in table_regions if r.page > 0]
    page = min(pages) if pages else 1
    page_end = max(pages) if pages else page
    caption_raw = raw_table.get("caption")
    caption = _textish(caption_raw)
    caption_regions = _regions(
        caption_raw.get("boundingRegions") if isinstance(caption_raw, Mapping) else None
    ) or _regions(caption_raw.get("bounding_regions") if isinstance(caption_raw, Mapping) else None)
    caption_page = caption_regions[0].page if caption_regions else None
    cells = [_to_cell(raw_cell) for raw_cell in raw_table.get("cells", []) or [] if isinstance(raw_cell, Mapping)]
    table_id = _stable_table_id(document_id, index, page, caption, cells)
    return Table(
        id=table_id,
        document_id=document_id,
        page=page,
        page_end=page_end,
        caption=caption,
        caption_page=caption_page,
        markdown=_table_markdown(cells),
        cells=cells,
        bounding_regions=table_regions,
        spans=_spans(raw_table.get("spans")),
    )


def _to_cell(raw_cell: Mapping[str, object]) -> TableCell:
    regions = _regions(raw_cell.get("boundingRegions") or raw_cell.get("bounding_regions"))
    spans = _spans(raw_cell.get("spans"))
    page = regions[0].page if regions else None
    first_span = spans[0] if spans else None
    return TableCell(
        row=int(raw_cell.get("rowIndex", raw_cell.get("row_index", 0)) or 0),
        col=int(raw_cell.get("columnIndex", raw_cell.get("column_index", 0)) or 0),
        text=_textish(raw_cell.get("content", raw_cell.get("text", ""))),
        kind=_textish(raw_cell.get("kind", "")),
        row_span=int(raw_cell.get("rowSpan", raw_cell.get("row_span", 1)) or 1),
        col_span=int(raw_cell.get("columnSpan", raw_cell.get("column_span", 1)) or 1),
        page=page,
        span_start=first_span.offset if first_span else None,
        span_end=(first_span.offset + first_span.length) if first_span else None,
        bounding_regions=regions,
        spans=spans,
    )


def _table_markdown(cells: Iterable[TableCell]) -> str:
    grid: dict[int, dict[int, str]] = {}
    max_row = 0
    max_col = 0
    for cell in cells:
        grid.setdefault(cell.row, {})[cell.col] = cell.text
        max_row = max(max_row, cell.row)
        max_col = max(max_col, cell.col)
    if not grid:
        return ""
    rows: list[list[str]] = []
    for row_idx in range(max_row + 1):
        row = [grid.get(row_idx, {}).get(col_idx, "") for col_idx in range(max_col + 1)]
        rows.append(row)
    header = rows[0]
    body = rows[1:]
    lines = [f"| {' | '.join(header)} |", f"| {' | '.join(['---'] * len(header))} |"]
    for row in body:
        lines.append(f"| {' | '.join(row)} |")
    return "\n".join(lines)


def _stable_table_id(document_id: str, index: int, page: int, caption: str, cells: Iterable[TableCell]) -> str:
    digest = hashlib.sha1(
        f"{document_id}:{index}:{page}:{caption}:{'|'.join(c.text for c in cells)}".encode()
    ).hexdigest()[:16]
    return f"tbl:{digest}"


def _regions(raw: object) -> list[LayoutBoundingRegion]:
    if not isinstance(raw, list):
        return []
    out: list[LayoutBoundingRegion] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        out.append(
            LayoutBoundingRegion(
                page=int(item.get("pageNumber", item.get("page_number", item.get("page", 0))) or 0),
                polygon=[float(v) for v in item.get("polygon", []) or []],
            )
        )
    return out


def _spans(raw: object) -> list[LayoutSpan]:
    if not isinstance(raw, list):
        return []
    out: list[LayoutSpan] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        out.append(
            LayoutSpan(
                offset=int(item.get("offset", 0) or 0),
                length=int(item.get("length", 0) or 0),
            )
        )
    return out


def _textish(raw: object) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, Mapping):
        return _textish(raw.get("content", raw.get("text", "")))
    return ""
