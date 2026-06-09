# Technical Reference — How It Works

> **Audience:** engineers (and LLM agents) who need to reason about or modify the
> `knowledge_extraction` codebase without re-reading every file. Concise, file-cited,
> no hand-waving.
>
> For the *why* — the methods, their rationale, strengths, and benefits — see the
> companion [Functional Guide](./functional.md). This document covers the *how*.
>
> All paths are relative to `knowledge_extraction/` unless noted.

---

## 1. What the system is

A two-stage Retrieval-Augmented Generation pipeline that turns PDFs into three
parallel, queryable knowledge stores, then exposes **five** retrieval backends over
them.

```
                       ┌─────────────────────────────────┐
                       │       PDF (assets/*.pdf)         │
                       └────────────────┬─────────────────┘
                                        │
  Stage 0  ingest (Azure Doc Intel) ────┤  → markdown + layout.json + tables.json + figures.json
  Stage 1  semantic chunking ───────────┤  → SQLite: chunks(page_start, page_end, section)
  Stage 2  render pages → PNG ──────────┤  → work/artifacts/<doc>/pages/page_NNNN.png
  Stage 3  figures (crop + GPT vision) ─┤  → SQLite: figures + per-chunk figure_refs
  Stage 4  governed LLM extraction ─────┤  → SQLite: entities + relationships + claims
  Stage 5  build property graph ────────┤  → work/graph/*.graphml | .jsonld | .cypher
  Stage 6  MS GraphRAG index (opt) ─────┘  → work/graphrag/<ontology>/output/*.parquet
                                        │
                                        ▼
                    5 retrieval backends share these stores:
                    mini · lazy · ms · agentic · nav
```

On-disk artifacts after a full ingest of one PDF:

```
work/
├── artifacts/<doc-stem>/
│   ├── pages/page_0001.png … page_NNNN.png   (rendered at 150 dpi)
│   ├── figures/figure_0094_002.png …          (cropped from page PNGs)
│   ├── layout.json                            (raw Azure DI response)
│   ├── doc.md                                 (markdown with <!--PageBreak--> markers)
│   ├── tables.json
│   └── figures.json
├── checkpoints/<doc_id>/<stage>/.done         (stage idempotency markers)
├── graph/<doc_id>.<ontology>.graphml/.jsonld/.cypher
├── graphrag/<ontology>/                       (MS GraphRAG workdir)
│   ├── settings.yaml  .env  prompts/
│   ├── input/<chunk_id>.txt                   (one file per chunk)
│   └── output/*.parquet                       (entities, relationships, communities,
│                                               community_reports, text_units, …)
├── knowledge_extraction.db                    (SQLite — schema in §4.1)
└── logs/run-*.jsonl                           (one wide-event per line)
```

---

## 2. Architecture — hexagonal / ports & adapters

Strict layering (enforced by import direction only — no linter):

```
domain        — pure pydantic v2. No I/O. No third-party clients.
application   — use_cases + ports (Protocol) + pipelines + services. Pure logic.
infrastructure— adapters: Azure clients, SQLite, NetworkX, MS GraphRAG, Doc Intel.
cli / tui     — composition root (cli/main.py) wires everything; UI lives here.
```

**Hard rule:** no module in `domain` or `application` may import from `infrastructure`.
The composition root `cli/main.py` builds an `ExtractionServices` bag
(`application/use_cases/run_extraction.py:95-117`) containing every port impl and hands
it to `RunExtractionUseCase.execute()`.

Ports (Python `Protocol`) live in `application/ports/__init__.py`:

| Port                  | Implementation                                                            |
| --------------------- | ------------------------------------------------------------------------- |
| `IngestionPort`       | `infrastructure/ingestion/document_intelligence_adapter.py`               |
| `PageRendererPort`    | `infrastructure/ingestion/pdf_renderer.py` (`PdfPageRenderer`)            |
| `LLMPort`             | `infrastructure/llm/azure_foundry_client.py` (Azure OpenAI / Foundry)     |
| `VisionPort`          | same client, vision-capable model                                         |
| `EmbeddingPort`       | same client, embedding model                                              |
| `RelationalStorePort` | `infrastructure/persistence/sqlite/repositories.py: RelationalRepository` |
| `GraphStorePort`      | `infrastructure/persistence/graph/networkx_store.py`                      |
| `CheckpointPort`      | `infrastructure/checkpointing/filesystem_checkpoint_store.py`             |

`MsGraphRagAgent` (the production retrieval path) shells out to the official `graphrag`
CLI; it does **not** implement a port — it's a stand-alone service in
`application/services/ms_graphrag_agent.py`.

The pipeline is exposed as a single `RunExtractionUseCase.execute(ExtractionRequest)`
call. Wrapping it as an MCP tool, a Foundry skill, or an HTTP handler is a thin shim:
build `ExtractionServices` once at startup, call `execute()` per request.

