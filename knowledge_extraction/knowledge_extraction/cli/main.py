"""Typer CLI entrypoint — composition root."""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from knowledge_extraction.application.pipelines.stage_1_chunking import SemanticChunker
from knowledge_extraction.application.pipelines.stage_2b_extraction_discovery import (
    DiscoveryExtractionPipeline,
)
from knowledge_extraction.application.pipelines.stage_2a_extraction_governed import (
    GovernedExtractionPipeline,
)
from knowledge_extraction.application.pipelines.stage_3_semantic_clustering import SemanticClusterer
from knowledge_extraction.application.pipelines.stage_4_ontology_proposal import (
    OntologyProposalPipeline,
)
from knowledge_extraction.application.pipelines.stage_5_graph import GraphBuildPipeline
from knowledge_extraction.application.pipelines.orchestrator import Orchestrator
from knowledge_extraction.application.services.canonicalization_service import CanonicalizationService
from knowledge_extraction.application.services.drift_detector import DriftDetector
from knowledge_extraction.application.services.ontology_governance import OntologyGovernance
from knowledge_extraction.application.services.ontology_service import OntologyService
from knowledge_extraction.application.services.ontology_validator import OntologyValidator
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.config.settings import ExtractionMode, get_settings
from knowledge_extraction.infrastructure.checkpointing.filesystem_checkpoint_store import (
    FilesystemCheckpointStore,
)
from knowledge_extraction.infrastructure.ingestion.docling_adapter import DoclingIngestionAdapter
from knowledge_extraction.infrastructure.ingestion.pdf_renderer import PdfPageRenderer
from knowledge_extraction.infrastructure.llm.azure_foundry_client import AzureFoundryLLM
from knowledge_extraction.infrastructure.llm.embedding_adapter import AzureEmbeddingAdapter
from knowledge_extraction.infrastructure.persistence.graph.networkx_store import NetworkXGraphStore
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import (
    GovernanceRepository,
    RelationalRepository,
    make_engine,
    make_session_factory,
)
from knowledge_extraction.infrastructure.telemetry.observability import (
    bind,
    new_run_id,
    setup_logging,
    wide_event,
)
from knowledge_extraction.infrastructure.telemetry.otel_setup import setup_otel
from knowledge_extraction.tui.events import EventBus

app = typer.Typer(no_args_is_help=True, help="Knowledge extraction & ontology governance CLI.")
ontology_app = typer.Typer(help="Ontology governance.")
graphrag_app = typer.Typer(help="Microsoft GraphRAG integration.")
app.add_typer(ontology_app, name="ontology")
app.add_typer(graphrag_app, name="graphrag")
console = Console()


def _bootstrap() -> tuple:
    import atexit
    import time as _time

    settings = get_settings()
    settings.ensure_dirs()
    run_id = new_run_id()
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    argv = " ".join(sys.argv[1:])
    log_path = setup_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        run_id=run_id,
        console_format=settings.log_console_format,
    )
    bind(run_id=run_id, command=command)
    if log_path is not None:
        bind(log_file=str(log_path))

    # Emit lifecycle bookends so timings can be reconstructed post-hoc.
    started_wall = _time.time()
    started_perf = _time.perf_counter()
    log = logging.getLogger("ke")
    log.info("run.start", extra={"event": "run.start", "argv": argv,
                                 "started_at": datetime.fromtimestamp(started_wall, UTC).isoformat()})

    @atexit.register
    def _emit_run_finish() -> None:
        import contextlib

        elapsed_ms = int((_time.perf_counter() - started_perf) * 1000)
        log.info("run.finish", extra={
            "event": "run.finish",
            "duration_ms": elapsed_ms,
            "status": "ok",
            "log_file": str(log_path) if log_path else None,
        })
        for h in list(logging.getLogger().handlers):
            with contextlib.suppress(Exception):
                h.flush()

    setup_otel(settings.otel_enabled, settings.otel_exporter_otlp_endpoint or None)
    engine = make_engine(settings.sqlite_path)
    sf = make_session_factory(engine)
    relational = RelationalRepository(sf)
    governance = GovernanceRepository(sf)
    onto_service = OntologyService(governance, settings.ontology_yaml_path)
    with wide_event("ontology.bootstrap"):
        onto_service.bootstrap()
    return settings, relational, governance, onto_service


