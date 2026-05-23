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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.table import Table

from knowledge_extraction.application.pipelines.stage_1_chunking import SemanticChunker
from knowledge_extraction.application.pipelines.stages import Stage
from knowledge_extraction.application.services.chunk_retriever import ChunkRetriever
from knowledge_extraction.application.services.graphrag_agent import MiniGraphRagAgent
from knowledge_extraction.application.services.graphrag_eval import (
    GraphRagEvalCase,
    aggregate_results,
    evaluate_case,
)
from knowledge_extraction.application.services.lazy_graphrag_agent import (
    LazyGraphRagAgent,
    lazy_index_available,
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
)
from knowledge_extraction.config.settings import AzureAuthMode, ExtractionMode, Settings, get_settings
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

app = typer.Typer(
    no_args_is_help=True,
    help="Knowledge extraction & ontology governance CLI.",
    pretty_exceptions_show_locals=False,
)
ontology_app = typer.Typer(help="Ontology governance.")
graphrag_app = typer.Typer(help="Microsoft GraphRAG integration.")

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

    class _RunErrorFlagFilter(logging.Filter):
        def __init__(self, seen: dict[str, bool]) -> None:
            super().__init__()
            self._seen = seen

        def filter(self, record: logging.LogRecord) -> bool:
            if record.levelno >= logging.ERROR:
                event = str(getattr(record, "event", "") or "").strip()
                # These may be transient and recovered by higher-level retries.
                if event not in {"llm.complete_json", "extract.chunk"}:
                    self._seen["error"] = True
            return True

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
    run_state = {"error": False}
    run_error_filter = _RunErrorFlagFilter(run_state)
    root_logger = logging.getLogger()
    filtered_handlers = list(root_logger.handlers)
    for handler in filtered_handlers:
        handler.addFilter(run_error_filter)
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
            "status": "error" if run_state["error"] else "ok",
            "log_file": str(log_path) if log_path else None,
            "input_tokens": token_totals["input_tokens"],
            "output_tokens": token_totals["output_tokens"],
            "total_tokens": token_totals["total_tokens"],
            "models": token_totals["models"],
        })
        for handler in filtered_handlers:
            with contextlib.suppress(Exception):
                handler.removeFilter(run_error_filter)
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
_BUILD_KNOWLEDGE_TREE_OPT = typer.Option(
    True,
    "--build-knowledge-tree/--no-build-knowledge-tree",
    help="Build Microsoft GraphRAG knowledge tree after extraction (enabled by default).",
)
_REASON_OPT = typer.Option(..., help="rejection reason")
_BASE_OPT = typer.Option("", help="base version")
_EVAL_SUITE_OPT = typer.Option(
    Path("config/evals/graphrag_eval.json"),
    help="Path to GraphRAG eval suite JSON file",
)
_INGEST_PDF_ARG = typer.Argument(
    None,
    help="PDF to ingest. Defaults to ingesting all PDFs in assets/.",
)


@dataclass(slots=True)
class PreflightResult:
    status: str
    check: str
    detail: str


def _is_https_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme.lower() == "https" and bool(parsed.netloc)


def _collect_preflight_results(
    settings: Settings, *, live: bool, include_graphrag: bool,
) -> list[PreflightResult]:
    results: list[PreflightResult] = []
    results.extend(_check_project_layout(settings))
    results.extend(_check_openai_config(settings))
    results.extend(_check_document_intelligence_config(settings))
    if settings.azure_auth_mode is AzureAuthMode.CREDENTIAL:
        results.append(_check_default_credential_token())
    if live:
        results.append(_probe_openai_chat(settings))
        results.append(_probe_openai_embeddings(settings))
        results.append(_probe_document_intelligence(settings))
    if include_graphrag:
        results.append(_check_graphrag_executable(settings))
    return results


def _check_project_layout(settings: Settings) -> list[PreflightResult]:
    prompts = sorted(settings.prompts_dir.glob("*.j2"))
    prompt_status = "PASS" if prompts else "FAIL"
    prompt_detail = (
        f"{len(prompts)} prompt template(s) found in {settings.prompts_dir}"
        if prompts else f"no prompt templates found in {settings.prompts_dir}"
    )
    ontology_ok = settings.ontology_yaml_path.exists()
    return [
        PreflightResult(
            status="PASS" if ontology_ok else "FAIL",
            check="ontology config",
            detail=(
                f"found {settings.ontology_yaml_path}"
                if ontology_ok else f"missing {settings.ontology_yaml_path}"
            ),
        ),
        PreflightResult(status=prompt_status, check="prompt templates", detail=prompt_detail),
    ]


