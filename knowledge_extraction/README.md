# Knowledge Extraction

Production-grade PDF knowledge extraction → ontology + property graph → Microsoft GraphRAG.

Two extraction modes:

- **Discovery** — free-form, reasoning-model-driven, emits versioned ontology candidates for human review. Never mutates the canonical ontology.
- **Governed** — schema-guided extraction against an approved ontology version. Validates edges, canonicalizes entities, detects semantic drift, emits refinement proposals.

## Quickstart

```bash
cd knowledge_extraction
uv sync
cp .env.example .env       # fill in Azure endpoints/auth
uv run ke --help
uv run ke preflight
```

End-to-end on the bundled HAI AI Index report:

```bash
uv run ke ingest                          # full ingest+extract for all PDFs in assets/
uv run ke webui --backend lazy
uv run ke stats
```

The Web UI supports retrieval backends (`lazy`, `mini`, `ms`) for side-by-side
demo and debugging flows.

`ke ingest` now builds the Microsoft GraphRAG knowledge tree by default, so
`ms` backend works out of the box. To skip it for faster/local runs:

```bash
uv run ke ingest --no-build-knowledge-tree
```

### Three retrieval modes

| Backend  | Ingestion cost | Per-query cost | When to use |
|----------|----------------|----------------|-------------|
| `mini`   | $0 (chunks only) | ~0 LLM, instant | Lexical baseline; offline; regression tests |
| `ms`     | ~$87 / ~80 min for HAI corpus | 1 LLM call | SOTA: pre-built entity/community graph |
| `lazy`   | **$0 — reuses chunks from any normal ingest** | 2 LLM calls (~10–20 s) | LazyGraphRAG: JIT subgraph at query time, no graph build |

```bash
uv run ke webui --backend lazy
```

`ke webui` includes two pages in one app: **Telemetry** and **Chat**.
For demos, switch `--backend` to compare retrieval styles (`mini`, `lazy`, `ms`)
on the same question set in the Chat page.

#### HAI 2025 benchmark (32-case suite)

| Backend | Pass | MRR  | Avg query (s) | Index ($) | Index time |
|---------|------|------|---------------|-----------|------------|
| mini    | 28/32 | 0.69 | <1 | $0    | n/a        |
| lazy    | 12/32 | 0.78 | ~15 | **$0** | **0**      |
| ms (local) | 5/32 | 0.41 | ~42 | $87.83 | ~80 min   |

Eval bias caveat: the suite scores lexical overlap with chunk text, which
favours `mini` (returns chunks verbatim). `lazy` and `ms` both synthesize
prose, so their pass-rates undercount answer quality. Lazy nonetheless beats
`ms` ~2.4× on this suite at zero ingestion cost. Adversarial refusal: `ms`
2/2, `mini` 2/2, `lazy` 0/2 — lazy is currently too eager to answer
out-of-scope questions; the synthesis prompt is a candidate for a refusal
guard in v1.1.

Discovery run on an unfamiliar corpus:

```bash
uv run ke ingest <doc.pdf> --mode discovery
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
