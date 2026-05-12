"""Run the end-to-end PDF -> knowledge graph extraction.

This is *the* pipeline. Read top-to-bottom and you've understood the system::

      slice (optional) ─► ingest ─► chunk ─► render ─► figures ─► extract ─► graph
                                                       (vision)   (LLM)     (governed)

Every stage is registered with the :class:`Orchestrator`, which:
  * Runs them in dependency order
  * Skips stages whose checkpoint marker already exists (resume)
  * Wraps each stage in a `wide_event` + OTEL span (token + heartbeat metrics)

This module deliberately knows nothing about CLI/MCP/HTTP. Compose it from
:mod:`knowledge_extraction.cli.main` (or any future interface adapter).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from knowledge_extraction.application.pipelines.orchestrator import Orchestrator
from knowledge_extraction.application.pipelines.stage_1_chunking import SemanticChunker
from knowledge_extraction.application.pipelines.stage_2a_extraction_governed import (
    GovernedExtractionPipeline,
)
from knowledge_extraction.application.pipelines.stage_2b_extraction_discovery import (
    DiscoveryExtractionPipeline,
)
from knowledge_extraction.application.pipelines.stage_2c_figure_interpretation import (
    FigureInterpretationPipeline,
)
from knowledge_extraction.application.pipelines.stage_3_semantic_clustering import SemanticClusterer
from knowledge_extraction.application.pipelines.stage_4_ontology_proposal import (
    OntologyProposalPipeline,
)
from knowledge_extraction.application.pipelines.stage_5_graph import GraphBuildPipeline
from knowledge_extraction.application.pipelines.stages import (
    PIPELINE_DISCOVERY,
    PIPELINE_GOVERNED,
    Stage,
    parse_stage,
)
from knowledge_extraction.application.ports import (
    CheckpointPort,
    EmbeddingPort,
    GraphStorePort,
    IngestionPort,
    LLMPort,
    PageRendererPort,
    RelationalStorePort,
    VisionPort,
)
from knowledge_extraction.application.services.canonicalization_service import CanonicalizationService
from knowledge_extraction.application.services.drift_detector import DriftDetector
from knowledge_extraction.application.services.ontology_governance import OntologyGovernance
from knowledge_extraction.application.services.ontology_service import OntologyService
from knowledge_extraction.application.services.ontology_validator import OntologyValidator
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.config.settings import ExtractionMode, Settings
from knowledge_extraction.domain import Document, ExtractionResult, Figure
from knowledge_extraction.infrastructure.telemetry.observability import bind, wide_event
from knowledge_extraction.tui.events import EventBus, PipelineEvent

log = logging.getLogger(__name__)


# ---------- request / result objects ----------

@dataclass(slots=True)
class ExtractionRequest:
    """Inputs for a single end-to-end run."""

    pdf: Path
    mode: ExtractionMode
    pages_limit: int | None = None
    ontology_version: str | None = None
    resume: bool = True
    redo_stage: str | None = None


@dataclass(slots=True)
class ExtractionRunResult:
    """Outputs surfaced back to the caller (CLI/MCP/API)."""

    document_id: str
    page_count: int
    chunk_count: int
    extraction_results: list[ExtractionResult] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)


# ---------- service bag (composition root provides this) ----------

@dataclass(slots=True)
class ExtractionServices:
    """Everything the use case needs, supplied by the composition root."""

    settings: Settings
    relational: RelationalStorePort
    governance: OntologyGovernance
    onto_service: OntologyService

    # adapters
    ingestion_chain: list[IngestionPort]
    renderer: PageRendererPort
    llm: LLMPort
    vision: VisionPort
    embeddings: EmbeddingPort
    graph_store: GraphStorePort
    checkpoints: CheckpointPort

    # collaborators
    chunker: SemanticChunker
    prompts: PromptRegistry
    bus: EventBus


# ---------- the use case ----------

class RunExtractionUseCase:
    """Owns the end-to-end pipeline graph and runs it via the Orchestrator."""

    def __init__(self, services: ExtractionServices) -> None:
        self._svc = services

    async def execute(self, request: ExtractionRequest) -> ExtractionRunResult:
        svc = self._svc
        bind(mode=request.mode.value, pdf=request.pdf.name, pages_limit=request.pages_limit or 0)

        work_dir = svc.settings.artifact_path / request.pdf.stem
        pages_dir = work_dir / "pages"
        work_dir.mkdir(parents=True, exist_ok=True)

        source_pdf = self._maybe_slice(request.pdf, request.pages_limit, work_dir)
        document = await self._ingest(source_pdf, work_dir)
        svc.relational.save_document(document)
        bind(document_id=document.id)

        if request.redo_stage:
            self._cascade_redo(document.id, request.mode, parse_stage(request.redo_stage))

        chunks = self._chunk(document, work_dir)
        svc.relational.save_chunks(chunks)

        # Active ontology drives both schema-governed extraction and graph tagging.
        version, schema = svc.onto_service.active(request.ontology_version)
        bind(ontology_version=version.version)

        # Mutable handoff between stage closures (results -> graph, figures -> extract).
        results: list[ExtractionResult] = []
        figures: list[Figure] = []

        figures_pipeline = FigureInterpretationPipeline(
            vision=svc.vision, prompts=svc.prompts, repo=svc.relational,
            model=svc.settings.azure_openai_vision_model,
            concurrency=svc.settings.pipeline_concurrency,
        )

        async def stage_render() -> None:
            await svc.renderer.render(source_pdf, pages_dir, dpi=150)

        async def stage_figures() -> None:
            nonlocal figures, chunks
            figures = await figures_pipeline.run(document, pages_dir, work_dir / "figures")
            chunks = svc.relational.list_chunks()
            svc.bus.publish(PipelineEvent("figures", "metric", {"figures_extracted": len(figures)}))

        if request.mode is ExtractionMode.GOVERNED:
            governed = GovernedExtractionPipeline(
                llm=svc.llm, prompts=svc.prompts,
                validator=OntologyValidator(schema),
                canonicalizer=CanonicalizationService(svc.governance),
                drift=DriftDetector(svc.governance, version.version),
                repo=svc.relational, schema=schema,
                model=svc.settings.azure_openai_extraction_model,
                concurrency=svc.settings.pipeline_concurrency,
            )

            async def stage_extract() -> None:
                nonlocal results
                results = await governed.run(document.title, chunks, document.tables, figures)
                svc.bus.publish(PipelineEvent("governed", "governed", {
                    "canonical_reused": governed.stats.canonical_reused,
                    "violations_prevented": governed.stats.violations_prevented,
                    "entities_unknown": governed.stats.entities_unknown,
                    "refinements_queued": len(governed.stats.refinement_suggestions),
                }))

            graph_pipeline = GraphBuildPipeline(svc.graph_store)

            async def stage_graph() -> None:
                if not results:
                    log.info("graph stage skipped: no extraction results in this run")
                    return
                stats = graph_pipeline.build(results)
                tag = version.version
                out = svc.settings.graph_storage_path
                svc.graph_store.export_graphml(out / f"{document.id}.{tag}.graphml")
                svc.graph_store.export_jsonld(out / f"{document.id}.{tag}.jsonld")
                svc.graph_store.export_cypher(out / f"{document.id}.{tag}.cypher")
                svc.bus.publish(PipelineEvent("graph", "metric", {**stats, "ontology_version": tag}))
        else:
            discovery = DiscoveryExtractionPipeline(
                llm=svc.llm, prompts=svc.prompts, repo=svc.relational,
                model=svc.settings.azure_openai_reasoning_model,
            )
            clusterer = SemanticClusterer(svc.embeddings, model=svc.settings.azure_openai_embedding_model)
            proposer = OntologyProposalPipeline(
                llm=svc.llm, prompts=svc.prompts, gov=svc.governance,
                model=svc.settings.azure_openai_reasoning_model,
                artifact_dir=work_dir / "ontology_candidates",
            )

            async def stage_extract() -> None:
                findings = await discovery.run(document.title, chunks)
                clusters = await clusterer.cluster(
                    [n for names in findings.entity_examples.values() for n in names]
                )
                proposal = await proposer.propose(findings, clusters, base_version=version.version)
                svc.bus.publish(PipelineEvent("discovery", "discovery", {
                    "new_entity_types_proposed": len(findings.entity_type_counter),
                    "new_relationship_types_proposed": len(findings.relationship_type_counter),
                    "semantic_clusters_detected": len(clusters),
                    "proposal_id": proposal.id or 0,
                }))

            stage_graph = None  # discovery mode has no graph stage

        orchestrator = Orchestrator(document.id, svc.checkpoints, svc.bus)
        orchestrator.add(Stage.RENDER.value, stage_render)
        orchestrator.add(Stage.FIGURES.value, stage_figures, deps=[Stage.RENDER.value])
        orchestrator.add(Stage.EXTRACT.value, stage_extract,
                          deps=[Stage.RENDER.value, Stage.FIGURES.value])
        if stage_graph is not None:
            orchestrator.add(Stage.GRAPH.value, stage_graph, deps=[Stage.EXTRACT.value])

        await orchestrator.run(resume=request.resume)

        return ExtractionRunResult(
            document_id=document.id,
            page_count=document.page_count,
            chunk_count=len(chunks),
            extraction_results=results,
            figures=figures,
        )

    # ---------- helpers (intentionally tiny, named for the workflow) ----------

    def _maybe_slice(self, pdf: Path, limit: int | None, work_dir: Path) -> Path:
        return slice_pdf_if_requested(pdf, limit, work_dir)

    async def _ingest(self, source_pdf: Path, work_dir: Path) -> Document:
        return await pick_first_working_ingestion(self._svc.ingestion_chain, source_pdf, work_dir)

    def _chunk(self, document: Document, work_dir: Path) -> list:
        markdown_path = document.markdown_path or work_dir / "doc.md"
        markdown = markdown_path.read_text(encoding="utf-8")
        with wide_event("chunk.semantic") as ev:
            sections, chunks = self._svc.chunker.chunk(document, markdown)
            ev["chunks"] = len(chunks)
            ev["sections"] = len(sections)
        return chunks

    def _cascade_redo(self, document_id: str, mode: ExtractionMode, from_stage: Stage) -> None:
        ordered = PIPELINE_GOVERNED if mode is ExtractionMode.GOVERNED else PIPELINE_DISCOVERY
        if from_stage not in ordered:
            allowed = ", ".join(s.value for s in ordered)
            raise ValueError(
                f"--redo-stage '{from_stage.value}' not valid for {mode.value} mode (allowed: {allowed})"
            )
        downstream = [s.value for s in ordered[ordered.index(from_stage):]]
        self._svc.checkpoints.clear_from(document_id, downstream)
        log.info("cleared checkpoints from %s onward (%s)", from_stage.value, downstream)


# ---------- public helpers (also used by the `ke ingest` CLI command) ----------

def slice_pdf_if_requested(pdf: Path, limit: int | None, work_dir: Path) -> Path:
    """Return the first ``limit`` pages of ``pdf`` (or ``pdf`` unchanged)."""
    if not limit:
        return pdf
    with wide_event("ingest.slice_pdf", source=str(pdf), pages=limit) as ev:
        sliced = _slice_pdf(pdf, work_dir / f"{pdf.stem}.first{limit}.pdf", limit)
        ev["destination"] = str(sliced)
        ev["bytes"] = sliced.stat().st_size
    return sliced


async def pick_first_working_ingestion(
    chain: list[IngestionPort], source_pdf: Path, work_dir: Path,
) -> Document:
    """Try each ingestion adapter in order; return the first success."""
    last_error: Exception | None = None
    for adapter in chain:
        try:
            document = await adapter.ingest(source_pdf, work_dir)
            if not document.layout_source:
                document.layout_source = adapter.name
            with wide_event(f"ingest.{document.layout_source or 'unknown'}",
                             source=str(source_pdf)) as ev:
                ev["document_id"] = document.id
                ev["pages"] = document.page_count
                md_path = document.markdown_path or work_dir / "doc.md"
                if md_path.exists():
                    ev["markdown_chars"] = md_path.stat().st_size
            return document
        except Exception as exc:  # try next adapter; preserve final error
            last_error = exc
            log.warning("ingest.adapter.failed adapter=%s error=%s", adapter.name, exc)
    assert last_error is not None
    raise last_error


def _slice_pdf(src: Path, dst: Path, pages: int) -> Path:
    import pypdfium2 as pdfium

    src_doc = pdfium.PdfDocument(str(src))
    out = pdfium.PdfDocument.new()
    n = min(pages, len(src_doc))
    out.import_pages(src_doc, list(range(n)))
    out.save(str(dst))
    src_doc.close()
    return dst