def _check_openai_config(settings: Settings) -> list[PreflightResult]:
    if not _is_https_url(settings.azure_openai_endpoint):
        endpoint = PreflightResult(
            status="FAIL",
            check="azure openai endpoint",
            detail="AZURE_OPENAI_ENDPOINT must be a valid https URL",
        )
    else:
        endpoint = PreflightResult(
            status="PASS",
            check="azure openai endpoint",
            detail=settings.azure_openai_endpoint,
        )

    key_status = "PASS"
    key_detail = f"auth mode: {settings.azure_auth_mode.value}"
    if settings.azure_auth_mode is AzureAuthMode.KEY and not settings.azure_openai_api_key:
        key_status = "FAIL"
        key_detail = "AZURE_AUTH_MODE=key but AZURE_OPENAI_API_KEY is empty"

    required_models = [
        ("reasoning model", settings.azure_openai_reasoning_model),
        ("extraction model", settings.azure_openai_extraction_model),
        ("vision model", settings.azure_openai_vision_model),
        ("embedding model", settings.azure_openai_embedding_model),
    ]
    model_results = [
        PreflightResult(
            status="PASS" if model.strip() else "FAIL",
            check=label,
            detail=model if model.strip() else "model deployment name is empty",
        )
        for label, model in required_models
    ]
    return [endpoint, PreflightResult(status=key_status, check="azure auth", detail=key_detail), *model_results]


def _check_document_intelligence_config(settings: Settings) -> list[PreflightResult]:
    if not settings.azure_document_intelligence_endpoint:
        return [
            PreflightResult(
                status="WARN",
                check="document intelligence",
                detail="not configured; ingest will fall back to Docling",
            )
        ]
    endpoint_ok = _is_https_url(settings.azure_document_intelligence_endpoint)
    endpoint = PreflightResult(
        status="PASS" if endpoint_ok else "FAIL",
        check="document intelligence endpoint",
        detail=(
            settings.azure_document_intelligence_endpoint
            if endpoint_ok else "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT must be a valid https URL"
        ),
    )
    auth = PreflightResult(
        status="PASS",
        check="document intelligence auth",
        detail=f"auth mode: {settings.azure_auth_mode.value}",
    )
    if settings.azure_auth_mode is AzureAuthMode.KEY and not settings.azure_document_intelligence_key:
        auth = PreflightResult(
            status="FAIL",
            check="document intelligence auth",
            detail="AZURE_AUTH_MODE=key but AZURE_DOCUMENT_INTELLIGENCE_KEY is empty",
        )
    return [endpoint, auth]


def _check_default_credential_token() -> PreflightResult:
    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity import CredentialUnavailableError, DefaultAzureCredential

    cred = DefaultAzureCredential()
    scope = "https://cognitiveservices.azure.com/.default"
    try:
        token = cred.get_token(scope)
    except CredentialUnavailableError as exc:
        return PreflightResult(
            status="FAIL",
            check="defaultazurecredential token",
            detail=f"credential unavailable: {exc}",
        )
    except ClientAuthenticationError as exc:
        return PreflightResult(
            status="FAIL",
            check="defaultazurecredential token",
            detail=f"authentication failed: {exc}",
        )
    return PreflightResult(
        status="PASS",
        check="defaultazurecredential token",
        detail=f"token acquired (expires_on={token.expires_on})",
    )


def _build_sync_openai_client(settings: Settings):
    from openai import AzureOpenAI

    if settings.azure_auth_mode is AzureAuthMode.CREDENTIAL:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        return AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
            azure_ad_token_provider=token_provider,
        )
    return AzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_version=settings.azure_openai_api_version,
        api_key=settings.azure_openai_api_key,
    )


def _uses_max_completion_tokens(model: str) -> bool:
    normalized = model.lower()
    return (
        normalized.startswith(("gpt-5", "o1", "o3", "o4"))
        or normalized.startswith("phi-4")
    )


