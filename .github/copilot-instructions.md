# Copilot instructions — AdvancedRAG

Production-grade PDF → knowledge-graph → GraphRAG pipeline. The real project lives in
`knowledge_extraction/` (a `uv`-managed Python 3.12 package); run all commands from there.

Authoritative docs to read before non-trivial work:
- `docs/technical.md` — how it works internally: architecture, pipeline stages, persistence,
  observability, resilience, config, CLI, gotchas, file layout, Neo4j.
- `docs/functional.md` — which methods are applied, how/why, strengths/benefits: extraction
  modes, ontology governance, the five retrieval backends, benchmarks, evaluation.
- `knowledge_extraction/README.md` — quickstart, install, CLI reference, retrieval backends.

## Build / test / lint

All commands run from `knowledge_extraction/`:

```bash
uv sync                          # install (add --extra neo4j / --extra tour as needed)
uv run pytest                    # full test suite
uv run pytest tests/unit/test_chunker.py            # single file
uv run pytest tests/unit/test_chunker.py::test_name # single test
uv run ruff check .              # lint (line-length 110; E501 ignored)
uv run ruff format .             # format
uv run mypy                      # type-check (strict mode)
uv run ke preflight              # config/auth check before a live Azure run
```

`pytest` uses `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed). Most tests are unit
tests under `tests/unit/`; `tests/integration/test_smoke.py` is the slow path.

## Architecture (the big picture)

Strict layering — `domain` and `application` must **never** import `infrastructure`:

```
domain/         pure pydantic v2 models, no I/O
application/    use_cases + pipelines + ports (Protocols) + services — pure orchestration
infrastructure/ adapters implementing ports: Azure clients, SQLite, Qdrant, GraphRAG, FS
cli/ + tui/     presentation; cli/main.py is the composition root
```

Wiring happens only in `cli/main.py`, which builds an `ExtractionServices` bag and hands it to
`RunExtractionUseCase`. To use a new adapter, add a `Protocol` in `application/ports/`, implement
it in `infrastructure/`, and wire it in the composition root — not by importing directly.

The entire ingest pipeline is one readable file: `application/use_cases/run_extraction.py`.
Stage order/names/checkpoint paths come from the `Stage` enum in
`application/pipelines/stages.py` (single source of truth; also drives `--redo-stage`).
`application/pipelines/orchestrator.py` is the checkpoint-aware DAG runner.

## Key conventions

- **Two extraction modes.** *Discovery* (reasoning model) never writes `ontology_versions`
  directly — candidates always land in `ontology_proposals` for human approval. *Governed*
  extracts against an approved `OntologyVersion`; off-schema → `UNKNOWN` + refinement proposal.
- **Checkpointing.** Stage-level `.done` markers under `work/checkpoints/<doc_hash>/<stage>/`;
  resume skips completed stages. Chunk-level resume re-processes only incomplete chunks. Use
  `ke ingest <pdf> --redo-stage <stage>` to clear a stage + everything downstream.
- **Persistence split:** SQLite (SQLAlchemy) for chunks/entities/relationships/claims; Qdrant
  for vectors; NetworkX + GraphML/JSON-LD/Cypher exports for the property graph; filesystem
  under `work/artifacts/` for images/markdown/JSON; parquet under `work/graphrag/<version>/`.
  No Alembic — `make_engine()` applies lightweight `ALTER TABLE` patches for legacy DBs.
- **Observability.** Wrap long blocking ops in `wide_event(name, **fields)` (one JSON record
  per logical op) — see `infrastructure/telemetry/observability.py`. Every run writes
  `work/logs/run-*.jsonl`; token usage rolls up self-vs-total per span.
- **Five retrieval backends** (`mini`, `ms`, `lazy`, `agentic`, `nav`) share the same stores.
  Adding one requires wiring its id into **both** `cli/main.py` and `cli/webui_app.py` (parse/
  valid sets, ask/eval/bench dispatch, backend builders, webui labels/options).
- **Prompts** are versioned Jinja2 templates under `config/prompts/` (e.g. `*.v1.j2`).
- **Config** via `pydantic-settings` from `.env`; `AZURE_AUTH_MODE` toggles API key vs
  `DefaultAzureCredential`. Copy `.env.example` first.
- **Windows/UTF-8:** CLIs reconfigure stdout/stderr to UTF-8 and pass `PYTHONUTF8=1` /
  `PYTHONIOENCODING=utf-8` to subprocesses; preserve this when touching CLI or subprocess code.
