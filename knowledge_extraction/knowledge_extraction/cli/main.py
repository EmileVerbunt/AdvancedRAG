"""Typer CLI entrypoint — composition root.

Wiring only. The actual pipeline lives in
:mod:`knowledge_extraction.application.use_cases.run_extraction`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from knowledge_extraction.application.pipelines.stage_1_chunking import SemanticChunker
from knowledge_extraction.application.pipelines.stages import Stage
from knowledge_extraction.application.services.graphrag_agent import MiniGraphRagAgent
from knowledge_extraction.application.services.graphrag_eval import (
    GraphRagEvalCase,
    aggregate_results,
    evaluate_case,
)
from knowledge_extraction.application.services.ms_graphrag_agent import (
    IndexNotFoundError,
    MsGraphRagAgent,
    graphrag_index_available,
)
from knowledge_extraction.application.services.ontology_governance import OntologyGovernance
from knowledge_extraction.application.services.ontology_service import OntologyService
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.application.services.query_rewriter import (
    LexicalQueryRewriter,
    LlmQueryRewriter,
    QueryRewriter,
)
from knowledge_extraction.application.use_cases.run_extraction import (
    ExtractionRequest,
    ExtractionServices,
    RunExtractionUseCase,
    pick_first_working_ingestion,
    slice_pdf_if_requested,
)
from knowledge_extraction.config.settings import ExtractionMode, get_settings
from knowledge_extraction.infrastructure.checkpointing.filesystem_checkpoint_store import (
    FilesystemCheckpointStore,
)
from knowledge_extraction.infrastructure.ingestion.docling_adapter import DoclingIngestionAdapter
from knowledge_extraction.infrastructure.ingestion.document_intelligence_adapter import (
    DocumentIntelligenceAdapter,
)
from knowledge_extraction.infrastructure.ingestion.pdf_renderer import PdfPageRenderer
from knowledge_extraction.infrastructure.llm.azure_foundry_client import AzureFoundryLLM
from knowledge_extraction.infrastructure.llm.embedding_adapter import AzureEmbeddingAdapter
from knowledge_extraction.infrastructure.llm.vision_adapter import AzureVisionAdapter
from knowledge_extraction.infrastructure.persistence.graph.networkx_store import NetworkXGraphStore
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import (
    GovernanceRepository,
    RelationalRepository,
    make_engine,
    make_session_factory,
)
from knowledge_extraction.infrastructure.telemetry.observability import (
    bind,
    configure_observability,
    get_run_token_totals,
    new_run_id,
    reset_run_token_totals,
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

# Force UTF-8 on stdout/stderr so Rich + plain prints can render answers containing
# smart quotes, em-dashes, accented characters, etc. on Windows consoles (cp1252 default).
# Must run before ``Console()`` is constructed below.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        with suppress(AttributeError, ValueError):
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

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
    reset_run_token_totals()
    configure_observability(
        heartbeat_enabled=settings.observability_heartbeat_enabled,
        heartbeat_interval_seconds=settings.observability_heartbeat_interval_seconds,
        stall_threshold_seconds=settings.observability_stall_threshold_seconds,
    )
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
        token_totals = get_run_token_totals()
        log.info("run.finish", extra={
            "event": "run.finish",
            "duration_ms": elapsed_ms,
            "status": "ok",
            "log_file": str(log_path) if log_path else None,
            "input_tokens": token_totals["input_tokens"],
            "output_tokens": token_totals["output_tokens"],
            "total_tokens": token_totals["total_tokens"],
            "models": token_totals["models"],
        })
        for h in list(logging.getLogger().handlers):
            with contextlib.suppress(Exception):
                h.flush()

    setup_otel(
        settings.otel_enabled,
        settings.otel_exporter_otlp_endpoint or None,
        local_sink_path=settings.otel_local_sink_path,
        service_name=settings.otel_service_name,
    )
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
_REDO_STAGE_OPT = typer.Option(
    None,
    "--redo-stage",
    help="clear checkpoint for this stage and downstream stages (render|figures|extract|graph)",
)
_REASON_OPT = typer.Option(..., help="rejection reason")
_BASE_OPT = typer.Option("", help="base version")
_EVAL_SUITE_OPT = typer.Option(
    Path("config/evals/graphrag_eval.json"),
    help="Path to GraphRAG eval suite JSON file",
)


@app.command()
def ingest(pdf: Path, pages: int | None = _PAGES_OPT) -> None:
    """Ingest a PDF (layout + page images) and persist artifacts."""
    settings, relational, governance, onto_service = _bootstrap()
    bus = EventBus()
    asyncio.run(_ingest_only(settings, relational, governance, onto_service, bus, pdf, pages))


async def _ingest_only(settings, relational, governance, onto_service, bus,
                        pdf: Path, pages_limit: int | None = None) -> None:
    """Ingest-only flow: slice + first-working-adapter + render. No extraction."""
    services = _build_services(settings, relational, governance, onto_service, bus)
    work_dir = settings.artifact_path / pdf.stem
    work_dir.mkdir(parents=True, exist_ok=True)
    source_pdf = slice_pdf_if_requested(pdf, pages_limit, work_dir)
    document = await pick_first_working_ingestion(services.ingestion_chain, source_pdf, work_dir)
    relational.save_document(document)
    await services.renderer.render(source_pdf, work_dir / "pages", dpi=150)
    console.print(f"[green]Ingested[/green] {pdf.name} -> {document.id} ({document.page_count} pages)")


@app.command()
def extract(
    pdf: Path,
    mode: ExtractionMode = _MODE_OPT,
    pages: int | None = _PAGES_OPT,
    ontology_version: str | None = _VERSION_OPT,
    fresh: bool = _FRESH_OPT,
    redo_stage: str | None = _REDO_STAGE_OPT,
) -> None:
    """Run end-to-end ingest -> chunk -> render -> figures -> extract -> graph."""
    settings, relational, governance, onto_service = _bootstrap()
    selected_mode = mode or settings.default_mode
    if redo_stage is not None:
        try:
            Stage(redo_stage)
        except ValueError:
            allowed = ", ".join(s.value for s in Stage)
            raise typer.BadParameter(
                f"invalid --redo-stage '{redo_stage}', expected one of: {allowed}"
            ) from None
    bus = EventBus()
    services = _build_services(settings, relational, governance, onto_service, bus)
    use_case = RunExtractionUseCase(services)
    request = ExtractionRequest(
        pdf=pdf,
        mode=selected_mode,
        pages_limit=pages,
        ontology_version=ontology_version,
        resume=not fresh,
        redo_stage=redo_stage,
    )
    asyncio.run(use_case.execute(request))
    console.print("[green]extract complete[/green]")


def _build_services(settings, relational, governance, onto_service, bus) -> ExtractionServices:
    """Compose adapters + collaborators for the extraction use case."""
    ingestion_chain: list = []
    if settings.azure_document_intelligence_endpoint:
        ingestion_chain.append(DocumentIntelligenceAdapter())
    ingestion_chain.append(DoclingIngestionAdapter())

    return ExtractionServices(
        settings=settings,
        relational=relational,
        governance=governance,
        onto_service=onto_service,
        ingestion_chain=ingestion_chain,
        renderer=PdfPageRenderer(),
        llm=AzureFoundryLLM(settings),
        vision=AzureVisionAdapter(settings, settings.azure_openai_vision_model),
        embeddings=AzureEmbeddingAdapter(settings),
        graph_store=NetworkXGraphStore(),
        checkpoints=FilesystemCheckpointStore(settings.checkpoint_path),
        chunker=SemanticChunker(),
        prompts=PromptRegistry(settings.prompts_dir),
        bus=bus,
    )


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
    extract(pdf=pdf, mode=mode, pages=pages, ontology_version=None, fresh=False, redo_stage=None)


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

    log_path = runner.workdir(version) / "logs" / "indexing-engine.log"

    def _probe_index_progress() -> str | None:
        # Tail the latest line so the heartbeat can show real progress and
        # reset the stall timer whenever graphrag advances.
        try:
            if not log_path.exists():
                return None
            with log_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", errors="replace")
            for line in reversed(tail.splitlines()):
                if "progress:" in line:
                    return line.split(" - ")[-1].strip()
            return None
        except OSError:
            return None

    with wide_event(
        "graphrag.index",
        version=version.version,
        progress_probe=_probe_index_progress,
    ) as ev:
        code = asyncio.run(runner.index(version))
        ev["exit_code"] = code
    if code != 0:
        console.print(f"[red]graphrag index failed (exit {code}). Inspect logs in work/graphrag/{version.version}/logs[/red]")
    raise typer.Exit(code=code)


@graphrag_app.command("ask")
def graphrag_ask(
    question: str = typer.Argument(..., help="Natural-language question"),
    backend: str = typer.Option(
        "auto", "--backend", "-b",
        help="Retrieval backend: 'ms' (Microsoft GraphRAG), 'mini' (BM25 baseline), or 'auto' (ms if indexed, else mini).",
    ),
    method: str = typer.Option(
        "auto", "--method", "-m",
        help="MS GraphRAG search method: local | global | drift | basic | auto. Ignored for --backend mini.",
    ),
    top_k: int = typer.Option(8, help="[mini] Maximum retrieval hits to return"),
    max_neighbors: int = typer.Option(5, help="[mini] Max graph neighbors per matched node"),
    include_graph: bool = typer.Option(True, "--graph/--no-graph", help="[mini] Include graph neighborhood context"),
    community_level: int = typer.Option(2, help="[ms] Leiden community level for global search"),
    response_type: str = typer.Option("Multiple Paragraphs", help="[ms] Desired answer format"),
    timeout: int = typer.Option(180, help="[ms] Per-query timeout in seconds"),
    rewrite: str = typer.Option(
        "none", "--rewrite",
        case_sensitive=False,
        help="[mini] Query rewriting: none | lexical | llm. With lexical|llm, the agent retrieves "
             "for each variant and fuses with Reciprocal Rank Fusion.",
    ),
    rewrite_n: int = typer.Option(3, "--rewrite-n", help="[mini] Number of rewrite variants (excludes the original)."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Ask a question against a GraphRAG retrieval backend.

    Default backend is 'auto', which picks Microsoft GraphRAG when an index is
    available under work/graphrag/, otherwise falls back to the local BM25
    'mini' agent.
    """
    settings = get_settings()
    settings.ensure_dirs()

    chosen_backend = backend.lower()
    if chosen_backend == "auto":
        chosen_backend = "ms" if graphrag_index_available(settings) else "mini"

    if chosen_backend == "ms":
        try:
            agent = MsGraphRagAgent(settings)
            ms_method = None if method.lower() == "auto" else method.lower()  # type: ignore[assignment]
            answer = agent.ask(
                question,
                method=ms_method,  # type: ignore[arg-type]
                community_level=community_level,
                response_type=response_type,
                timeout_seconds=timeout,
            )
        except IndexNotFoundError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=2) from None

        if as_json:
            typer.echo(json.dumps(answer.to_dict(), ensure_ascii=True, indent=2))
            return
        console.print(f"[bold cyan]MS GraphRAG ({answer.method}) — {answer.duration_ms} ms[/bold cyan]")
        console.print(answer.answer)
        return

    if chosen_backend != "mini":
        raise typer.BadParameter(f"unknown backend: {backend!r} (expected ms | mini | auto)")

    default_pdf = settings.project_root / "assets" / "hai_ai_index_report_2025.pdf"
    default_md = settings.artifact_path / "hai_ai_index_report_2025" / "doc.md"
    agent = MiniGraphRagAgent(
        settings.sqlite_path,
        settings.graph_storage_path,
        source_pdf=default_pdf if default_pdf.exists() else None,
        source_markdown=default_md if default_md.exists() else None,
    )
    rewriter = _build_query_rewriter(rewrite, settings)
    queries = _expand_queries(question, rewriter, n=rewrite_n)
    if rewriter is not None and len(queries) > 1:
        result = agent.ask_multi(
            queries,
            top_k=top_k,
            include_graph=include_graph,
            max_neighbors=max_neighbors,
        )
    else:
        result = agent.ask(
            question,
            top_k=top_k,
            include_graph=include_graph,
            max_neighbors=max_neighbors,
        )
    if as_json:
        payload = result.to_dict()
        if rewriter is not None and len(queries) > 1:
            payload["rewrites"] = queries[1:]
        typer.echo(json.dumps(payload, ensure_ascii=True, indent=2))
        return

    if rewriter is not None and len(queries) > 1:
        console.print(f"[dim]rewrites ({rewrite}, n={len(queries) - 1}):[/dim]")
        for q in queries[1:]:
            console.print(f"  • {q}")

    hits = Table(title="Mini GraphRAG retrieval hits")
    hits.add_column("rank", justify="right")
    hits.add_column("kind")
    hits.add_column("id")
    hits.add_column("score", justify="right")
    hits.add_column("text")
    for i, hit in enumerate(result.hits, start=1):
        hits.add_row(str(i), hit.kind, hit.id, f"{hit.score:.3f}", hit.text)
    console.print(hits)

    if include_graph and result.graph_context:
        for ctx in result.graph_context:
            gt = Table(title=f"Graph context: {ctx.node_id} ({ctx.node_type})")
            gt.add_column("neighbor_id")
            gt.add_column("neighbor_label")
            gt.add_column("neighbor_type")
            gt.add_column("edge_types")
            for nb in ctx.neighbors:
                edge_types = ", ".join(str(t) for t in nb.get("edge_types", []))
                gt.add_row(
                    str(nb.get("id", "")),
                    str(nb.get("label", "")),
                    str(nb.get("type", "")),
                    edge_types,
                )
            console.print(gt)