def _probe_openai_chat(settings: Settings) -> PreflightResult:
    from openai import APIConnectionError, APIStatusError

    try:
        client = _build_sync_openai_client(settings)
        kwargs: dict[str, object] = {
            "model": settings.azure_openai_extraction_model,
            "messages": [
                {"role": "system", "content": "Return OK."},
                {"role": "user", "content": "ping"},
            ],
        }
        if _uses_max_completion_tokens(settings.azure_openai_extraction_model):
            kwargs["max_completion_tokens"] = 8
        else:
            kwargs["max_tokens"] = 8
            kwargs["temperature"] = 0.0
        client.chat.completions.create(**kwargs)
    except APIConnectionError as exc:
        return PreflightResult(status="FAIL", check="openai chat probe", detail=f"connection failed: {exc}")
    except APIStatusError as exc:
        return PreflightResult(
            status="FAIL",
            check="openai chat probe",
            detail=f"request failed: status={exc.status_code} {exc}",
        )
    return PreflightResult(
        status="PASS",
        check="openai chat probe",
        detail=f"chat completion reachable via {settings.azure_openai_extraction_model}",
    )


def _probe_openai_embeddings(settings: Settings) -> PreflightResult:
    from openai import APIConnectionError, APIStatusError

    try:
        client = _build_sync_openai_client(settings)
        client.embeddings.create(
            model=settings.azure_openai_embedding_model,
            input=["preflight"],
        )
    except APIConnectionError as exc:
        return PreflightResult(
            status="FAIL",
            check="openai embedding probe",
            detail=f"connection failed: {exc}",
        )
    except APIStatusError as exc:
        return PreflightResult(
            status="FAIL",
            check="openai embedding probe",
            detail=f"request failed: status={exc.status_code} {exc}",
        )
    return PreflightResult(
        status="PASS",
        check="openai embedding probe",
        detail=f"embedding endpoint reachable via {settings.azure_openai_embedding_model}",
    )


def _probe_document_intelligence(settings: Settings) -> PreflightResult:
    if not settings.azure_document_intelligence_endpoint:
        return PreflightResult(
            status="WARN",
            check="document intelligence probe",
            detail="skipped (endpoint not configured)",
        )

    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.core.exceptions import HttpResponseError, ServiceRequestError

    if settings.azure_auth_mode is AzureAuthMode.CREDENTIAL:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        client = DocumentIntelligenceClient(
            endpoint=settings.azure_document_intelligence_endpoint,
            credential=credential,
        )
    else:
        from azure.core.credentials import AzureKeyCredential

        client = DocumentIntelligenceClient(
            endpoint=settings.azure_document_intelligence_endpoint,
            credential=AzureKeyCredential(settings.azure_document_intelligence_key),
        )

    test_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    try:
        poller = client.begin_analyze_document("prebuilt-layout", test_pdf)
        poller.result()
    except ServiceRequestError as exc:
        return PreflightResult(
            status="FAIL",
            check="document intelligence probe",
            detail=f"connection failed: {exc}",
        )
    except HttpResponseError as exc:
        if exc.status_code in (401, 403):
            return PreflightResult(
                status="FAIL",
                check="document intelligence probe",
                detail=f"authentication failed: status={exc.status_code}",
            )
        return PreflightResult(
            status="PASS",
            check="document intelligence probe",
            detail=f"service reachable (status={exc.status_code}, auth accepted)",
        )
    return PreflightResult(
        status="PASS",
        check="document intelligence probe",
        detail="service reachable and accepted probe document",
    )


def _check_graphrag_executable(settings: Settings) -> PreflightResult:
    from knowledge_extraction.infrastructure.graphrag.graphrag_runner import resolve_graphrag_executable

    try:
        executable = resolve_graphrag_executable(settings)
    except RuntimeError as exc:
        return PreflightResult(status="FAIL", check="graphrag executable", detail=str(exc))
    return PreflightResult(status="PASS", check="graphrag executable", detail=executable)


