"""Figure extraction and multimodal interpretation pipeline."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from knowledge_extraction.application.ports import VisionPort
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.domain import (
    ChartInterpretation,
    Document,
    Figure,
    LayoutBoundingRegion,
    LayoutSpan,
)
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import RelationalRepository
from knowledge_extraction.infrastructure.telemetry.observability import wide_event

log = logging.getLogger(__name__)


@dataclass(slots=True)
class FigureSpec:
    source_id: str | None
    page: int
    caption: str = ""
    crop_box: tuple[float, float, float, float] | None = None
    source_image_path: Path | None = None
    bounding_regions: list[LayoutBoundingRegion] | None = None
    spans: list[LayoutSpan] | None = None
    elements: list[str] | None = None


class FigureInterpretationPipeline:
    PROMPT_NAME = "figure_interpretation"
    PROMPT_VERSION = "v1"

    def __init__(
        self,
        *,
        vision: VisionPort,
        prompts: PromptRegistry,
        repo: RelationalRepository,
        model: str,
        concurrency: int = 1,
    ) -> None:
        self._vision = vision
        self._prompts = prompts
        self._repo = repo
        self._model = model
        self._concurrency = max(1, concurrency)

    async def run(self, document: Document, pages_dir: Path, artifact_dir: Path) -> list[Figure]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        layout = self._load_layout(document.layout_json_path)
        specs = self._figure_specs_from_document(document, layout)
        if not specs:
            specs = self._figure_specs(layout)
        if not specs:
            specs = self._fallback_specs(document)

        with wide_event(
            "figures.pipeline",
            document_id=document.id,
            model=self._model,
            figure_specs=len(specs),
            concurrency=self._concurrency,
        ) as ev:
            # Crop pass (sequential, fast, disk-bound).
            figures: list[Figure] = []
            page_refs: dict[int, list[str]] = {}
            for index, spec in enumerate(specs, start=1):
                page_image = pages_dir / f"page_{spec.page:04d}.png"
                if not page_image.exists():
                    continue
                crop_path = artifact_dir / f"figure_{spec.page:04d}_{index:03d}.png"
                figure = self._crop_figure(document.id, spec, page_image, crop_path)
                figures.append(figure)
                page_refs.setdefault(figure.page, []).append(figure.id)
            ev["figures_cropped"] = len(figures)

            # Vision pass (bounded parallel, network-bound).
            interpretations = await self._interpret_all(figures)
            ev["figures_interpreted"] = sum(1 for v in interpretations.values() if v.title or v.interpretation)
            ev["figures_failed"] = sum(1 for v in interpretations.values() if not (v.title or v.interpretation))

            if figures:
                self._repo.save_figures(figures, interpretations, model=self._model)
                for chunk in self._repo.list_chunks():
                    refs = [fid for page in range(chunk.page_start, chunk.page_end + 1) for fid in page_refs.get(page, [])]
                    if refs:
                        self._repo.update_chunk_figure_refs(chunk.id, refs)
            ev["figures_saved"] = len(figures)
            return figures

    async def _interpret_all(self, figures: list[Figure]) -> dict[str, ChartInterpretation]:
        if not figures:
            return {}
        sem = asyncio.Semaphore(self._concurrency)
        completed = 0
        completed_lock = asyncio.Lock()

        async def _one(figure: Figure) -> tuple[str, ChartInterpretation]:
            nonlocal completed
            async with sem:
                prompt = self._prompts.render(
                    self.PROMPT_NAME, self.PROMPT_VERSION,
                    caption=figure.caption, page=figure.page,
                )
                try:
                    interp = await self._vision.interpret_figure(
                        figure, f"SYSTEM:\n{prompt.system}\n\nUSER:\n{prompt.user}",
                    )
                except Exception as exc:  # tolerate per-figure failures, continue pipeline
                    log.warning("vision.interpret_figure.failed figure=%s page=%d error=%s",
                                figure.id, figure.page, exc)
                    interp = ChartInterpretation(figure_id=figure.id)
                async with completed_lock:
                    completed += 1
                    if completed % 50 == 0 or completed == len(figures):
                        log.info("figures.progress %d/%d", completed, len(figures))
                return figure.id, interp

        pairs = await asyncio.gather(*(_one(f) for f in figures))
        return dict(pairs)

    def _crop_figure(self, document_id: str, spec: FigureSpec, page_image: Path, crop_path: Path) -> Figure:
        if spec.source_image_path is not None and spec.source_image_path.exists():
            shutil.copyfile(spec.source_image_path, crop_path)
        else:
            with Image.open(page_image) as img:
                crop_box = self._clamp_box(spec.crop_box, img.width, img.height)
                cropped = img.crop(crop_box) if crop_box else img.copy()
                cropped.save(crop_path)
        figure_id = hashlib.sha1(
            f"{document_id}:{spec.source_id or ''}:{spec.page}:{spec.caption}:{spec.crop_box}".encode()
        ).hexdigest()[:16]
        return Figure(
            id=figure_id,
            document_id=document_id,
            page=spec.page,
            caption=spec.caption,
            image_path=crop_path,
            crop_box=spec.crop_box,
            bounding_regions=spec.bounding_regions or [],
            spans=spec.spans or [],
            elements=spec.elements or [],
        )

    @staticmethod
    def _clamp_box(
        crop_box: tuple[float, float, float, float] | None,
        width: int,
        height: int,
    ) -> tuple[int, int, int, int] | None:
        if crop_box is None:
            return None
        x0, y0, x1, y1 = crop_box
        left = max(0, min(width, int(x0)))
        upper = max(0, min(height, int(y0)))
        right = max(left + 1, min(width, int(x1)))
        lower = max(upper + 1, min(height, int(y1)))
        return left, upper, right, lower

    def _figure_specs_from_document(
        self,
        document: Document,
        layout: dict[str, object],
    ) -> list[FigureSpec]:
        if not document.figures:
            return []
        pages = self._page_map(layout)
        specs: list[FigureSpec] = []
        for figure in document.figures:
            crop_box = self._regions_to_box(figure.page, figure.bounding_regions, pages)
            specs.append(
                FigureSpec(
                    source_id=figure.id,
                    page=figure.page,
                    caption=figure.caption,
                    crop_box=crop_box,
                    source_image_path=figure.image_path,
                    bounding_regions=list(figure.bounding_regions),
                    spans=list(figure.spans),
                    elements=list(figure.elements),
                )
            )
        return specs

    def _figure_specs(self, layout: dict[str, object]) -> list[FigureSpec]:
        pages = self._page_map(layout)
        raw_figures: object = layout.get("figures")
        if not isinstance(raw_figures, list) or not raw_figures:
            analyze_result = layout.get("analyzeResult")
            if isinstance(analyze_result, dict):
                raw_figures = analyze_result.get("figures") or []
        if not isinstance(raw_figures, list):
            raw_figures = []
        specs: list[FigureSpec] = []
        for raw in raw_figures:
            if not isinstance(raw, dict):
                continue
            page = self._figure_page(raw)
            if page is None:
                continue
            caption = self._caption(raw)
            regions = self._regions(raw)
            spans = self._spans(raw)
            crop_box = self._regions_to_box(page, regions, pages)
            specs.append(
                FigureSpec(
                    source_id=str(raw.get("id")) if raw.get("id") is not None else None,
                    page=page,
                    caption=caption,
                    crop_box=crop_box,
                    bounding_regions=regions,
                    spans=spans,
                    elements=[str(item) for item in raw.get("elements", []) if item is not None],
                )
            )
        return specs

    def _fallback_specs(self, document: Document) -> list[FigureSpec]:
        specs: list[FigureSpec] = []
        for page in document.pages:
            text = (page.text or "").lower()
            if any(token in text for token in ("figure", "fig.", "diagram", "chart")):
                specs.append(FigureSpec(source_id=None, page=page.number, caption=self._caption_from_text(page.text)))
        return specs

    @staticmethod
    def _caption_from_text(text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith(("figure", "fig.", "diagram", "chart")):
                return stripped[:240]
        return ""

    @staticmethod
    def _load_layout(path: Path | None) -> dict[str, object]:
        if path is None or not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _page_map(layout: dict[str, object]) -> dict[int, tuple[float | None, float | None, str | None]]:
        pages: dict[int, tuple[float | None, float | None, str | None]] = {}
        raw_pages = layout.get("pages")
        if not isinstance(raw_pages, list):
            return pages
        for raw in raw_pages:
            if not isinstance(raw, dict):
                continue
            page = raw.get("pageNumber") or raw.get("page_number") or raw.get("number")
            if page is None:
                continue
            try:
                pages[int(page)] = (
                    float(raw.get("width")) if raw.get("width") is not None else None,
                    float(raw.get("height")) if raw.get("height") is not None else None,
                    str(raw.get("unit")) if raw.get("unit") is not None else None,
                )
            except (TypeError, ValueError):
                continue
        return pages

    @staticmethod
    def _figure_page(raw: dict[str, object]) -> int | None:
        for key in ("pageNumber", "page_number", "page"):
            page = raw.get(key)
            if page is not None:
                try:
                    return int(page)
                except (TypeError, ValueError):
                    continue
        regions = raw.get("boundingRegions") or raw.get("bounding_regions") or []
        if isinstance(regions, list):
            for region in regions:
                if isinstance(region, dict):
                    page = region.get("pageNumber") or region.get("page_number")
                    if page is not None:
                        try:
                            return int(page)
                        except (TypeError, ValueError):
                            continue
        return None

    @staticmethod
    def _caption(raw: dict[str, object]) -> str:
        for key in ("caption", "text"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:240]
            if isinstance(value, dict):
                for nested in ("content", "text", "value"):
                    nested_value = value.get(nested)
                    if isinstance(nested_value, str) and nested_value.strip():
                        return nested_value.strip()[:240]
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        return item.strip()[:240]
                    if isinstance(item, dict):
                        for nested in ("content", "text"):
                            nested_value = item.get(nested)
                            if isinstance(nested_value, str) and nested_value.strip():
                                return nested_value.strip()[:240]
        return ""

    @staticmethod
    def _regions_to_box(
        page: int,
        regions: list[LayoutBoundingRegion],
        page_map: dict[int, tuple[float | None, float | None, str | None]],
    ) -> tuple[float, float, float, float] | None:
        if not regions:
            return None
        points: list[tuple[float, float]] = []
        for region in regions:
            if region.page and region.page != page:
                continue
            polygon = region.polygon
            if len(polygon) >= 8:
                pairs = [(float(polygon[i]), float(polygon[i + 1])) for i in range(0, len(polygon) - 1, 2)]
                points.extend(pairs)
        if not points:
            return None
        xs = [x for x, _ in points]
        ys = [y for _, y in points]
        width, height, _unit = page_map.get(page, (None, None, None))
        if _unit and str(_unit).lower().startswith("pixel"):
            return min(xs), min(ys), max(xs), max(ys)
        if width and height and max(xs) <= 1.5 and max(ys) <= 1.5:
            dpi = 150.0
            return min(xs) * width * dpi, min(ys) * height * dpi, max(xs) * width * dpi, max(ys) * height * dpi
        if width and height:
            dpi = 150.0
            return min(xs) * dpi, min(ys) * dpi, max(xs) * dpi, max(ys) * dpi
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def _regions(raw: dict[str, object]) -> list[LayoutBoundingRegion]:
        parsed: list[LayoutBoundingRegion] = []
        raw_regions = raw.get("boundingRegions") or raw.get("bounding_regions") or []
        if not isinstance(raw_regions, list):
            return parsed
        for region in raw_regions:
            if not isinstance(region, dict):
                continue
            page = region.get("pageNumber") or region.get("page_number") or region.get("page")
            try:
                page_number = int(page) if page is not None else 0
            except (TypeError, ValueError):
                page_number = 0
            polygon_raw = region.get("polygon") or region.get("boundingBox") or region.get("bounding_box") or []
            polygon: list[float] = []
            if isinstance(polygon_raw, list):
                coords = list(polygon_raw)
                if coords and isinstance(coords[0], dict):
                    coords = [
                        coord
                        for point in coords
                        if isinstance(point, dict)
                        for coord in (point.get("x"), point.get("y"))
                        if coord is not None
                    ]
                for coord in coords:
                    try:
                        polygon.append(float(coord))
                    except (TypeError, ValueError):
                        continue
            parsed.append(LayoutBoundingRegion(page=page_number, polygon=polygon))
        return parsed

    @staticmethod
    def _spans(raw: dict[str, object]) -> list[LayoutSpan]:
        parsed: list[LayoutSpan] = []
        raw_spans = raw.get("spans") or []
        if not isinstance(raw_spans, list):
            return parsed
        for span in raw_spans:
            if not isinstance(span, dict):
                continue
            try:
                parsed.append(
                    LayoutSpan(
                        offset=int(span.get("offset", 0)),
                        length=int(span.get("length", 0)),
                    )
                )
            except (TypeError, ValueError):
                continue
        return parsed