@graphrag_app.command("eval")
def graphrag_eval(
    suite: Path = _EVAL_SUITE_OPT,
    backend: str = typer.Option(
        "mini",
        "--backend",
        case_sensitive=False,
        help="Retrieval backend: 'mini' (BM25 baseline), 'ms' (Microsoft GraphRAG), "
             "or 'both' (run both and print side-by-side comparison).",
    ),
    method: str = typer.Option(
        "local",
        "--method",
        case_sensitive=False,
        help="MS GraphRAG search method when --backend is 'ms' or 'both'. "
             "'local' is fast and entity-aware (good per-case parity with mini); "
             "'global' synthesizes across community reports (slower, ~200s/case); "
             "'auto' picks per-question.",
    ),
    community_level: int = typer.Option(2, help="MS GraphRAG community level (1=fine, 4=coarse)."),
    response_type: str = typer.Option("Multiple Paragraphs", help="MS GraphRAG response type."),
    ms_timeout: int = typer.Option(180, help="Per-case timeout (seconds) for MS GraphRAG queries."),
    top_k: int = typer.Option(15, help="Default top-k retrieval for cases that do not specify one"),
    rewrite: str = typer.Option(
        "none", "--rewrite",
        case_sensitive=False,
        help="[mini] Query rewriting: none | lexical | llm. With lexical|llm, mini "
             "retrieves for each variant and fuses with Reciprocal Rank Fusion.",
    ),
    rewrite_n: int = typer.Option(3, "--rewrite-n", help="[mini] Number of rewrite variants."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
) -> None:
    """Run retrieval eval cases against the mini and/or MS GraphRAG backends."""
    settings = get_settings()
    settings.ensure_dirs()
    if not suite.exists():
        raise typer.BadParameter(f"eval suite not found: {suite}")

    chosen_backend = backend.lower()
    if chosen_backend not in {"mini", "ms", "both"}:
        raise typer.BadParameter(f"unknown backend: {backend!r} (expected mini | ms | both)")

    raw = json.loads(suite.read_text(encoding="utf-8"))
    raw_cases = raw.get("cases", [])
    if not isinstance(raw_cases, list) or not raw_cases:
        raise typer.BadParameter("eval suite must define a non-empty 'cases' array")
    cases: list[GraphRagEvalCase] = [GraphRagEvalCase.from_dict(c) for c in raw_cases if isinstance(c, dict)]

    runs: dict[str, list] = {}
    if chosen_backend in {"mini", "both"}:
        rewriter = _build_query_rewriter(rewrite, settings)
        runs["mini"] = _run_mini_eval(cases, settings, top_k, rewriter=rewriter, rewrite_n=rewrite_n)
    if chosen_backend in {"ms", "both"}:
        runs["ms"] = _run_ms_eval(
            cases, settings,
            method=method.lower(),
            community_level=community_level,
            response_type=response_type,
            timeout_seconds=ms_timeout,
        )

    if as_json:
        payload = {
            "suite": str(suite),
            "backends": {
                name: {
                    "passed": sum(1 for r in results if r.passed),
                    "total": len(results),
                    "aggregates": aggregate_results(results),
                    "results": [r.to_dict() for r in results],
                }
                for name, results in runs.items()
            },
        }
        typer.echo(json.dumps(payload, ensure_ascii=True, indent=2))
        if any(any(not r.passed for r in results) for results in runs.values()):
            raise typer.Exit(code=1)
        return

    for name, results in runs.items():
        _print_eval_table(results, title=f"{name.upper()} GraphRAG eval")

    if chosen_backend == "both":
        _print_backend_comparison(runs)

    failed = any(any(not r.passed for r in results) for results in runs.values())
    if failed:
        raise typer.Exit(code=1)


def _run_mini_eval(
    cases: list[GraphRagEvalCase],
    settings,
    top_k: int,
    *,
    rewriter: QueryRewriter | None = None,
    rewrite_n: int = 3,
) -> list:
    default_pdf = settings.project_root / "assets" / "hai_ai_index_report_2025.pdf"
    default_md = settings.artifact_path / "hai_ai_index_report_2025" / "doc.md"
    agent = MiniGraphRagAgent(
        settings.sqlite_path,
        settings.graph_storage_path,
        source_pdf=default_pdf if default_pdf.exists() else None,
        source_markdown=default_md if default_md.exists() else None,
    )
    results = []
    for case in cases:
        eval_question = case.query_rewrite or case.question
        case_top_k = max(1, case.top_k or top_k)
        queries = _expand_queries(eval_question, rewriter, n=rewrite_n)
        if rewriter is not None and len(queries) > 1:
            result = agent.ask_multi(queries, top_k=case_top_k, include_graph=False)
        else:
            result = agent.ask(eval_question, top_k=case_top_k, include_graph=False)
        results.append(evaluate_case(case, result.hits, mode="retrieval"))
    return results


def _build_query_rewriter(mode: str, settings) -> QueryRewriter | None:
    """Map ``--rewrite none|lexical|llm`` to a :class:`QueryRewriter` (or ``None``)."""
    m = (mode or "none").lower()
    if m in ("", "none", "off", "false"):
        return None
    if m == "lexical":
        return LexicalQueryRewriter()
    if m == "llm":
        from knowledge_extraction.infrastructure.llm.azure_foundry_client import AzureFoundryLLM
        llm = AzureFoundryLLM(settings)
        return LlmQueryRewriter(
            llm=llm,
            model=settings.azure_openai_extraction_model,
            fallback=LexicalQueryRewriter(),
        )
    raise typer.BadParameter(f"unknown --rewrite mode: {mode!r} (expected none | lexical | llm)")


def _expand_queries(question: str, rewriter: QueryRewriter | None, *, n: int) -> list[str]:
    """Return [original, *variants]; just [original] when no rewriter is supplied."""
    if rewriter is None or n <= 0:
        return [question]
    return rewriter.rewrite(question, n=n)


def _run_ms_eval(
    cases: list[GraphRagEvalCase],
    settings,
    *,
    method: str,
    community_level: int,
    response_type: str,
    timeout_seconds: int,
) -> list:
    """Run each case against MS GraphRAG, wrap the answer as a synthetic hit, evaluate."""
    from knowledge_extraction.application.services.graphrag_agent import RetrievalHit

    agent = MsGraphRagAgent(settings)
    results = []
    for idx, case in enumerate(cases, start=1):
        eval_question = case.query_rewrite or case.question
        console.print(f"[dim]ms[/dim] [{idx}/{len(cases)}] {case.case_id}: {eval_question[:80]}")
        try:
            answer = agent.ask(
                eval_question,
                method=method,  # type: ignore[arg-type]
                community_level=community_level,
                response_type=response_type,
                timeout_seconds=timeout_seconds,
            )
            synthetic_hit = RetrievalHit(
                kind="ms_answer",
                id=f"ms:{case.case_id}",
                score=1.0,
                text=answer.answer,
                meta={"method": answer.method, "duration_ms": answer.duration_ms},
            )
            results.append(evaluate_case(case, [synthetic_hit], mode="synthesis"))
        except (RuntimeError, IndexNotFoundError) as exc:
            console.print(f"  [red]error:[/red] {exc}")
            error_hit = RetrievalHit(
                kind="ms_error", id=f"ms-error:{case.case_id}", score=0.0,
                text=f"[ms-graphrag error: {exc}]", meta={},
            )
            results.append(evaluate_case(case, [error_hit], mode="synthesis"))
    return results


def _print_eval_table(results: list, title: str) -> None:
    table = Table(title=title)
    table.add_column("case_id")
    table.add_column("cat")
    table.add_column("passed")
    table.add_column("MRR", justify="right")
    table.add_column("P@k", justify="right")
    table.add_column("R@k", justify="right")
    table.add_column("cite", justify="right")
    table.add_column("reason")
    for r in results:
        mark = "[green]yes[/green]" if r.passed else "[red]no[/red]"
        m = r.metrics
        table.add_row(
            r.case_id, r.category, mark,
            f"{m.get('mrr', 0.0):.2f}",
            f"{m.get('positive_precision_at_k', 0.0):.2f}",
            f"{m.get('positive_recall_at_k', 0.0):.2f}",
            f"{m.get('citation_recall', 0.0):.2f}",
            r.reason,
        )
    console.print(table)

    agg = aggregate_results(results)
    summary = Table(title=f"{title} — aggregates by category")
    summary.add_column("category")
    summary.add_column("passed")
    summary.add_column("avg MRR", justify="right")
    summary.add_column("avg P@k", justify="right")
    for cat, vals in sorted(agg["by_category"].items()):
        summary.add_row(
            cat,
            f"{vals['passed']}/{vals['total']}",
            f"{vals['avg_mrr']:.2f}",
            f"{vals['avg_precision_at_k']:.2f}",
        )
    overall = agg["overall"]
    summary.add_row(
        "[bold]OVERALL[/bold]",
        f"[bold]{overall['passed']}/{overall['total']}[/bold]",
        f"[bold]{overall['avg_mrr']:.2f}[/bold]",
        f"[bold]{overall['avg_precision_at_k']:.2f}[/bold]",
    )
    console.print(summary)


def _print_backend_comparison(runs: dict[str, list]) -> None:
    """Side-by-side per-case win/loss table for the mini vs ms comparison."""
    if "mini" not in runs or "ms" not in runs:
        return
    mini_by_id = {r.case_id: r for r in runs["mini"]}
    ms_by_id = {r.case_id: r for r in runs["ms"]}
    cmp_table = Table(title="Backend comparison (mini vs ms)")
    cmp_table.add_column("case_id")
    cmp_table.add_column("cat")
    cmp_table.add_column("mini", justify="center")
    cmp_table.add_column("ms", justify="center")
    cmp_table.add_column("Δ", justify="center")
    wins = {"mini": 0, "ms": 0, "tie": 0}
    for case_id in mini_by_id:
        mr = mini_by_id[case_id]
        sr = ms_by_id.get(case_id)
        mini_mark = "[green]✓[/green]" if mr.passed else "[red]✗[/red]"
        ms_mark = "[green]✓[/green]" if (sr and sr.passed) else "[red]✗[/red]"
        if mr.passed and sr and sr.passed:
            delta, key = "[dim]tie[/dim]", "tie"
        elif sr and sr.passed and not mr.passed:
            delta, key = "[bold green]ms[/bold green]", "ms"
        elif mr.passed and (not sr or not sr.passed):
            delta, key = "[bold yellow]mini[/bold yellow]", "mini"
        else:
            delta, key = "[dim]tie[/dim]", "tie"
        wins[key] += 1
        cmp_table.add_row(case_id, mr.category, mini_mark, ms_mark, delta)
    console.print(cmp_table)
    console.print(
        f"[bold]Wins:[/bold] ms-only={wins['ms']}, mini-only={wins['mini']}, tie={wins['tie']}"
    )


if __name__ == "__main__":
    app()