@app.command()
def preflight(
    live: bool = typer.Option(
        True,
        "--live/--no-live",
        help="Run live Azure service probes (chat, embeddings, and Document Intelligence).",
    ),
    graphrag: bool = typer.Option(
        False,
        "--graphrag",
        help="Also validate that the `graphrag` executable is discoverable.",
    ),
) -> None:
    """Run quick config/auth checks before starting a heavy run."""
    settings = get_settings()
    settings.ensure_dirs()
    results = _collect_preflight_results(settings, live=live, include_graphrag=graphrag)

    table = Table(title="Preflight")
    table.add_column("status")
    table.add_column("check")
    table.add_column("detail")
    status_colors = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
    for row in results:
        color = status_colors.get(row.status, "white")
        table.add_row(f"[{color}]{row.status}[/{color}]", row.check, row.detail)
    console.print(table)

    failed = [r for r in results if r.status == "FAIL"]
    warned = [r for r in results if r.status == "WARN"]
    if failed:
        console.print(f"[red]preflight failed: {len(failed)} check(s) failed[/red]")
        raise typer.Exit(code=2)
    if warned:
        console.print(f"[yellow]preflight passed with {len(warned)} warning(s)[/yellow]")
        raise typer.Exit(code=0)
    console.print("[green]preflight passed[/green]")


@app.command()
def ingest(
    pdf: Path | None = _INGEST_PDF_ARG,
    mode: ExtractionMode = _MODE_OPT,
    pages: int | None = _PAGES_OPT,
    ontology_version: str | None = _VERSION_OPT,
    fresh: bool = _FRESH_OPT,
    redo_stage: str | None = _REDO_STAGE_OPT,
    build_knowledge_tree: bool = _BUILD_KNOWLEDGE_TREE_OPT,
) -> None:
    """Run full extraction for one PDF or, by default, every PDF in assets/."""
    settings, relational, governance, onto_service = _bootstrap()
    pdfs = _resolve_ingest_sources(settings, pdf)
    asyncio.run(
        _run_extraction_batch(
            settings=settings,
            relational=relational,
            governance=governance,
            onto_service=onto_service,
            pdfs=pdfs,
            mode=mode,
            pages=pages,
            ontology_version=ontology_version,
            fresh=fresh,
            redo_stage=redo_stage,
        )
    )
    if build_knowledge_tree:
        _run_ms_graphrag_index(settings=settings, relational=relational, onto_service=onto_service)


def _resolve_ingest_sources(settings: Settings, pdf: Path | None) -> list[Path]:
    """Resolve ingest sources; default to all PDFs in the assets directory."""
    if pdf is not None:
        if pdf.is_dir():
            docs = sorted(p for p in pdf.glob("*.pdf") if p.is_file())
            if not docs:
                raise typer.BadParameter(f"no PDFs found in directory: {pdf}")
            return docs
        if not pdf.exists():
            raise typer.BadParameter(f"file not found: {pdf}")
        if pdf.suffix.lower() != ".pdf":
            raise typer.BadParameter(f"expected a PDF file, got: {pdf.name}")
        return [pdf]

    assets_dir = settings.project_root / "assets"
    if not assets_dir.exists():
        raise typer.BadParameter(f"default assets directory not found: {assets_dir}")
    docs = sorted(p for p in assets_dir.glob("*.pdf") if p.is_file())
    if not docs:
        raise typer.BadParameter(f"no PDFs found in default assets directory: {assets_dir}")
    return docs


async def _run_extraction_batch(
    *,
    settings: Settings,
    relational: RelationalRepository,
    governance: GovernanceRepository,
    onto_service: OntologyService,
    pdfs: list[Path],
    mode: ExtractionMode | None,
    pages: int | None,
    ontology_version: str | None,
    fresh: bool,
    redo_stage: str | None,
) -> None:
    if len(pdfs) > 1:
        console.print(f"[cyan]extracting {len(pdfs)} PDFs[/cyan]")
    for idx, source_pdf in enumerate(pdfs, start=1):
        if len(pdfs) > 1:
            console.print(f"[cyan][{idx}/{len(pdfs)}][/cyan] {source_pdf.name}")
        await _run_extraction_for_pdf(
            settings=settings,
            relational=relational,
            governance=governance,
            onto_service=onto_service,
            pdf=source_pdf,
            mode=mode,
            pages=pages,
            ontology_version=ontology_version,
            fresh=fresh,
            redo_stage=redo_stage,
        )


async def _run_extraction_for_pdf(
    *,
    settings: Settings,
    relational: RelationalRepository,
    governance: GovernanceRepository,
    onto_service: OntologyService,
    pdf: Path,
    mode: ExtractionMode | None,
    pages: int | None,
    ontology_version: str | None,
    fresh: bool,
    redo_stage: str | None,
) -> None:
    """Run end-to-end extraction for a single PDF."""
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
    await use_case.execute(request)
    console.print(f"[green]extract complete[/green] {pdf.name}")


