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

The `graphrag ask` command queries the indexed corpus through Microsoft GraphRAG
by default (entity-aware `local` search and community-aware `global` search). A
lightweight lexical `mini` backend is kept as a deterministic baseline — useful
for offline runs, regression tests, and side-by-side eval comparisons.

### Three retrieval modes

| Backend  | Ingestion cost | Per-query cost | When to use |
|----------|----------------|----------------|-------------|
| `mini`   | $0 (chunks only) | ~0 LLM, instant | Lexical baseline; offline; regression tests |
| `ms`     | ~$87 / ~80 min for HAI corpus | 1 LLM call | SOTA: pre-built entity/community graph |
| `lazy`   | **$0 — reuses chunks from any normal ingest** | 2 LLM calls (~10–20 s) | LazyGraphRAG: JIT subgraph at query time, no graph build |

```bash
uv run ke graphrag ask "..." --backend lazy        # LazyGraphRAG (query-time)
uv run ke graphrag eval --backend ms,lazy,mini     # 3-way comparison
```

`--backend auto` is unchanged: prefers `ms` if an index exists, else falls back
to `mini`. `lazy` is opt-in only (controlled benchmark mode).

`graphrag eval` runs reusable retrieval eval cases from
`config/evals/graphrag_eval.json` (includes a SuperGLUE disambiguation case:
benchmark/model meaning vs adhesive meaning). Defaults to the `ms` backend;
pass a comma-separated list (`mini,lazy,ms` or `mini,ms`) for side-by-side
comparison runs. The legacy `--backend both` shorthand is still accepted as
`mini,ms`.

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
