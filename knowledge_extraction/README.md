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

The Web UI supports four retrieval backends (`mini`, `lazy`, `ms`, `agentic`) for side-by-side
demo and debugging flows.

`ke ingest` now builds the Microsoft GraphRAG knowledge tree by default, so
`ms` backend works out of the box. To skip it for faster/local runs:

```bash
uv run ke ingest --no-build-knowledge-tree
```

### Four retrieval modes

| Backend   | Ingestion cost | Per-query cost | When to use |
|-----------|----------------|----------------|-------------|
| `mini`    | $0 (chunks only) | ~0 LLM, instant | Lexical baseline; offline; regression tests |
| `ms`      | ~$87 / ~80 min for HAI corpus | 1 LLM call | SOTA: pre-built entity/community graph |
| `lazy`    | **$0 — reuses chunks from any normal ingest** | 2 LLM calls (~10–20 s) | LazyGraphRAG: JIT subgraph at query time, no graph build |
| `agentic` | $0 | 4–10 LLM calls (~20–60 s) | Broad/ambiguous questions; multi-source reasoning; first retrieval likely incomplete |
| `nav`     | $0 | 3–8 LLM calls (~15–45 s) | Agentic Navigator: metadata-first routing, then opens the actual document and drills into sections/tables/figures on demand |

**Compute placement:**

| Backend   | Compute spent when |
|-----------|--------------------|
| `ms`      | Upfront (index build) |
| `lazy`    | At query time (graph construction) |
| `agentic` | At query time (planning + reasoning + critique) |
| `nav`     | At query time (metadata routing + document navigation) |

```bash
uv run ke webui --backend lazy
uv run ke webui --backend agentic
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

#### 4-way comparison (`ke graphrag bench`)

For an apples-to-apples comparison across all four backends on a small curated
suite — measuring quality **and** cost — use `graphrag bench`:

```bash
uv run ke graphrag bench --backend mini,lazy,ms,agentic
```

It runs the curated 6-case suite (`config/evals/bench_4way.json`), then reports:

- per-backend quality (pass rate, avg MRR) and the per-case win/loss table;
- per-query **latency** (p50 / p95 / total) and **token cost** (in / out / total),
  with `n/a` where a backend does not expose token counts (`ms`);
- the one-off **ingestion cost** (time + tokens) read from `work/logs/run-*.jsonl`,
  plus the MS GraphRAG index runtime from its `stats.json`.

Results are written to `work/benchmarks/bench-<ts>.{json,md}`. Tokens are `0`
(not `n/a`) for `mini` because it never calls an LLM; the distinction between
"zero cost" and "not measured" is preserved throughout.

## Visualize the graph (Neo4j)

After an MS GraphRAG ingest, you can browse the knowledge graph visually in
Neo4j Browser — great for demos and for spotting why GraphRAG beats lexical
retrieval on multi-hop questions.

```bash
# 1. Install the extra (one-time)
uv sync --extra neo4j

# 2. Bring up Neo4j 5 + APOC + GDS in Docker (one-time)
uv run ke neo4j up

# 3. Load the latest MS GraphRAG parquets (auto-detects work/graphrag/<ver>/output/)
uv run ke neo4j load