def _run_ms_graphrag_index(
    *,
    settings: Settings,
    relational: RelationalRepository,
    onto_service: OntologyService,
) -> None:
    """Build the Microsoft GraphRAG index from extracted chunks."""
    from knowledge_extraction.infrastructure.graphrag.graphrag_runner import GraphRagRunner

    version = onto_service.active()[0]
    chunks = relational.list_chunks()
    if not chunks:
        console.print("[yellow]No chunks in the relational store yet — run `ke ingest` first.[/yellow]")
        raise typer.Exit(code=2)

    runner = GraphRagRunner(Path("./work/graphrag"), settings)
    with wide_event("graphrag.write_inputs", chunks=len(chunks), version=version.version):
        runner.write_inputs(version, chunks)

    log_path = runner.workdir(version) / "logs" / "indexing-engine.log"

    def _probe_index_progress() -> str | None:
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

    code = 0
    try:
        with wide_event(
            "graphrag.index",
            version=version.version,
            progress_probe=_probe_index_progress,
        ) as ev:
            code = asyncio.run(runner.index(version))
            ev["exit_code"] = code
            if code != 0:
                raise RuntimeError(f"graphrag index exited {code}")
    except RuntimeError:
        console.print(f"[red]graphrag index failed (exit {code}). Inspect logs in work/graphrag/{version.version}/logs[/red]")
        raise typer.Exit(code=max(1, code)) from None
    console.print(f"[green]ms index ready[/green] work\\graphrag\\{version.version}")


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


@app.command()
def resume(
    pdf: Path,
    mode: ExtractionMode = ExtractionMode.GOVERNED,
    pages: int | None = _PAGES_OPT,
) -> None:
    """Re-run extraction; checkpointed stages are skipped."""
    settings, relational, governance, onto_service = _bootstrap()
    asyncio.run(
        _run_extraction_for_pdf(
            settings=settings,
            relational=relational,
            governance=governance,
            onto_service=onto_service,
            pdf=pdf,
            mode=mode,
            pages=pages,
            ontology_version=None,
            fresh=False,
            redo_stage=None,
        )
    )


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


def _resolve_streamlit_app_path(module_name: str, file_name: str) -> Path:
    app_path = Path(__file__).parent / file_name
    if app_path.exists():
        return app_path
    import importlib.util

    spec = importlib.util.find_spec(module_name)
    if spec and spec.origin:
        resolved = Path(spec.origin)
        if resolved.exists():
            return resolved
    return app_path


@app.command()
def webui(
    backend: str = typer.Option(
        "lazy",
        "--backend",
        "-b",
        help="Default retrieval backend for the Chat page: lazy | mini | ms.",
    ),
    port: int = typer.Option(8502, help="Port for Streamlit"),
    host: str = typer.Option("localhost", help="Host"),
) -> None:
    """Launch unified Streamlit UI with Telemetry and Chat pages."""
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

    chosen = backend.lower().strip()
    if chosen not in {"lazy", "mini", "ms"}:
        raise typer.BadParameter("backend must be one of: lazy, mini, ms")

    app_path = _resolve_streamlit_app_path("knowledge_extraction.cli.webui_app", "webui_app.py")
    if not app_path.exists():
        console.print(f"[red]{app_path.name} not found at {app_path}[/red]")
        raise typer.Exit(code=1)

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(port),
        "--server.address",
        host,
        "--browser.gatherUsageStats",
        "false",
    ]
    cmd.extend(["--", f"--backend={chosen}"])
    console.print(f"[green]Launching webui:[/green] http://{host}:{port}")
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
    settings, relational, _governance, onto_service = _bootstrap()
    _run_ms_graphrag_index(settings=settings, relational=relational, onto_service=onto_service)


