"""Render PDF pages to PNG images using pypdfium2."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pypdfium2 as pdfium
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

log = logging.getLogger(__name__)


class PdfPageRenderer:
    """Concrete adapter for :class:`PageRendererPort`."""

    name = "pypdfium2"

    async def render(self, pdf_path: Path, out_dir: Path, dpi: int = 150) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        scale = dpi / 72.0
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            total = len(pdf)
            log.info(
                "render.start pdf=%s pages=%d dpi=%d out=%s",
                pdf_path.name, total, dpi, out_dir,
            )
            t0 = time.perf_counter()
            with Progress(
                TextColumn("[bold blue]rendering pages"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                transient=True,
            ) as progress:
                task = progress.add_task("render", total=total)
                for i, page in enumerate(pdf, start=1):
                    pil = page.render(scale=scale).to_pil()
                    target = out_dir / f"page_{i:04d}.png"
                    pil.save(target)
                    outputs.append(target)
                    progress.advance(task)
            log.info(
                "render.complete pages=%d duration_ms=%.0f out=%s",
                total, (time.perf_counter() - t0) * 1000, out_dir,
            )
        finally:
            pdf.close()
        return outputs