---

## 3. The pipeline, stage by stage

The orchestrator + stages **are** the system. Read `run_extraction.py` top-to-bottom and
you have understood the codebase.

### 3.1 Entry point

`application/use_cases/run_extraction.py: RunExtractionUseCase.execute()` runs, in order:

```python
slice_pdf_if_requested(pdf, pages_limit)     # optional first-N-pages slice
document = ingest(source_pdf)                 # adapter chain, first hit wins
relational.save_document(document)            # SQLite documents row
chunks   = chunker.chunk(document, markdown)  # section + token-window split
relational.save_chunks(chunks)
relational.relink_chunks_to_figures(doc.id)   # idempotent (no-op on 1st run)
version, schema = onto_service.active(...)    # which ontology to use
orchestrator.add(RENDER,  stage_render)
orchestrator.add(FIGURES, stage_figures, deps=[RENDER])
orchestrator.add(EXTRACT, stage_extract, deps=[RENDER, FIGURES])
orchestrator.add(GRAPH,   stage_graph,   deps=[EXTRACT])     # governed only
await orchestrator.run(resume=request.resume)
```

Stage names are typed (`application/pipelines/stages.py: Stage` enum) — the **single
source of truth** for ordering, on-disk checkpoint paths, and `--redo-stage` validation.
`application/pipelines/orchestrator.py` is a tiny DAG runner that:

1. Resolves stages topologically (deps).
2. Skips stages whose `.done` marker exists at `work/checkpoints/<doc_id>/<stage>/`.
3. Wraps each stage in a `wide_event` + OTEL span with heartbeat + stall warning.

### 3.2 Stage 0 — ingest (PDF → markdown + layout)

`infrastructure/ingestion/document_intelligence_adapter.py: DocumentIntelligenceAdapter`

- Calls Azure AI Document Intelligence (`prebuilt-layout` model).
- **Fallback:** if `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` is unset, falls through to
  `DoclingIngestionAdapter` (local CPU, no Azure). The composition root builds an
  `ingestion_chain` and `pick_first_working_ingestion` tries each adapter in order.
- Persists four artifacts under `work/artifacts/<doc-stem>/`: `layout.json` (raw API
  response), `doc.md` (markdown with `<!-- PageBreak -->` markers), `tables.json`,
  `figures.json`.
- Resume-friendly: if all four exist, `_load_cached_document()` returns immediately
  without an API call (`document_intelligence_adapter.py:29-47`).
- Document id = SHA-256 hash of the PDF bytes — stable across runs.
- Auth via `Settings.azure_auth_mode`: `KEY` → `AzureKeyCredential`; `CREDENTIAL` →
  `DefaultAzureCredential` (managed identity / env / VS / az CLI).

### 3.3 Stage 1 — semantic chunking

`application/pipelines/stage_1_chunking/pipeline.py: SemanticChunker`

- Parses markdown headings (`# .. ######`) → `Section[]`.
- Per section: split body into chunks ~2,400 chars target / 3,600 max, prefer paragraph
  breaks, fall back to sentence breaks (`_split_with_offsets`).
- Computes `(page_start, page_end)` by mapping char offsets to `<!-- PageBreak -->`
  marker offsets (`_page_for_offset`).
- Sets `chunk.table_refs` from `_tables_for_range(page_start, page_end)`.
- Does **NOT** set `chunk.figure_refs` (always `[]`); the figures stage fills these. This
  is why `repositories.save_chunks` preserves existing `figure_refs_json` on update —
  without that guard, every resume run would wipe figure linkage.
- Chunk id = `sha256(document_id + section_id + first_120_chars)[:16]`.

### 3.4 Stage 2 — render

`infrastructure/ingestion/pdf_renderer.py: PdfPageRenderer`

- Renders every page to `pages/page_NNNN.png` at 150 dpi. Pure local CPU. Resumable:
  skips pages whose PNG exists.

### 3.5 Stage 3 — figures (crop + vision interpretation)

`application/pipelines/stage_2c_figure_interpretation.py: FigureInterpretationPipeline`

**Crop pass** (sequential, disk-bound, ~10 ms/figure): for each figure in `layout.json`,
open the page PNG, compute the crop box from polygon coords + DPI, save as
`figures/figure_PPPP_III.png`. id = sha256 of `(doc_id, page, crop_box)`.

**Vision pass** (bounded parallel, network-bound, ~3 s/figure): render the
`figure_interpretation` prompt and POST each cropped figure to the vision model
(`gpt-4.1` by default), returning a `ChartInterpretation` (title, chart_type,
interpretation prose, confidence). Concurrency: `pipeline_concurrency` (default 8) via
`asyncio.Semaphore`. After the pass, `repo.save_figures(...)` writes each chunk's
`figure_refs_json = [fig_id for fig on chunk pages]` — the **single source of truth** for
chunk→figure linkage.

