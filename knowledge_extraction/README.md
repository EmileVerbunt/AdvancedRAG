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
uv run ke stats
```

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
  domain/            # pure pydantic models, no I/O
  application/       # ports + pipelines + services
  infrastructure/    # adapters: ingestion, llm, persistence, graphrag, telemetry
  tui/               # rich dashboard
  cli/               # typer entrypoint
config/
  ontology.yaml
  prompts/           # versioned jinja2 templates
work/                # checkpoints + artifacts + sqlite + qdrant (gitignored)
tests/
```