_MODE_OPT = typer.Option(None, help="discovery | governed (defaults to settings)")
_PAGES_OPT = typer.Option(None, help="limit to first N pages for smoke runs")
_VERSION_OPT = typer.Option(None, help="explicit ontology version")
_FRESH_OPT = typer.Option(False, "--fresh", help="ignore checkpoints and re-run every stage")
_REASON_OPT = typer.Option(..., help="rejection reason")
_BASE_OPT = typer.Option("", help="base version")


@app.command()
def ingest(pdf: Path, pages: int | None = _PAGES_OPT) -> None:
    """Ingest a PDF (layout + page images) and persist artifacts."""
    settings, relational, _, _ = _bootstrap()
    bus = EventBus()
    asyncio.run(_ingest_only(settings, relational, bus, pdf, pages))


async def _ingest_only(settings, relational, bus, pdf: Path, pages_limit: int | None = None) -> None:
    docling = DoclingIngestionAdapter()
    renderer = PdfPageRenderer()
    work_dir = settings.artifact_path / pdf.stem
    work_dir.mkdir(parents=True, exist_ok=True)

    source_pdf = pdf
    if pages_limit:
        source_pdf = _slice_pdf(pdf, work_dir / f"{pdf.stem}.first{pages_limit}.pdf", pages_limit)
        console.print(f"[yellow]sliced[/yellow] first {pages_limit} pages -> {source_pdf.name}")

    document = await docling.ingest(source_pdf, work_dir)
    relational.save_document(document)
    await renderer.render(source_pdf, work_dir / "pages", dpi=150)
    console.print(f"[green]Ingested[/green] {pdf.name} -> {document.id} ({document.page_count} pages)")


def _slice_pdf(src: Path, dst: Path, pages: int) -> Path:
    import pypdfium2 as pdfium

    src_doc = pdfium.PdfDocument(str(src))
    out = pdfium.PdfDocument.new()
    n = min(pages, len(src_doc))
    out.import_pages(src_doc, list(range(n)))
    out.save(str(dst))
    src_doc.close()
    return dst


@app.command()
def extract(
    pdf: Path,
    mode: ExtractionMode = _MODE_OPT,
    pages: int | None = _PAGES_OPT,
    ontology_version: str | None = _VERSION_OPT,
    fresh: bool = _FRESH_OPT,
) -> None:
    """Run end-to-end ingest -> chunk -> extract -> graph build for a PDF."""
    settings, relational, governance, onto_service = _bootstrap()
    selected_mode = mode or settings.default_mode
    bus = EventBus()
    asyncio.run(_run_extract(settings, relational, governance, onto_service,
                              bus, pdf, selected_mode, pages, ontology_version,
                              resume=not fresh))