A defensive backstop exists at the use-case level: `run_extraction.py:151` calls
`relink_chunks_to_figures(doc.id)` right after `save_chunks`, rebuilding refs from page
ranges so resume runs (which skip the figures stage) still end with intact linkage.

### 3.6 Stage 4 — governed LLM extraction

`application/pipelines/stage_2a_extraction_governed.py: GovernedExtractionPipeline`

Per chunk (concurrent, `Semaphore(pipeline_concurrency)`):

1. **Resume fast-path:** `_repo.needs_chunk_extraction(chunk.id)` checks the
   `chunk_extractions` table; if already recorded, hydrate from DB and skip the LLM call.
2. Render the `governed_extraction` prompt with `chunk.text`, the `OntologySchema`
   (entity types, relation types, allowed source/target constraints), and surrounding
   context — any tables/figures referenced by this chunk (figure interpretations inlined
   so the LLM "sees" the chart prose).
3. `llm.complete_json(..., max_tokens=4096, temperature=0.0)`.
4. Parse JSON → `ExtractionResult(entities, relationships, claims)`.
5. **Validation pipeline** (3 services, in order):
   - `OntologyValidator(schema)` — drops edges violating `allowed_source/target`.
   - `CanonicalizationService(governance)` — alias table → embedding cosine similarity →
     rapidfuzz fallback; merges duplicates, records merges in `entity_merges`.
   - `DriftDetector(governance, version)` — tags chunks producing many `UNKNOWN` types or
     off-schema attempts → drift events.
6. Off-schema entities become `Entity(type=UNKNOWN)` + a `RefinementSuggestion` queued to
   `ontology_proposals` (mode=governed).
7. **Retry policy** for transient Azure content-filter 400s and 429s:
   `_is_retryable_azure_400(exc)` → exponential backoff (1/2/4/8 s capped), max 5 attempts
   per chunk. If every attempt fails, the chunk is logged + skipped — the pipeline does
   **NOT** abort. Final assertion: at least one chunk must succeed (`successful == 0` →
   `RuntimeError`).

`GovernedExtractionStats` tracks `chunks_processed`, `chunks_resumed`, `chunks_failed`,
`entities_unknown`, `canonical_reused`, `violations_prevented`, etc., emitted as a
`governed` pipeline event.

### 3.7 Stage 5 — graph build (governed only)

`application/pipelines/stage_5_graph.py: GraphBuildPipeline`

Pure in-memory `networkx.MultiDiGraph` build from `ExtractionResult[]` — **no LLM calls**
(hence the explicit `tokens=0 expected` log at `run_extraction.py:216`):

- Each entity → node with `properties={confidence, aliases}`.
- Each claim → `Claim` node (text, confidence, supporting_table_id,
  supporting_figure_id). Auto-creates `Table`/`Figure` nodes with `SUPPORTED_BY_TABLE` /
  `SUPPORTED_BY_FIGURE` edges.
- Each relationship → edge.

Exports the same graph to three formats — GraphML (Gephi), JSON-LD (RDF projection),
Cypher (Neo4j) — all tagged with the active ontology version:
`<doc_id>.<version>.{graphml,jsonld,cypher}`.

### 3.8 Stage 6 — Microsoft GraphRAG index (optional, post-pipeline)

`infrastructure/graphrag/graphrag_runner.py: GraphRagRunner`

Triggered by `ke ingest --build-knowledge-tree` (default ON) **after**
`RunExtractionUseCase.execute` returns. Wraps the official `graphrag` 2.x CLI (the Python
API changes between minor releases; the CLI contract is stable).

`runner.write_inputs(version, chunks)`:

1. `graphrag init --root work/graphrag/<version> --force` — scaffolds prompts, `.env`,
   `settings.yaml`. (Interactively prompts for model names; we pipe `b"\n" * 10` on stdin
   with a 90 s timeout.)
2. Overwrite `settings.yaml` with our Azure-aware template (`_azure_settings_yaml`). Key
   fields: `completion_models:` / `embedding_models:` (graphrag 2.x, **not** the older
   `models:` dict); `auth_method: azure_managed_identity` when
   `AzureAuthMode.CREDENTIAL` (uses `DefaultAzureCredential` via LiteLLM's
   `azure_ad_token_provider`, and the `api_key` field MUST be absent); `azure_deployment_name:`
   per model; workflow refs `completion_model_id:` / `embedding_model_id:`.
3. Overlay any custom prompts from `config/graphrag_prompts/*.txt` (`graphrag init --force`
   always regenerates defaults).
4. Write `input/<chunk_id>.txt` — one text file per chunk. graphrag does its **own**
   chunking/extraction/community detection over this corpus; our pre-extracted ontology
   graph is a **separate** artifact not fed to graphrag.

