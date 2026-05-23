from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from knowledge_extraction.cli.main import _resolve_ingest_sources


def test_ingest_defaults_to_all_assets_pdfs(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "b.pdf").write_bytes(b"%PDF-1.4\n")
    (assets / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (assets / "ignore.txt").write_text("x", encoding="utf-8")

    settings = SimpleNamespace(project_root=tmp_path)
    resolved = _resolve_ingest_sources(settings, None)

    assert [p.name for p in resolved] == ["a.pdf", "b.pdf"]


def test_ingest_accepts_single_pdf_path(tmp_path: Path) -> None:
    pdf = tmp_path / "custom.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    settings = SimpleNamespace(project_root=tmp_path)

    resolved = _resolve_ingest_sources(settings, pdf)

    assert resolved == [pdf]


def test_ingest_accepts_directory_path(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "x.pdf").write_bytes(b"%PDF-1.4\n")
    (docs / "y.pdf").write_bytes(b"%PDF-1.4\n")
    settings = SimpleNamespace(project_root=tmp_path)

    resolved = _resolve_ingest_sources(settings, docs)

    assert [p.name for p in resolved] == ["x.pdf", "y.pdf"]


def test_ingest_rejects_missing_path(tmp_path: Path) -> None:
    settings = SimpleNamespace(project_root=tmp_path)

    with pytest.raises(typer.BadParameter):
        _resolve_ingest_sources(settings, tmp_path / "missing.pdf")