async def _run_extract(settings, relational, governance, onto_service, bus, pdf,
                        selected_mode: ExtractionMode, pages_limit: int | None,
                        ontology_version: str | None, *, resume: bool = True) -> None:
    from knowledge_extraction.infrastructure.telemetry.observability import bind, wide_event

    bind(mode=selected_mode.value, pdf=pdf.name, pages_limit=pages_limit or 0)
    docling = DoclingIngestionAdapter()
    renderer = PdfPageRenderer()
    chunker = SemanticChunker()
    work_dir = settings.artifact_path / pdf.stem
    pages_dir = work_dir / "pages"
    work_dir.mkdir(parents=True, exist_ok=True)
    checkpoints = FilesystemCheckpointStore(settings.checkpoint_path)

    # Slice the PDF up front when --pages is provided so Docling only sees the slice.
    source_pdf = pdf
    if pages_limit:
        with wide_event("ingest.slice_pdf", source=str(pdf), pages=pages_limit) as ev:
            source_pdf = _slice_pdf(pdf, work_dir / f"{pdf.stem}.first{pages_limit}.pdf", pages_limit)
            ev["destination"] = str(source_pdf)
            ev["bytes"] = source_pdf.stat().st_size
        console.print(f"[yellow]sliced[/yellow] first {pages_limit} pages -> {source_pdf.name}")

    with wide_event("ingest.docling", source=str(source_pdf)) as ev:
        document = await docling.ingest(source_pdf, work_dir)
        ev["document_id"] = document.id
        ev["pages"] = document.page_count
        ev["markdown_chars"] = (document.markdown_path or work_dir / "doc.md").stat().st_size
    relational.save_document(document)
    bind(document_id=document.id)

    markdown = (document.markdown_path or work_dir / "doc.md").read_text(encoding="utf-8")

    with wide_event("chunk.semantic") as ev:
        _sections, chunks = chunker.chunk(document, markdown)
        ev["chunks"] = len(chunks)
        ev["sections"] = len(_sections)
    relational.save_chunks(chunks)

    llm = AzureFoundryLLM(settings)
    prompts = PromptRegistry(settings.prompts_dir)
    version, schema = onto_service.active(ontology_version)
    bind(ontology_version=version.version)

    orchestrator = Orchestrator(document.id, checkpoints, bus)

    async def stage_render() -> None:
        await renderer.render(source_pdf, pages_dir, dpi=150)

    results: list = []
    if selected_mode is ExtractionMode.GOVERNED:
        validator = OntologyValidator(schema)
        canonicalizer = CanonicalizationService(governance)
        drift = DriftDetector(governance, version.version)
        governed = GovernedExtractionPipeline(
            llm=llm, prompts=prompts, validator=validator, canonicalizer=canonicalizer,
            drift=drift, repo=relational, schema=schema,
            model=settings.azure_openai_extraction_model,
        )

        async def stage_extract() -> None:
            nonlocal results
            results = await governed.run(document.title, chunks)
            from knowledge_extraction.tui.events import PipelineEvent
            bus.publish(PipelineEvent("governed", "governed", {
                "canonical_reused": governed.stats.canonical_reused,
                "violations_prevented": governed.stats.violations_prevented,
                "entities_unknown": governed.stats.entities_unknown,
                "refinements_queued": len(governed.stats.refinement_suggestions),
            }))
    else:
        discovery = DiscoveryExtractionPipeline(
            llm=llm, prompts=prompts, repo=relational,
            model=settings.azure_openai_reasoning_model,
        )
        embeddings = AzureEmbeddingAdapter(settings)
        clusterer = SemanticClusterer(embeddings, model=settings.azure_openai_embedding_model)
        proposer = OntologyProposalPipeline(
            llm=llm, prompts=prompts, gov=governance,
            model=settings.azure_openai_reasoning_model,
            artifact_dir=work_dir / "ontology_candidates",
        )

        async def stage_extract() -> None:
            findings = await discovery.run(document.title, chunks)
            clusters = await clusterer.cluster(
                [n for names in findings.entity_examples.values() for n in names]
            )
            proposal = await proposer.propose(findings, clusters, base_version=version.version)
            from knowledge_extraction.tui.events import PipelineEvent
            bus.publish(PipelineEvent("discovery", "discovery", {
                "new_entity_types_proposed": len(findings.entity_type_counter),
                "new_relationship_types_proposed": len(findings.relationship_type_counter),
                "semantic_clusters_detected": len(clusters),
                "proposal_id": proposal.id or 0,
            }))

    graph = NetworkXGraphStore()
    graph_pipeline = GraphBuildPipeline(graph)

    async def stage_graph() -> None:
        stats = graph_pipeline.build(results)
        tag = version.version
        graph.export_graphml(settings.graph_storage_path / f"{document.id}.{tag}.graphml")
        graph.export_jsonld(settings.graph_storage_path / f"{document.id}.{tag}.jsonld")
        graph.export_cypher(settings.graph_storage_path / f"{document.id}.{tag}.cypher")
        from knowledge_extraction.tui.events import PipelineEvent
        bus.publish(PipelineEvent("graph", "metric", {**stats, "ontology_version": tag}))

    orchestrator.add("render", stage_render)
    orchestrator.add("extract", stage_extract, deps=["render"])
    if selected_mode is ExtractionMode.GOVERNED:
        orchestrator.add("graph", stage_graph, deps=["extract"])

    await orchestrator.run(resume=resume)

    console.print("[green]extract complete[/green]")


@app.command(name="graph")
def graph_cmd(action: str = typer.Argument(..., help="build")) -> None:
    """Graph utilities (build is run automatically by `extract`)."""
    if action != "build":
        raise typer.BadParameter("only 'build' is supported standalone (use `extract` for end-to-end)")
    console.print("[yellow]use `ke extract <pdf>` for end-to-end build[/yellow]")