`runner.index(version)`: wipes `output/`/`cache/`/`logs/`, spawns `graphrag index`,
retries up to 3× with backoff on the transient `"Key based authentication is disabled"`
401. Output parquets in `work/graphrag/<version>/output/`: `communities`,
`community_reports`, `entities`, `relationships`, `text_units` (plus internal). If
`community_reports.parquet` is missing, `graphrag query` throws — re-run
`ke graphrag index`.

---

## 4. Persistence — what lives where

| Concern                                                  | Store                                       |
| -------------------------------------------------------- | ------------------------------------------- |
| Chunks / entities / relationships / claims / prompt log  | SQLite via SQLAlchemy                       |
| Vectors                                                  | Qdrant adapter exists but **not wired** (see §4.4) |
| Property graph                                           | NetworkX in-memory + GraphML/JSON-LD/Cypher exports |
| Page images, layout JSON, markdown, table/figure inventory | Filesystem under `work/artifacts/`        |
| MS GraphRAG artifacts                                    | parquet under `work/graphrag/<version>/`    |

### 4.1 SQLite (`work/knowledge_extraction.db`)

`infrastructure/persistence/sqlite/repositories.py`. Schema is autocreated by
`Base.metadata.create_all(engine)` plus lightweight forward-compat ALTER patches in
`_apply_schema_patches()` — **no Alembic**.

| Table                | Holds                                                            |
| -------------------- | --------------------------------------------------------------- |
| `documents`          | One row per ingested PDF (id = sha256 of bytes, page_count)     |
| `chunks`             | text + page range + section_id + figure_refs_json + table_refs_json |
| `tables`             | Table metadata; cells in `table_cells`                          |
| `figures`            | id, page, crop_box_json, image_path, interpretation_json        |
| `entities`           | id, name, type, confidence, aliases_json                        |
| `relationships`      | source/target ids + type + properties_json                      |
| `claims`             | id, text, confidence, supporting_table_id, supporting_figure_id |
| `chunk_extractions`  | Per-chunk expected counts (the chunk-level checkpoint table)    |
| `prompt_calls`       | Full audit log of LLM calls (system, user, response, tokens)    |
| `ontology_versions`  | Approved versions (semver, YAML blob, status, approvers)        |
| `ontology_proposals` | Discovery candidates + governed refinements (status, source)    |
| `ontology_rejections`| Audit of explicitly rejected proposals                          |
| `entity_aliases`     | canonical_id ↔ alias with provenance                            |
| `entity_merges`      | Merge history from canonicalization service                     |
| `drift_events`       | Drift signals tagged by ontology version                        |

Two idempotent repository methods to know about:

- `save_chunks(chunks)`: on update preserves existing `figure_refs_json` /
  `table_refs_json` if the incoming chunk has none — prevents resume runs from silently
  wiping figure linkage.
- `relink_chunks_to_figures(document_id) -> int`: rebuilds chunk→figure refs from page
  ranges. Cheap, safe to call every run.

`RelationalRepository.__init__` takes a `sessionmaker`, NOT a path. Build it with
`make_engine(path)` then `make_session_factory(engine)` — see `cli/main.py:183-185`.

### 4.2 Filesystem (`work/`)

See §1 for the full layout.

### 4.3 Property graph

NetworkX in-memory store (`infrastructure/persistence/graph/networkx_store.py`). Not
durable — every run rebuilds it from SQLite. The three serialized exports are the durable
form.

### 4.4 Qdrant vector store (infrastructure exists, not wired)

`infrastructure/persistence/qdrant/qdrant_vector_store.py` implements `VectorStorePort`,
and `settings.py` exposes `vector_db_path` / `qdrant_url` / `qdrant_api_key`. However
`VectorStorePort` is **not** imported anywhere except its own definition — it is dead
infrastructure. Canonicalization uses direct embedding calls via `EmbeddingPort`, not a
vector store.

### 4.5 MS GraphRAG index

We do **NOT** read these parquets directly anywhere in this codebase (except the Neo4j
loader, §13); the only consumer is the `graphrag query` subprocess.

---

## 5. Observability

`infrastructure/telemetry/observability.py` is the **only** observability substrate;
everything else calls into it.

- `wide_event(name, **fields)` context manager: one structured JSON line per logical
  operation. Bound run/document/stage context auto-merged via `bind(...)`/`bound(...)`.
  Records `event`, `duration_ms`, `status` (ok/error), `input_tokens_self` /
  `output_tokens_self` (just this op), `input_tokens_total` / `output_tokens_total` (self +
  nested children), plus any custom kwargs.
- A daemon heartbeat thread emits `{event}.heartbeat` every
  `OBSERVABILITY_HEARTBEAT_INTERVAL_SECONDS` (default 30 s) and a one-time
  `{event}.stalled` warning at `OBSERVABILITY_STALL_THRESHOLD_SECONDS` (120 s).