@graphrag_app.command("ask")
def graphrag_ask(
    question: str = typer.Argument(..., help="Natural-language question"),
    backend: str = typer.Option(
        "auto", "--backend", "-b",
        help="Retrieval backend: 'ms' (Microsoft GraphRAG, default when indexed), "
             "'lazy' (LazyGraphRAG — JIT subgraph at query time, no index), "
             "'mini' (lexical BM25 baseline), or 'auto' (ms if indexed, else mini).",
    ),
    method: str = typer.Option(
        "auto", "--method", "-m",
        help="MS GraphRAG search method: local | global | drift | basic | auto. Ignored for --backend mini|lazy.",
    ),
    top_k: int = typer.Option(8, help="[mini/lazy] Maximum retrieval hits/chunks to use"),
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
    'mini' agent. ``--backend lazy`` opts into the query-time-only LazyGraphRAG
    path (skips the pre-built knowledge graph entirely).
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

    if chosen_backend == "lazy":
        if not lazy_index_available(settings):
            console.print(
                f"[red]No chunks found in {settings.sqlite_path}. "
                f"Run `ke ingest <pdf>` first to populate the chunk store.[/red]"
            )
            raise typer.Exit(code=2)
        lazy_agent = _build_lazy_agent(settings)
        lazy_answer = lazy_agent.ask(question, top_k_chunks=top_k)
        if as_json:
            typer.echo(json.dumps(lazy_answer.to_dict(), ensure_ascii=True, indent=2))
            return
        console.print(
            f"[bold cyan]LazyGraphRAG — {lazy_answer.duration_ms} ms, "
            f"{len(lazy_answer.chunks)} chunks, "
            f"{lazy_answer.tokens.total} tokens[/bold cyan]"
        )
        console.print(lazy_answer.answer)
        return

    if chosen_backend != "mini":
        raise typer.BadParameter(f"unknown backend: {backend!r} (expected ms | lazy | mini | auto)")

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
        "ms",
        "--backend",
        case_sensitive=False,
        help="Retrieval backend(s): 'ms' (Microsoft GraphRAG, default and SOTA), "
             "'lazy' (LazyGraphRAG, JIT subgraph at query time), "
             "'mini' (lexical BM25 baseline). Pass a comma-separated list "
             "(e.g. 'ms,lazy,mini') for a side-by-side comparison run, or use "
             "'both' as a shorthand for 'mini,ms'.",
    ),
    method: str = typer.Option(
        "local",
        "--method",
        case_sensitive=False,
        help="MS GraphRAG search method when --backend includes 'ms'. "
             "'local' is fast and entity-aware (good per-case parity with mini); "
             "'global' synthesizes across community reports (slower, ~200s/case); "
             "'auto' picks per-question.",
    ),
    community_level: int = typer.Option(2, help="MS GraphRAG community level (1=fine, 4=coarse)."),
    response_type: str = typer.Option("Multiple Paragraphs", help="MS GraphRAG response type."),
    ms_timeout: int = typer.Option(180, help="Per-case timeout (seconds) for MS GraphRAG queries."),
    lazy_top_k: int = typer.Option(20, help="[lazy] chunks to retrieve per question."),
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
    """Run retrieval eval cases against one or more GraphRAG backends."""
    settings = get_settings()
    settings.ensure_dirs()
    if not suite.exists():
        raise typer.BadParameter(f"eval suite not found: {suite}")

    chosen_backends = _parse_backends(backend)

    raw = json.loads(suite.read_text(encoding="utf-8"))
    raw_cases = raw.get("cases", [])
    if not isinstance(raw_cases, list) or not raw_cases:
        raise typer.BadParameter("eval suite must define a non-empty 'cases' array")
    cases: list[GraphRagEvalCase] = [GraphRagEvalCase.from_dict(c) for c in raw_cases if isinstance(c, dict)]

    runs: dict[str, list] = {}
    if "mini" in chosen_backends:
        rewriter = _build_query_rewriter(rewrite, settings)
        runs["mini"] = _run_mini_eval(cases, settings, top_k, rewriter=rewriter, rewrite_n=rewrite_n)
    if "lazy" in chosen_backends:
        runs["lazy"] = _run_lazy_eval(cases, settings, top_k_chunks=lazy_top_k)
    if "ms" in chosen_backends:
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

    if len(runs) > 1:
        _print_backend_comparison(runs)

    failed = any(any(not r.passed for r in results) for results in runs.values())
    if failed:
        raise typer.Exit(code=1)


def _parse_backends(spec: str) -> list[str]:
    """Parse the ``--backend`` value into an ordered, deduplicated list of backends.

    Accepts a single backend (``"ms"``), a comma-separated list (``"ms,lazy,mini"``),
    or the legacy shorthand ``"both"`` (= ``"mini,ms"``).
    """
    raw = (spec or "").strip().lower()
    if not raw:
        raise typer.BadParameter("--backend cannot be empty")
    if raw == "both":
        return ["mini", "ms"]
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    valid = {"mini", "ms", "lazy"}
    seen: set[str] = set()
    ordered: list[str] = []
    for p in parts:
        if p not in valid:
            raise typer.BadParameter(
                f"unknown backend: {p!r} (expected combination of mini | ms | lazy, or 'both')"
            )
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


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


