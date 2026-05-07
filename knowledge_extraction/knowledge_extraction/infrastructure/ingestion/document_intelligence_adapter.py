"""Azure AI Document Intelligence adapter.

Produces a layout JSON sidecar and (optionally) a tables/figures inventory.
Requires AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT + key (or DefaultAzureCredential).
"""
from __future__ import annotations

import json
from pathlib import Path

from knowledge_extraction.config.settings import AzureAuthMode, get_settings
from knowledge_extraction.domain import Document, Page


class DocumentIntelligenceAdapter:
    name = "document_intelligence"

    async def ingest(self, pdf_path: Path, work_dir: Path) -> Document:
        work_dir.mkdir(parents=True, exist_ok=True)
        s = get_settings()
        if not s.azure_document_intelligence_endpoint:
            raise RuntimeError("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is not configured")

        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, ContentFormat

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

        with pdf_path.open("rb") as f:
            poller = client.begin_analyze_document(
                "prebuilt-layout",
                AnalyzeDocumentRequest(bytes_source=f.read()),
                output_content_format=ContentFormat.MARKDOWN,
            )
        result = poller.result()

        layout_path = work_dir / "layout.json"
        markdown_path = work_dir / "doc.md"
        layout_path.write_text(json.dumps(result.as_dict(), default=str), encoding="utf-8")
        markdown_path.write_text(getattr(result, "content", "") or "", encoding="utf-8")

        pages = [Page(number=i + 1, text="") for i in range(len(result.pages or []))]
        from knowledge_extraction.infrastructure.ingestion.docling_adapter import _hash_file

        return Document(
            id=_hash_file(pdf_path),
            title=pdf_path.stem,
            source_path=pdf_path,
            pages=pages,
            sections=[],
            markdown_path=markdown_path,
            layout_json_path=layout_path,
        )
