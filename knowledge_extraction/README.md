# Knowledge Extraction

Production-grade PDF knowledge extraction → ontology + property graph → Microsoft GraphRAG.

Two extraction modes:

- **Discovery** — free-form, reasoning-model-driven, emits versioned ontology candidates for human review. Never mutates the canonical ontology.
- **Governed** — schema-guided extraction against an approved ontology version. Validates edges, canonicalizes entities, detects semantic drift, emits refinement proposals.

## Quickstart

```bash
cd knowledge_extraction
uv sync
cp .env.example .env       # fill in Azure endpoints/keys
uv run ke --help
```

End-to-end on the bundled HAI AI Index report:

```bash
uv run ke ingest assets/hai_ai_index_report_2025.pdf
uv run ke extract assets/hai_ai_index_report_2025.pdf --mode governed
uv run ke graph build
uv run ke graphrag index
uv run ke graphrag ask "Which table shows AI model performance on page 88?" --top-k 10
uv run ke graphrag eval
uv run ke stats
```

The `graphrag ask` command is a lightweight reusable retrieval scaffold for
local experimentation. It runs hybrid lookup across claims, entities, tables,
figures, and (optionally) graph neighbors from the latest GraphML export.

`graphrag eval` runs reusable retrieval eval cases from
`config/evals/graphrag_eval.json` (includes a SuperGLUE disambiguation case:
benchmark/model meaning vs adhesive meaning).

Discovery run on an unfamiliar corpus:

```bash
uv run ke extract <doc.pdf> --mode discovery
uv run ke ontology list
uv run ke ontology diff v1.0.0 candidate-3
uv run ke ontology approve <proposal_id>
uv run ke ontology migrate v1.0.0 v1.1.0
```

## Architecture

See [`architecture.md`](./architecture.md). Layered domain → application → infrastructure with adapters behind ports for OCR, LLM, vision, embeddings, vector store, graph store, relational store, and checkpoints.

## Recommended models

| Role        | Recommendation                                  |
|-------------|-------------------------------------------------|
| Reasoning   | `o4-mini` or `o3` (Discovery mode prefers this) |
| Extraction  | `gpt-4.1-mini` / `gpt-4o-mini` (JSON mode)      |
| Vision      | `gpt-4.1` / `gpt-4o`                            |
| Embeddings  | `text-embedding-3-large` (3072d)                |

## Layout

```
knowledge_extraction/
  domain/                # pure pydantic models, no I/O
  application/
    use_cases/           # ★ start here — `run_extraction.py` IS the pipeline
    pipelines/           # individual stage implementations + Stage catalog
    services/            # ontology governance, GraphRAG agent, eval
    ports/               # Protocols (LLMPort, VisionPort, CheckpointPort, …)
  infrastructure/        # adapters: ingestion, llm, persistence, graphrag, telemetry
  tui/                   # rich dashboard
  cli/                   # typer entrypoint (composition root)
config/
  ontology.yaml
  prompts/               # versioned jinja2 templates
  evals/                 # graphrag eval suite
work/                    # checkpoints + artifacts + sqlite + qdrant (gitignored)
tests/
```

## Where to start reading

1. `application/use_cases/run_extraction.py` — the entire pipeline in one file
2. `application/pipelines/stages.py` — stage names + ordering
3. `application/pipelines/orchestrator.py` — checkpoint-aware DAG runner
4. `infrastructure/telemetry/observability.py` — wide events, heartbeats, token rollups
5. `cli/main.py` — composition root that builds `ExtractionServices` and invokes the use case