@app.command()
def resume(
    pdf: Path,
    mode: ExtractionMode = ExtractionMode.GOVERNED,
    pages: int | None = _PAGES_OPT,
) -> None:
    """Re-run extraction; checkpointed stages are skipped."""
    extract(pdf=pdf, mode=mode, pages=pages, ontology_version=None, fresh=False)


@app.command()
def clean(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip confirmation prompt"),
) -> None:
    """Remove ALL derived state so the next run starts from scratch.

    Wipes every configured path (artifacts, checkpoints, SQLite DB, logs,
    graph exports, vector store, ontology candidates) plus the default
    work/ folder as a catch-all. Source PDFs in assets/ and the ontology
    config are NOT touched.
    """
    import shutil

    settings = get_settings()
    root = settings.project_root

    def _resolve(p: Path) -> Path:
        return p if p.is_absolute() else (root / p).resolve()

    targets: list[Path] = []
    seen: set[Path] = set()
    for path in [
        settings.artifact_path,
        settings.checkpoint_path,
        settings.graph_storage_path,
        settings.vector_db_path,
        settings.log_dir,
        settings.sqlite_path,
        root / "work",
    ]:
        resolved = _resolve(path)
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        targets.append(resolved)

    if not targets:
        console.print("[yellow]nothing to clean[/yellow]")
        return

    total_bytes = 0
    for t in targets:
        if t.is_file():
            total_bytes += t.stat().st_size
        else:
            total_bytes += sum(p.stat().st_size for p in t.rglob("*") if p.is_file())
    size_mb = total_bytes / (1024 * 1024)

    console.print(f"will remove [red]{len(targets)} target(s)[/red] ({size_mb:.1f} MB):")
    for t in targets:
        kind = "file" if t.is_file() else "dir "
        console.print(f"  [dim]{kind}[/dim] {t}")
    if not yes and not typer.confirm("proceed?", default=False):
        console.print("[yellow]aborted[/yellow]")
        raise typer.Exit(code=1)

    for t in targets:
        try:
            if t.is_file():
                t.unlink()
            else:
                shutil.rmtree(t)
            console.print(f"[green]removed[/green] {t}")
        except OSError as exc:
            console.print(f"[red]failed[/red] {t}: {exc}")


@app.command()
def stats() -> None:
    """Print persistence + governance + drift stats."""
    _, relational, governance, onto_service = _bootstrap()
    s = relational.stats()
    table = Table(title="Knowledge Extraction Stats")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for k, v in s.items():
        table.add_row(k, str(v))
    console.print(table)
    versions = governance.list_versions()
    vt = Table(title="Ontology Versions")
    vt.add_column("version")
    vt.add_column("status")
    for v in versions:
        vt.add_row(v.version, v.status.value)
    console.print(vt)
    active = onto_service.active()[0]
    drift = governance.drift_summary(active.version)
    if drift:
        dt = Table(title=f"Drift @ {active.version}")
        dt.add_column("kind")
        dt.add_column("count", justify="right")
        for k, v in drift.items():
            dt.add_row(k, str(v))
        console.print(dt)


@app.command()
def tour(
    port: int = typer.Option(8501, help="Port for Streamlit"),
    host: str = typer.Option("localhost", help="Host"),
) -> None:
    """Launch the interactive Streamlit pipeline tour."""
    import subprocess

    try:
        import streamlit  # noqa: F401
    except ImportError:
        console.print(
            "[red]Streamlit not installed.[/red] Run: "
            "[cyan]uv pip install -e \".[tour]\"[/cyan] "
            "or [cyan]uv sync --extra tour[/cyan]"
        )
        raise typer.Exit(code=1) from None

    app_path = Path(__file__).parent / "tour_app.py"
    if not app_path.exists():
        # Fall back to importlib resolution if __file__ is stale (e.g. after
        # an editable-install layout change).
        import importlib.util
        spec = importlib.util.find_spec("knowledge_extraction.cli.tour_app")
        if spec and spec.origin:
            app_path = Path(spec.origin)
    if not app_path.exists():
        console.print(f"[red]tour_app.py not found at {app_path}[/red]")
        raise typer.Exit(code=1)
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.port", str(port),
        "--server.address", host,
        "--browser.gatherUsageStats", "false",
    ]
    console.print(f"[green]Launching tour:[/green] http://{host}:{port}")
    subprocess.run(cmd, check=False)