# 4. Open Neo4j Browser (default: http://localhost:7474, neo4j / graphrag-demo)
uv run ke neo4j open
```

Paste queries from `../infrastructure/neo4j/demo_queries.cypher` (repo-root path;
character subgraph, shortest-path traversal, top-degree entities, PageRank, etc.).
`ke neo4j down` stops the container; `ke neo4j wipe` clears all graph data.

## Architecture & deeper docs

Comprehensive documentation lives in [`../docs`](../docs/README.md):

- **[Functional Guide](../docs/functional.md)** — which methods are applied, how, why, and
  their strengths/benefits: extraction modes, ontology governance, the five retrieval
  backends, benchmarks, evaluation.
- **[Technical Reference](../docs/technical.md)** — how it works internally: layered domain
  → application → infrastructure with adapters behind ports (ingestion, LLM, vision,
  embeddings, graph store, relational store, checkpoints), the pipeline stage by stage,
  persistence, observability, resilience, configuration, gotchas, and the Neo4j layer.

## Recommended models

| Role        | Recommendation                                  |
|-------------|-------------------------------------------------|
| Reasoning   | `o4-mini` or `o3` (Discovery mode + agentic planner/critic) |
| Extraction  | `gpt-4.1-mini` / `gpt-4o-mini` (JSON mode, agentic synthesis) |
| Vision      | `gpt-4.1` / `gpt-4o`                            |
| Embeddings  | `text-embedding-3-large` (3072d)                |

### Agentic search settings (`.env`)

```env
# Optional model overrides — fall back to reasoning/extraction models when unset
AZURE_OPENAI_AGENTIC_PLANNER_MODEL=     # defaults to AZURE_OPENAI_REASONING_MODEL
AZURE_OPENAI_AGENTIC_CRITIC_MODEL=      # defaults to AZURE_OPENAI_REASONING_MODEL
AZURE_OPENAI_AGENTIC_SYNTHESIS_MODEL=   # defaults to AZURE_OPENAI_EXTRACTION_MODEL

# Loop bounds
AGENTIC_MAX_ROUNDS=2
AGENTIC_MAX_SUBQUESTIONS=5
AGENTIC_TOP_K_PER_QUERY=8

# Agentic Navigator (`nav`) — metadata routing + on-demand document reading
AGENTIC_NAV_MAX_DOCS=3                   # max documents the router may select
AGENTIC_NAV_MAX_STEPS=6                  # max tool-navigation steps
AGENTIC_NAV_MAX_CHARS=4000              # per-observation truncation budget
AGENTIC_NAV_ROUTER_MODEL=               # defaults to AZURE_OPENAI_REASONING_MODEL
AGENTIC_NAV_NAVIGATOR_MODEL=            # defaults to AZURE_OPENAI_REASONING_MODEL
AGENTIC_NAV_SYNTHESIS_MODEL=            # defaults to AZURE_OPENAI_EXTRACTION_MODEL
```

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

## CLI reference

```
ke preflight [--live/--no-live] [--graphrag]       # config / auth checks before a heavy run
ke ingest [pdf|dir] [--mode discovery|governed]    # extract one PDF, a directory, or all assets/
          [--pages N] [--fresh] [--redo-stage STAGE]
          [--build-knowledge-tree/--no-build-knowledge-tree]
ke resume <pdf> [--mode …] [--pages N]             # re-run; already-checkpointed stages are skipped
ke stats                                           # persistence + governance + drift summary
ke clean [--yes]                                   # wipe all derived state (keeps assets/ and config)
ke webui [--backend lazy|mini|ms|agentic|nav] [--port 8502]    # Streamlit UI (Telemetry + Chat pages)

ke ontology list                                   # show all versions and proposals
ke ontology show <version>                         # print ontology YAML
ke ontology diff <a> <b>                           # diff two ontology versions
ke ontology approve <proposal_id> [--by NAME]      # promote proposal → new ontology version
ke ontology reject  <proposal_id> --reason TEXT
ke ontology propose <yaml_file>  [--base VERSION]  # submit a YAML as a new proposal
ke ontology migrate <from_version> <to_version>    # relabel existing graph nodes

ke graphrag index                                  # (re-)run Microsoft GraphRAG indexing
ke graphrag ask <question> [--backend ms|lazy|mini|agentic|nav|auto]
                            [--method local|global|drift|basic|auto]
                            [--top-k N] [--rewrite none|lexical|llm] [--json]
ke graphrag eval [--suite PATH] [--backend ms|lazy|mini|agentic|nav|both|<comma-list>]
                 [--method …] [--json]             # run scored eval suite against one or more backends
ke graphrag bench [--backend mini,lazy,ms,agentic] [--suite PATH] [--out-dir DIR]
                  # 4-way comparison: quality + latency + tokens + ingest cost → JSON + markdown

ke neo4j up      # start Neo4j 5 + APOC + GDS in Docker
ke neo4j load    # import latest GraphRAG parquets into Neo4j
ke neo4j open    # open Neo4j Browser in the default browser
ke neo4j down    # stop the Neo4j container
ke neo4j wipe    # clear all graph data from Neo4j
```