- OTEL spans mirror stage boundaries (`stage.<name>`). Enable with `OTEL_ENABLED=1` +
  `OTEL_EXPORTER_OTLP_ENDPOINT=...`; otherwise spans land in `work/telemetry/spans.jsonl`.
- Per-run JSONL log at `work/logs/run-<id>.jsonl` containing every wide event — the
  canonical forensics artifact.
- A `run.finish` event aggregates totals: `input_tokens`, `output_tokens`,
  `total_tokens`, models touched.

Token usage rolls up hierarchically — outer spans see their own LLM cost plus everything
nested inside.

---

## 6. Checkpointing, resilience & resume semantics

- **Stage-level checkpoints:** `.done` markers under `work/checkpoints/<doc_id>/<stage>/`
  (`infrastructure/checkpointing/filesystem_checkpoint_store.py`). Skipped stages do NOT
  execute their stage closure.
- **Chunk-level checkpoint:** the `chunk_extractions` table records expected
  entity/relationship/claim counts per chunk. On resume the extract stage short-circuits
  per-chunk if counts match (graph build still gets a full hydrated result set).
- **Per-chunk fault isolation:** a single chunk's failure does not abort the pipeline;
  it's logged and counted in `stats.chunks_failed`. The graph stage only sees successful
  results. Hard abort only if **all** chunks fail.
- **Azure 400 content-filter retry:** 5 attempts with exponential backoff (1/2/4/8 s).
  Many Azure RAI filter false-positives clear on retry.
- **`--redo-stage <stage>`:** clears checkpoints from that stage downward and re-runs
  (`run_extraction.py: _cascade_redo`).
- **`--fresh`:** nukes `work/` for this PDF before starting (full rebuild).
- **Schema migrations:** lightweight `ALTER TABLE` patches in `_apply_schema_patches()`
  run at engine creation. Forward-compatible only.

---

## 7. Configuration

`config/settings.py: Settings` (pydantic-settings). Reads `.env` then env vars.

| Setting                                | Purpose                                                |
| -------------------------------------- | ------------------------------------------------------ |
| `azure_openai_endpoint`                | `https://<resource>.openai.azure.com`                  |
| `azure_openai_api_key`                 | Only if `azure_auth_mode=key`                          |
| `azure_auth_mode`                      | `key` or `credential` (DefaultAzureCredential)         |
| `azure_openai_reasoning_model`         | Discovery + ontology proposal + agentic (default `o4-mini`) |
| `azure_openai_extraction_model`        | Governed extraction (default `gpt-4.1-mini`)           |
| `azure_openai_vision_model`            | Figure interpretation (default `gpt-4.1`)              |
| `azure_openai_embedding_model`         | Canonicalization + clustering (default `text-embedding-3-large`) |
| `azure_document_intelligence_endpoint` | Stage 0 ingest                                         |
| `pipeline_concurrency`                 | asyncio.Semaphore for figures + extract (default 8)    |
| `graphrag_executable`                  | Override path to `graphrag.exe` (Windows long-path)    |
| `default_mode`                         | `governed` or `discovery`                              |
| `active_ontology_version`              | Pin to a version, else newest approved                 |
| `observability_*`                      | Heartbeat + stall thresholds                           |
| `agentic_planner_model`                | Override agentic planner (default: reasoning_model)    |
| `agentic_critic_model`                 | Override agentic critic (default: reasoning_model)     |
| `agentic_synthesis_model`              | Override agentic synthesis (default: extraction_model) |
| `agentic_max_rounds`                   | Hard cap on agentic retrieval rounds (default 2)       |
| `agentic_max_subquestions`             | Max subquestions per plan (default 5)                  |
| `agentic_top_k_per_query`              | top-k chunks per subquery (default 8)                  |

**Foundry key-auth quirk:** if the Azure OpenAI resource has key auth disabled by policy,
you must (1) set tag `SecurityControl=Ignore` on the resource and (2) enable key-based
auth. Otherwise use `azure_auth_mode=credential`.

**Windows + graphrag:** install graphrag in a short-path venv (e.g. `C:\g`) and set
`GRAPHRAG_EXECUTABLE=C:\g\Scripts\graphrag.exe` — LiteLLM has a long-path import bug that
breaks the standard venv path.

**Windows + UTF-8:** CLIs reconfigure `sys.stdout`/`stderr` to `encoding="utf-8",
errors="replace"` before constructing the Rich Console, and pass `PYTHONUTF8=1` /
`PYTHONIOENCODING=utf-8` to Python subprocesses so their stdout decodes cleanly.

---

## 8. CLI surface

`pyproject.toml: [project.scripts] ke = "knowledge_extraction.cli.main:app"`. Invoke with
`uv run ke <command>`.