# ---- ontology subcommands ----

@ontology_app.command("list")
def onto_list() -> None:
    _, _, governance, _ = _bootstrap()
    table = Table(title="Ontology")
    table.add_column("kind")
    table.add_column("id/version")
    table.add_column("status")
    table.add_column("source")
    for v in governance.list_versions():
        table.add_row("version", v.version, v.status.value, v.approved_by or "")
    for p in governance.list_proposals():
        table.add_row("proposal", str(p.id), p.status.value, p.source_mode.value)
    console.print(table)


@ontology_app.command("show")
def onto_show(version: str) -> None:
    _, _, governance, _ = _bootstrap()
    v = governance.get_version(version)
    if v is None:
        raise typer.BadParameter(f"version {version} not found")
    console.print(v.schema_yaml)


@ontology_app.command("diff")
def onto_diff(a: str, b: str) -> None:
    _, _, governance, _ = _bootstrap()
    gov = OntologyGovernance(governance)
    console.print(gov.diff(a, b))


@ontology_app.command("approve")
def onto_approve(proposal_id: int, by: str = "cli") -> None:
    _, _, governance, _ = _bootstrap()
    gov = OntologyGovernance(governance)
    v = gov.approve(proposal_id, approved_by=by)
    console.print(f"[green]approved[/green] proposal {proposal_id} as ontology {v.version}")


@ontology_app.command("reject")
def onto_reject(proposal_id: int, reason: str = _REASON_OPT) -> None:
    _, _, governance, _ = _bootstrap()
    OntologyGovernance(governance).reject(proposal_id, reason)
    console.print(f"[yellow]rejected[/yellow] {proposal_id}: {reason}")


@ontology_app.command("propose")
def onto_propose(yaml_file: Path, base: str = _BASE_OPT) -> None:
    from knowledge_extraction.domain.ontology import OntologyProposalSource

    _, _, governance, _ = _bootstrap()
    gov = OntologyGovernance(governance)
    p = gov.propose_from_yaml(yaml_file.read_text(encoding="utf-8"), base or None,
                              OntologyProposalSource.GOVERNED_REFINEMENT)
    console.print(f"[green]proposal[/green] id={p.id}")


@ontology_app.command("migrate")
def onto_migrate(from_version: str, to_version: str) -> None:
    """Migrate existing graph nodes from one ontology version to another."""
    settings, _, governance, _ = _bootstrap()
    from knowledge_extraction.application.services.ontology_migration import OntologyMigrationService
    from knowledge_extraction.infrastructure.persistence.sqlite.repositories import (
        make_engine,
        make_session_factory,
    )

    engine = make_engine(settings.sqlite_path)
    sf = make_session_factory(engine)
    svc = OntologyMigrationService(governance, sf)
    report = svc.apply(from_version, to_version)
    console.print(
        f"[cyan]migration[/cyan] {from_version} -> {to_version}: "
        f"{len(report.type_renames)} type renames, {report.relabeled_entities} entities relabeled."
    )
    for old_t, new_t in report.type_renames.items():
        console.print(f"  {old_t} -> {new_t}")


@graphrag_app.command("index")
def graphrag_index() -> None:
    """Run Microsoft GraphRAG indexing on extracted artifacts."""
    from knowledge_extraction.infrastructure.graphrag.graphrag_runner import GraphRagRunner

    settings, relational, _governance, onto_service = _bootstrap()
    version = onto_service.active()[0]
    chunks = relational.list_chunks()
    if not chunks:
        console.print("[yellow]No chunks in the relational store yet — run `ke extract` first.[/yellow]")
        raise typer.Exit(code=2)
    runner = GraphRagRunner(Path("./work/graphrag"), settings)
    with wide_event("graphrag.write_inputs", chunks=len(chunks), version=version.version):
        runner.write_inputs(version, chunks)
    with wide_event("graphrag.index", version=version.version) as ev:
        code = asyncio.run(runner.index(version))
        ev["exit_code"] = code
    if code != 0:
        console.print(f"[red]graphrag index failed (exit {code}). Inspect logs in work/graphrag/{version.version}/logs[/red]")
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