def _build_lazy_agent(settings) -> LazyGraphRagAgent:
    """Wire a :class:`LazyGraphRagAgent` from the standard settings.

    Used by both ``graphrag ask --backend lazy`` and the eval harness.
    """
    llm = AzureFoundryLLM(settings)
    prompts = PromptRegistry(settings.prompts_dir)
    chunk_retriever = ChunkRetriever(settings.sqlite_path)
    return LazyGraphRagAgent(settings, chunk_retriever, llm, prompts)


def _run_lazy_eval(
    cases: list[GraphRagEvalCase],
    settings,
    *,
    top_k_chunks: int,
) -> list:
    """Run each case through LazyGraphRAG and evaluate the synthesized answer."""
    from knowledge_extraction.application.services.graphrag_agent import RetrievalHit

    if not lazy_index_available(settings):
        raise typer.BadParameter(
            f"No chunks found in {settings.sqlite_path}. Run `ke ingest <pdf>` first."
        )
    agent = _build_lazy_agent(settings)
    results = []
    for idx, case in enumerate(cases, start=1):
        eval_question = case.query_rewrite or case.question
        console.print(f"[dim]lazy[/dim] [{idx}/{len(cases)}] {case.case_id}: {eval_question[:80]}")
        try:
            answer = agent.ask(eval_question, top_k_chunks=top_k_chunks)
            synthetic_hit = RetrievalHit(
                kind="lazy_answer",
                id=f"lazy:{case.case_id}",
                score=1.0,
                text=answer.answer,
                meta={
                    "duration_ms": answer.duration_ms,
                    "tokens": answer.tokens.total,
                    "chunks": len(answer.chunks),
                },
            )
            results.append(evaluate_case(case, [synthetic_hit], mode="synthesis"))
        except RuntimeError as exc:
            console.print(f"  [red]error:[/red] {exc}")
            error_hit = RetrievalHit(
                kind="lazy_error", id=f"lazy-error:{case.case_id}", score=0.0,
                text=f"[lazy-graphrag error: {exc}]", meta={},
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
    """Side-by-side per-case win/loss table across all run backends.

    Generalised to any subset of {mini, lazy, ms}: prints one column per backend,
    plus a summary line counting how many cases each backend solved exclusively.
    """
    if len(runs) < 2:
        return
    backends = list(runs.keys())
    by_id: dict[str, dict[str, object]] = {b: {r.case_id: r for r in runs[b]} for b in backends}
    case_ids = list(next(iter(by_id.values())).keys())

    cmp_table = Table(title=f"Backend comparison ({' vs '.join(backends)})")
    cmp_table.add_column("case_id")
    cmp_table.add_column("cat")
    for b in backends:
        cmp_table.add_column(b, justify="center")
    cmp_table.add_column("winner", justify="center")

    exclusive_wins = dict.fromkeys(backends, 0)
    all_pass = 0
    all_fail = 0
    for case_id in case_ids:
        per_backend = {b: by_id[b].get(case_id) for b in backends}
        passes = {b: bool(r and r.passed) for b, r in per_backend.items()}
        category = next((r.category for r in per_backend.values() if r is not None), "")
        marks = ["[green]✓[/green]" if passes[b] else "[red]✗[/red]" for b in backends]
        n_pass = sum(passes.values())
        if n_pass == len(backends):
            winner = "[dim]all[/dim]"
            all_pass += 1
        elif n_pass == 0:
            winner = "[dim]none[/dim]"
            all_fail += 1
        elif n_pass == 1:
            sole = next(b for b, p in passes.items() if p)
            winner = f"[bold green]{sole}[/bold green]"
            exclusive_wins[sole] += 1
        else:
            winner = "[yellow]" + ",".join(b for b, p in passes.items() if p) + "[/yellow]"
        cmp_table.add_row(case_id, category, *marks, winner)
    console.print(cmp_table)

    summary = ", ".join(f"{b}-only={n}" for b, n in exclusive_wins.items())
    console.print(f"[bold]Exclusive wins:[/bold] {summary} | all-pass={all_pass} | all-fail={all_fail}")


if __name__ == "__main__":
    app()