| Command                              | Purpose                                                    |
| ------------------------------------ | --------------------------------------------------------- |
| `ke preflight [--live] [--graphrag]` | Config / auth checks before a heavy run                   |
| `ke ingest [pdf\|dir]`               | Full pipeline. Defaults to all PDFs in `assets/`.         |
| `ke ingest --mode discovery`         | Discovery extraction instead of governed                  |
| `ke ingest --pages N`                | First-N-page slice                                        |
| `ke ingest --redo-stage <stage>`     | Clear from that stage down and re-run                     |
| `ke ingest --fresh`                  | Wipe `work/` first                                        |
| `ke ingest --build-knowledge-tree`   | Also build MS GraphRAG index after extraction (default ON)|
| `ke resume <pdf>`                    | Re-run; checkpointed stages are skipped                   |
| `ke stats`                           | Persistence + governance + drift summary                  |
| `ke clean [--yes]`                   | Wipe all derived state (keeps `assets/` and config)       |
| `ke webui [--backend …] [--port N]`  | Streamlit UI (Telemetry + Chat pages)                     |
| `ke graphrag index`                  | (Re)build the MS GraphRAG index from existing chunks      |
| `ke graphrag ask <q> --backend …`    | One-shot question (`--method local\|global\|drift\|basic\|auto`) |
| `ke graphrag eval --backend …`       | Run a scored eval suite, optionally side-by-side          |
| `ke graphrag bench --backend …`      | 4-way comparison: quality + latency + tokens + ingest cost|
| `ke ontology list\|show\|diff\|approve\|reject\|propose\|migrate` | Manage ontology versions |
| `ke neo4j up\|down\|open\|load\|wipe` | Optional Neo4j visualization layer (§13)                  |

`--backend` accepts `mini`, `lazy`, `ms`, `agentic`, `nav`, `auto` (and comma-lists for
`eval`/`bench`).

The eval framework (`application/services/graphrag_eval.py`) produces per-case metrics
(MRR, precision@k, recall@k, citation_recall, top_score) plus per-category aggregates;
adversarial cases pass when `top_score < min_score_for_grounded`. Suite at
`config/evals/graphrag_eval.json`.

The benchmark harness (`application/services/benchmark.py` pure logic + the `graphrag
bench` command) reuses the eval runners, then rolls up per-case telemetry
(`latency_ms / tokens_in / tokens_out / extra`) into per-backend cost/latency
(p50/p95/total) via `summarize_cost`. Ingestion cost is read from `work/logs/run-*.jsonl`
(latest `ingest` run's `run.finish`) and the MS index runtime from GraphRAG's
`stats.json`. Tokens are `None` (→ `n/a`) when a backend never exposes them (`ms`),
distinct from `0` for `mini` (no LLM). Curated suite at `config/evals/bench_4way.json`;
artifacts land in `work/benchmarks/`.

---

## 9. Dev workflow

```powershell
cd C:\_CODE\AdvancedRAG\knowledge_extraction
uv sync                     # install deps (add --extra neo4j / --extra tour as needed)
uv run ruff check .         # lint (line-length 110, E501 ignored)
uv run ruff format .        # format
uv run mypy                 # strict type-check
uv run pytest -q            # all tests (asyncio_mode=auto)
uv run pytest -q tests/unit/test_chunker.py::test_name   # single test
uv run ke webui             # local Streamlit UI
```

Tests of note:

- `tests/integration/test_smoke.py` — end-to-end pipeline smoke test.
- `tests/unit/test_figure_pipeline.py` — figures stage + `save_chunks` preservation +
  `relink_chunks_to_figures` regression.
- `tests/unit/test_ms_graphrag_agent.py` — both sync and async paths.
- `tests/unit/test_graphrag_runner.py` — `settings.yaml` templating.
- `tests/unit/test_preflight.py` — index-completeness checks.
- `tests/unit/test_azure_foundry_json.py` — defensive JSON extraction (see gotcha 11).

---

## 10. Common gotchas (read before touching anything)

1. **`Chunk.figure_refs` is always `[]` from the chunker.** Filled in by the figures
   stage. `save_chunks` guards against blowing it away, and `relink_chunks_to_figures` is
   a backstop after every `save_chunks`.
2. **`graphrag init --force` overwrites `prompts/`, `settings.yaml`, AND `.env`** every
   time. Custom prompts must be re-applied; `runner._overlay_custom_prompts` handles this
   from `config/graphrag_prompts/`.
3. **graphrag 2.x YAML schema ≠ 1.x.** Use `completion_models:` / `embedding_models:`
   dicts (not `models:`), `auth_method:` (not `auth_type:`), `azure_deployment_name:`,
   `chunking:` (not `chunks:`), top-level `input_storage:` / `output_storage:`.
4. **`auth_method: azure_managed_identity` works with graphrag 2.7.2** via LiteLLM's
   `azure_ad_token_provider`. Do NOT also set `api_key` in CREDENTIAL mode.
5. **`asyncio.run` inside Streamlit on Windows can hang silently.** `MsGraphRagAgent.ask()`
   uses `subprocess.run` (sync); `ask_async` is only for non-Streamlit callers.
6. **HAI report figure captions are empty strings** (`""`, not `None`). UI uses
   `caption or figure_id` as label fallback.
7. **Two figure id flavours per page:** synthetic placeholders like `94.1` (no
   `image_path`) AND real hashed ids like `e4881d6ff3ef5ff6` (with `image_path`). Always
   check `image_path` before rendering a thumbnail.
8. **`.first1.pdf` artifacts are debug slices** from `--pages 1` runs; they pollute the
   `documents` table with stale truncated copies.
9. **MS GraphRAG query is slow** (~30-40 s `local`, ~200 s `global`). Always wrap UI calls
   in a spinner; default agent timeout is 300 s.
10. **Eval lexical scoring favours BM25 over LLM synthesis.** When comparing MS vs mini,
    build `positive_terms` keyed to the **answer prose**, not just keywords.
11. **gpt-5.x via `complete_json` sometimes emits its JSON object twice** (concatenated).
    `_extract_json` must return the FIRST brace-matched object, not first-`{`..last-`}`.

---

## 11. Where to start when modifying

| Goal                                | Start at                                                                        |
| ----------------------------------- | ------------------------------------------------------------------------------- |
| Add / reorder a pipeline stage      | `application/pipelines/stages.py` (enum) + `run_extraction.py`                  |
| Add a new retrieval backend         | New service in `application/services/`, then wire the id into **both** `cli/main.py` (parse/valid sets, ask/eval/bench dispatch, `_build_*`+`_run_*_eval`, webui valid set) and `cli/webui_app.py` (`BACKEND_LABELS`, `backend_options`, default valid set, `_build_*`, chat branch) |
| Change ontology schema              | `config/ontologies/<name>.yaml` + `ke ontology approve`                         |
| Change MS GraphRAG config           | `graphrag_runner.py: _azure_settings_yaml()` + reindex                          |
| Add a new ingestion adapter         | Implement `IngestionPort`; prepend to `ingestion_chain` in the composition root |
| Tune chunk sizes                    | `SemanticChunker(target_chars, max_chars)` in `cli/main.py`                     |
| Tune extraction concurrency         | `Settings.pipeline_concurrency`                                                 |
| Add a new figure cropping strategy  | `FigureInterpretationPipeline._figure_specs_from_document`                      |
| Add a new prompt                    | `config/prompts/<name>.v<n>.j2` + `PromptRegistry.render(name, version, **ctx)` |
| Modify eval scoring                 | `application/services/graphrag_eval.py`                                         |
| Debug a stuck run                   | Read the latest `work/logs/run-*.jsonl` (one event per line)                    |

---

## 12. File layout (essential paths only)

```
knowledge_extraction/
├── knowledge_extraction/
│   ├── domain/                       # pure pydantic models (Document, Chunk, Figure, Entity, …)
│   ├── application/
│   │   ├── ports/__init__.py         # all Protocol definitions
│   │   ├── pipelines/
│   │   │   ├── stages.py             # Stage enum (single source of truth)
│   │   │   ├── orchestrator.py       # the DAG runner
│   │   │   ├── stage_1_chunking/pipeline.py
│   │   │   ├── stage_2a_extraction_governed.py
│   │   │   ├── stage_2b_extraction_discovery.py
│   │   │   ├── stage_2c_figure_interpretation.py
│   │   │   ├── stage_3_semantic_clustering.py
│   │   │   ├── stage_4_ontology_proposal.py
│   │   │   └── stage_5_graph.py
│   │   ├── services/                 # cross-stage logic
│   │   │   ├── ms_graphrag_agent.py        # production retrieval (MS GraphRAG CLI)
│   │   │   ├── lazy_graphrag_agent.py      # JIT subgraph retrieval
│   │   │   ├── graphrag_agent.py           # MiniGraphRagAgent (lexical baseline)
│   │   │   ├── agentic_search_agent.py     # bounded multi-step agentic loop
│   │   │   ├── agentic_nav_agent.py        # Agentic Navigator (metadata routing + ReAct)
│   │   │   ├── document_navigator.py       # read-only doc navigation tools for nav
│   │   │   ├── chunk_retriever.py          # BM25 over chunks
│   │   │   ├── canonicalization_service.py
│   │   │   ├── ontology_governance.py
│   │   │   ├── ontology_validator.py
│   │   │   ├── drift_detector.py
│   │   │   ├── prompt_registry.py
│   │   │   ├── query_rewriter.py           # RRF + LLM/lexical rewrites
│   │   │   ├── graphrag_eval.py            # eval harness
│   │   │   └── benchmark.py                # 4-way bench: cost/latency rollup + report
│   │   └── use_cases/run_extraction.py     # THE pipeline entry point
│   ├── infrastructure/
│   │   ├── ingestion/document_intelligence_adapter.py
│   │   ├── ingestion/docling_adapter.py    # local fallback (no Azure DI needed)
│   │   ├── ingestion/pdf_renderer.py       # PdfPageRenderer (pypdfium2)
│   │   ├── llm/azure_foundry_client.py
│   │   ├── persistence/
│   │   │   ├── sqlite/{models.py,repositories.py}
│   │   │   ├── graph/networkx_store.py
│   │   │   └── checkpoints.py
│   │   ├── graphrag/graphrag_runner.py
│   │   ├── neo4j/parquet_loader.py         # MS GraphRAG parquets → Neo4j (demo-only)
│   │   └── telemetry/observability.py
│   ├── cli/
│   │   ├── main.py                   # composition root + Typer commands
│   │   └── webui_app.py              # Streamlit UI
│   └── config/settings.py
├── assets/*.pdf                      # documents to ingest
├── config/
│   ├── ontologies/*.yaml
│   ├── prompts/<name>.v1.j2          # versioned jinja2 templates (SYSTEM: / USER: sections)
│   ├── graphrag_prompts/*.txt        # overlay templates for `graphrag init`
│   └── evals/{graphrag_eval.json, bench_4way.json}
├── tests/{unit,integration}/
└── work/                             # all runtime artifacts (gitignored)

# repo root (C:/_CODE/AdvancedRAG/):
infrastructure/neo4j/
├── docker-compose.yml                # Neo4j 5 + APOC + GDS; `ke neo4j up` uses this
├── .env.example
└── demo_queries.cypher               # ready-to-paste Cypher queries
```

---

## 13. Neo4j visualization layer (demo-only, opt-in)

**Purpose.** MS GraphRAG produces parquet files + LLM answers — the underlying knowledge
graph is invisible. For demos and ad-hoc analysis we ship an optional Neo4j stack to SEE
the graph: entities, relationships, communities, and the actual paths GraphRAG traverses
on multi-hop questions.

**Surface area** (zero impact on the base pipeline):

| Component    | Path                                                | Notes                                                                              |
| ------------ | --------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Docker stack | `infrastructure/neo4j/docker-compose.yml`           | Neo4j `5.24-community` + APOC + GDS, persistent volumes, ports `7474`/`7687`. Default password `graphrag-demo`. |
| Demo queries | `infrastructure/neo4j/demo_queries.cypher`          | Ready-to-paste Cypher (neighborhoods, shortest path, communities, PageRank).       |
| Loader       | `knowledge_extraction/infrastructure/neo4j/parquet_loader.py` | Pure-Python payload builders + `GraphRagNeo4jLoader`. Idempotent `MERGE` batched via `UNWIND`. |
| CLI          | `ke neo4j {up,down,open,load,wipe}` in `cli/main.py`| `up`/`down` shell out to `docker compose`; `load` pushes latest parquets; `wipe` truncates nodes/rels. |
| Tests        | `tests/unit/test_neo4j_loader.py`                   | Pure-Python (no live DB) — payload shapes, numpy coercion, truncation.             |
| Extras       | `pyproject.toml → [project.optional-dependencies] neo4j` | `uv sync --extra neo4j` to install the driver — kept out of the base install.  |

**Graph schema written to Neo4j:**

```cypher
(:Entity    {id, human_id, title, type, description, frequency, degree})
(:Community {community, level, parent_id, title, size, summary, rating, rank, full_content})
(:TextUnit  {id, human_id, n_tokens})

(:Entity)-[:RELATED_TO {weight, description}]->(:Entity)
(:Entity)-[:IN_COMMUNITY {level}]->(:Community)
(:Community)-[:PARENT_OF]->(:Community)
(:Entity)-[:MENTIONED_IN]->(:TextUnit)
```

**Loader keying — the one non-obvious detail.** `relationships.parquet` stores `source`
and `target` as **entity TITLES** (upper-cased), not UUIDs — so the loader `MATCH`es
endpoints by `title`. `text_units.parquet`'s `entity_ids` column contains UUIDs, so
`MENTIONED_IN` matches by `id`. Relationships whose endpoint title is missing from
`entities.parquet` are counted in `skipped_relationships_missing_endpoint` (should be `0`
for clean runs).

**Lifecycle:**

```bash
uv sync --extra neo4j                  # one-time: install the driver
uv run ke neo4j up                     # one-time: start Docker container
uv run ke neo4j load                   # auto-detects latest work/graphrag/<ver>/output/
uv run ke neo4j open                   # opens http://localhost:7474
uv run ke neo4j down                   # stop container (data persisted on volume)
uv run ke neo4j wipe                   # truncate all graph data (keeps constraints)
```

Defaults: `bolt://localhost:7687`, user `neo4j`, password `graphrag-demo`, database
`neo4j`. Overrideable via `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` or
per-command flags. First load on the bundled corpus (8.6k entities, 21.7k relationships,
1.8k communities, 1k text units) completes in ~25 s on a laptop.
