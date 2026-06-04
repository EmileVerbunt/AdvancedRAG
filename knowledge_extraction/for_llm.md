# `knowledge_extraction` — technical reference for an LLM agent

> Audience: another LLM that needs to reason about or modify this codebase
> without re-reading every file. Concise, file-cited, no hand-waving.
>
> Repo root for everything in this document: `C:/_CODE/AdvancedRAG/knowledge_extraction/`.

## 1. What this system is

A two-stage Retrieval-Augmented Generation pipeline that turns PDFs into three
parallel, queryable knowledge stores and then exposes four retrieval backends
over them:

```
                       ┌─────────────────────────────────┐
                       │       PDF (assets/*.pdf)        │
                       └────────────────┬────────────────┘
                                        │
                    ┌───────────────────┴──────────────────┐
                    │ Stage 0  ingest (Azure Doc Intel)    │  → markdown + layout.json + tables.json + figures.json
                    └───────────────────┬──────────────────┘
                                        │
                    ┌───────────────────┴──────────────────┐
                    │ Stage 1  semantic chunking           │  → SQLite: chunks(page_start, page_end, section)
                    └───────────────────┬──────────────────┘
                                        │
                    ┌───────────────────┴──────────────────┐
                    │ Stage 2  render pages → PNG (pypdfium)│  → work/artifacts/<doc>/pages/page_NNNN.png
                    └───────────────────┬──────────────────┘
                                        │
                    ┌───────────────────┴──────────────────┐
                    │ Stage 3  figures (crop + GPT vision) │  → SQLite: figures + per-chunk figure_refs
                    └───────────────────┬──────────────────┘
                                        │
                    ┌───────────────────┴──────────────────┐
                    │ Stage 4  governed LLM extraction     │  → SQLite: entities + relationships + claims
                    └───────────────────┬──────────────────┘
                                        │
                    ┌───────────────────┴──────────────────┐
                    │ Stage 5  build property graph        │  → work/graph/*.graphml | .jsonld | .cypher
                    └───────────────────┬──────────────────┘
                                        │
                    ┌───────────────────┴──────────────────┐
                    │ Stage 6  MS GraphRAG index           │  → work/graphrag/<ontology>/output/*.parquet
                    └──────────────────────────────────────┘
                                        │
                                        ▼
                       3 retrieval backends share these stores:
                       mini (lexical) · lazy (JIT) · ms (Microsoft GraphRAG)
                       + 1 reasoning backend: agentic (bounded multi-step loop)
```

The on-disk artifacts after a full ingest of one PDF:

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
│   └── output/*.parquet                       (entities, relationships,
│                                               communities, community_reports,
│                                               text_units, ... )
├── knowledge_extraction.db                    (SQLite — see schema below)
└── logs/run-*.jsonl                           (one wide-event per line)
```

---

## 2. Architecture — hexagonal / ports & adapters

Strict layering (enforced by import direction only — no linter):

```
domain        — pure dataclasses / pydantic. No I/O. No third-party clients.
application   — use_cases + ports (Protocol) + pipelines + services. Pure logic.
infrastructure— adapters: Azure clients, SQLite, NetworkX, MS GraphRAG, Doc Intel.
cli / tui     — composition root (cli/main.py) wires everything; UI lives here.
```

The composition root is `knowledge_extraction/cli/main.py`. It builds an
`ExtractionServices` bag (`application/use_cases/run_extraction.py:95-117`)
containing every port impl and hands it to `RunExtractionUseCase.execute()`.

Ports (Python `Protocol`) live in `application/ports/__init__.py`:

| Port                  | Implementation                                                         |
| --------------------- | ---------------------------------------------------------------------- |
| `IngestionPort`       | `infrastructure/ingestion/document_intelligence_adapter.py`            |
| `PageRendererPort`    | `infrastructure/ingestion/pdf_renderer.py` (`PdfPageRenderer`)         |
| `LLMPort`             | `infrastructure/llm/azure_foundry_client.py` (Azure OpenAI / Foundry)  |
| `VisionPort`          | same client, vision-capable model                                      |
| `EmbeddingPort`       | same client, embedding model                                           |
| `RelationalStorePort` | `infrastructure/persistence/sqlite/repositories.py: RelationalRepository` |
| `GraphStorePort`      | `infrastructure/persistence/graph/networkx_store.py`                   |
| `CheckpointPort`      | `infrastructure/checkpointing/filesystem_checkpoint_store.py`          |

`MsGraphRagAgent` (the production retrieval path) shells out to the official
`graphrag` CLI; it does **not** implement a port — it's a stand-alone service
in `application/services/ms_graphrag_agent.py`.

---

## 3. The pipeline, file by file

The orchestrator + stages are *the* system. Read `run_extraction.py`
top-to-bottom and you have understood the codebase.

### 3.1 Entry point

`application/use_cases/run_extraction.py: RunExtractionUseCase.execute()`
runs (in order):

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

Stage names are typed (`application/pipelines/stages.py: Stage` enum). The
orchestrator is a tiny DAG runner that:

1. Resolves stages topologically (deps).
2. Skips stages whose `.done` marker exists at `work/checkpoints/<doc_id>/<stage>/`.
3. Wraps each stage in a `wide_event` + OTEL span with heartbeat + stall warning.

### 3.2 Stage 0 — ingest (PDF → markdown + layout)

`infrastructure/ingestion/document_intelligence_adapter.py: DocumentIntelligenceAdapter`

- Calls Azure AI Document Intelligence (`prebuilt-layout` model).
- **Fallback**: if `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` is not set, falls through
  to `DoclingIngestionAdapter` (local CPU, no Azure dependency). The composition
  root in `cli/main.py: _build_services` builds an `ingestion_chain` and
  `pick_first_working_ingestion` tries each adapter in order.
- Persists four artifacts under `work/artifacts/<doc-stem>/`:
  - `layout.json` (raw API response)
  - `doc.md` (markdown with `<!-- PageBreak -->` markers)
  - `tables.json` (`Table[]` with cell-level bounding boxes)
  - `figures.json` (`Figure[]` with bounding regions, spans, captions)
- Resume-friendly: if all four exist, `_load_cached_document()` returns
  immediately without an API call (`document_intelligence_adapter.py:29-47`).
- Document id = SHA-256 hash of the PDF bytes — stable across runs.
- Auth selected by `Settings.azure_auth_mode`:
  - `KEY` → `AzureKeyCredential`
  - `CREDENTIAL` → `DefaultAzureCredential` (managed identity / env / VS / az CLI)

### 3.3 Stage 1 — semantic chunking

`application/pipelines/stage_1_chunking/pipeline.py: SemanticChunker`

- Parses markdown headings (`# .. ######`) → `Section[]`.
- For each section: split body into chunks of ~2,400 chars target / 3,600 max,
  prefer paragraph breaks, fall back to sentence breaks (`_split_with_offsets`).
- For each chunk computes `(page_start, page_end)` by mapping absolute character
  offsets to the offsets of `<!-- PageBreak -->` markers (`_page_for_offset`).
- Sets `chunk.table_refs` from `_tables_for_range(page_start, page_end)`.
- Does **NOT** set `chunk.figure_refs` (always `[]`); figures stage fills these.
  This is why `repositories.save_chunks` preserves existing `figure_refs_json`
  on update — without that guard, every resume run would wipe figure linkage.
- Chunk id = `sha256(document_id + section_id + first_120_chars)[:16]`.

### 3.4 Stage 2 — render

`infrastructure/ingestion/pdf_renderer.py: PdfPageRenderer`

- Renders every page to `work/artifacts/<doc-stem>/pages/page_NNNN.png` at 150 dpi.
- Pure local CPU; no external services. Resumable: skips pages whose PNG exists.

### 3.5 Stage 3 — figures (crop + vision interpretation)

`application/pipelines/stage_2c_figure_interpretation.py: FigureInterpretationPipeline`

Two passes:

**Crop pass** (sequential, disk-bound, ~10 ms/figure):
- For each figure in `layout.json`, open the corresponding page PNG
  (`page_NNNN.png`), compute the crop box from polygon coords + page DPI,
  save as `figures/figure_PPPP_III.png`. id = sha256 of `(doc_id, page, crop_box)`.

**Vision pass** (bounded parallel, network-bound, ~3 s/figure):
- For each cropped figure, render the `figure_interpretation` prompt and POST
  to the vision model (`gpt-4.1` by default). The model returns a
  `ChartInterpretation` (title, chart_type, interpretation prose, confidence).
- Concurrency: `pipeline_concurrency` (default 8) via `asyncio.Semaphore`.
- After the vision pass: `repo.save_figures(figures, interpretations)` then walks
  every chunk and writes `figure_refs_json = [fig_id for fig on chunk pages]`.
  This is the **single source of truth** for chunk→figure linkage.

A defensive idempotent backstop also exists at the use-case level:
`run_extraction.py:151` calls `relink_chunks_to_figures(doc.id)` right after
`save_chunks`. It rebuilds refs from page ranges so resume runs (which skip
the figures stage via checkpoint) still end up with intact linkage.

### 3.6 Stage 4 — governed LLM extraction

`application/pipelines/stage_2a_extraction_governed.py: GovernedExtractionPipeline`

For each chunk (concurrent, `Semaphore(pipeline_concurrency)`):

1. **Resume fast-path**: `_repo.needs_chunk_extraction(chunk.id)` checks the
   `chunk_extractions` table; if extraction is already recorded, hydrate from
   DB and skip the LLM call.
2. Render the `governed_extraction` prompt with:
   - `chunk.text`
   - `OntologySchema` (entity types, relation types, allowed source/target
     constraints).
   - Surrounding context: any tables/figures referenced by this chunk
     (`tables_by_id`, `figures_by_id`) — figure interpretations are inlined
     so the LLM "sees" the chart prose.
3. `llm.complete_json(..., max_tokens=4096, temperature=0.0)`.
4. Parse JSON → `ExtractionResult(entities, relationships, claims)`.
5. **Validation pipeline** (3 services, in order):
   - `OntologyValidator(schema)` — drops edges that violate
     `RelationTypeDef.allowed_source/target`.
   - `CanonicalizationService(governance)` — alias table → embedding cosine
     similarity → rapidfuzz fallback; merges duplicate entities and records
     merges in `entity_merges`.
   - `DriftDetector(governance, version)` — tags chunks producing many
     `UNKNOWN` types or off-schema attempts → drift events.
6. Off-schema entities become `Entity(type=UNKNOWN_TYPE)` plus a
   `RefinementSuggestion` queued to `ontology_proposals` (mode=governed).
7. **Retry policy** for transient Azure content-filter 400s and 429s:
   `_is_retryable_azure_400(exc)` → exponential backoff (1s/2s/4s/8s capped),
   max 5 attempts per chunk (`retryable_error_attempts=5`).
   If every attempt fails, the chunk is logged + skipped — pipeline does
   **NOT** abort. Final assertion: at least one chunk must succeed
   (`successful == 0` → `RuntimeError`).

The `stats: GovernedExtractionStats` object tracks `chunks_processed`,
`chunks_resumed`, `chunks_failed`, `entities_unknown`, `canonical_reused`,
`violations_prevented`, etc. These are emitted as a `governed` pipeline event.

### 3.7 Stage 5 — graph build (governed only)

`application/pipelines/stage_5_graph.py: GraphBuildPipeline`

Pure in-memory `networkx.MultiDiGraph` build from `ExtractionResult[]`:

- Each entity → node with `properties={confidence, aliases}`.
- Each claim → node typed `Claim` with text, confidence, supporting_table_id,
  supporting_figure_id. Auto-creates `Table` and `Figure` nodes if claims
  reference them, with `SUPPORTED_BY_TABLE` / `SUPPORTED_BY_FIGURE` edges.
- Each relationship → edge.
- **NO LLM CALLS HERE** — pure DB → graph projection. (Hence the explicit
  `tokens=0 expected` log line at `run_extraction.py:216`.)

Exports the same graph to three formats:
- GraphML (`networkx` native, for Gephi).
- JSON-LD (web-friendly RDF projection).
- Cypher (for Neo4j load).

All tagged with the active ontology version: `<doc_id>.<version>.{graphml,jsonld,cypher}`.

### 3.8 Stage 6 — Microsoft GraphRAG index (optional, post-pipeline)

`infrastructure/graphrag/graphrag_runner.py: GraphRagRunner`

Triggered by `ke ingest --build-knowledge-tree` (default ON) AFTER
`RunExtractionUseCase.execute` returns. Wraps the official `graphrag` 2.x CLI
because the Python API surface changes between minor releases, but the CLI
contract is stable.

`runner.write_inputs(version, chunks)`:
1. `graphrag init --root work/graphrag/<version> --force` — scaffolds prompts,
   `.env`, `settings.yaml`. (Interactively prompts for chat/embedding model
   names; we pipe `b"\n" * 10` on stdin with a 90s timeout.)
2. Overwrite `settings.yaml` with our Azure-aware template
   (`_azure_settings_yaml`). Key fields:
   - `completion_models:` and `embedding_models:` (graphrag 2.x — NOT the
     older `models:` dict).
   - `auth_method: azure_managed_identity` (when `AzureAuthMode.CREDENTIAL`)
     uses `DefaultAzureCredential` via LiteLLM's `azure_ad_token_provider`,
     and the `api_key` field MUST be absent (graphrag rejects setting both).
   - `azure_deployment_name:` per model.
   - `workflow refs: completion_model_id:` / `embedding_model_id:`.
3. Overlay any custom prompts from `config/graphrag_prompts/*.txt`
   (`graphrag init --force` always regenerates the defaults).
4. Write `input/<chunk_id>.txt` — one text file per chunk. graphrag does its
   **own** chunking/extraction/community detection over this corpus. Our
   pre-extracted ontology graph is a **separate** artifact; it's not fed to
   graphrag.

`runner.index(version)`:
- Wipes `output/`, `cache/`, `logs/`.
- Spawns `graphrag index --root <workdir>`.
- Retries up to 3× with backoff on the transient
  `"Key based authentication is disabled"` 401 from
  `validate_config` (Azure preflight is flaky during identity rollout).
- Final output: parquets in `work/graphrag/<version>/output/`:
  ```
  communities.parquet      community_reports.parquet
  entities.parquet         relationships.parquet
  text_units.parquet       (plus internal: documents, covariates, ...)
  ```

If `community_reports.parquet` is missing, `graphrag query` will throw
`ValueError: Could not find community_reports.parquet in storage!` —
re-run `ke graphrag index` to regenerate.

---

## 4. Persistence — what lives where

### 4.1 SQLite (`work/knowledge_extraction.db`)

The relational store (`infrastructure/persistence/sqlite/repositories.py`).
Schema is autocreated by `Base.metadata.create_all(engine)` plus lightweight
forward-compat ALTER patches in `_apply_schema_patches()` — no Alembic.

| Table                | Holds                                                        |
| -------------------- | ------------------------------------------------------------ |
| `documents`          | One row per ingested PDF (id = sha256 of bytes, page_count)  |
| `chunks`             | Chunk text + page range + section_id + figure_refs_json + table_refs_json |
| `tables`             | Table metadata; cells in `table_cells`                       |
| `figures`            | id, page, crop_box_json, image_path, interpretation_json (vision output) |
| `entities`           | Extracted entities (id, name, type, confidence, aliases_json)|
| `relationships`      | Source/target ids + type + properties_json                   |
| `claims`             | id, text, confidence, supporting_table_id, supporting_figure_id |
| `chunk_extractions`  | Per-chunk expected counts (the chunk-level checkpoint table) |
| `prompt_calls`       | Full audit log of LLM calls (system, user, response, tokens) |
| `ontology_versions`  | Approved versions (semver, YAML blob, status, approvers)     |
| `ontology_proposals` | Discovery candidates + governed refinements (status, source) |
| `ontology_rejections`| Audit of explicitly rejected proposals                       |
| `entity_aliases`     | canonical_id ↔ alias with provenance                         |
| `entity_merges`      | Merge history from canonicalization service                  |
| `drift_events`       | Drift signals tagged by ontology version                     |

Two repository methods to know about (defensive, idempotent):

- `RelationalRepository.save_chunks(chunks)`: on update preserves existing
  `figure_refs_json` / `table_refs_json` if the incoming chunk has none —
  prevents resume runs from silently wiping figure linkage.
- `RelationalRepository.relink_chunks_to_figures(document_id) -> int`:
  rebuilds chunk→figure refs from page ranges. Cheap, safe to call every run.

`RelationalRepository.__init__` takes a `sessionmaker`, NOT a path. Build it
with `make_engine(path)` then `make_session_factory(engine)` — see
`cli/main.py:183-185`.

### 4.2 Filesystem (`work/`)

See section 1 (top of doc) for the layout.

### 4.3 Property graph

NetworkX in-memory store (`infrastructure/persistence/graph/networkx_store.py`).
Not durable — every run rebuilds it from SQLite. The three serialized exports
are the durable form.

### 4.4 Qdrant vector store (infrastructure exists, not currently wired)

`infrastructure/persistence/qdrant/qdrant_vector_store.py` implements
`VectorStorePort` and `settings.py` exposes `vector_db_path` / `qdrant_url` /
`qdrant_api_key`. However, `VectorStorePort` is **not** imported or used
anywhere except its own definition — it is dead infrastructure. Canonicalization
uses direct embedding calls via `EmbeddingPort`, not a vector store.

### 4.5 MS GraphRAG index
`graphrag query` CLI. We do **NOT** read these parquets directly anywhere in
this codebase; the only consumer is the subprocess we shell out to.

---

## 5. Retrieval — four backends, one knowledge base

All four retrieval backends share the SQLite store + filesystem artifacts.
They differ in what they do at query time and what shape of evidence they
return. Selectable in `ke webui` and `ke graphrag ask`/`eval` via
`--backend mini|lazy|ms|agentic`.

### 5.1 `mini` — lexical baseline (BM25-style)

`application/services/graphrag_agent.py: MiniGraphRagAgent`

- Pure SQLite + filesystem. No LLM call. No network.
- Tokenize the question with `_TOKEN_RE = [a-z0-9]{3,}` and a stoplist.
- Score candidates from five sources in parallel, sort, keep top-K:
  - `claim_candidates` — claims table
  - `relationship_candidates` — relationships text + properties
  - `table_candidates` — table summaries
  - `figure_candidates` — figure captions + interpretations
  - `entity_candidates` — entity names + aliases
  - `chunk_candidates` — full chunk text (fallback when above produce nothing)
- Optional: include 1-hop graph neighbours from the NetworkX export.
- `ask_multi(queries)`: run multiple paraphrases, fuse via Reciprocal Rank
  Fusion (`application/services/query_rewriter.py: reciprocal_rank_fusion`).
- Temporal-aware: questions with date keywords get a bonus for hits containing
  parseable dates (`_apply_temporal_bonus`).

**Use this when**: you need deterministic, offline, no-LLM retrieval — CI,
air-gapped benchmarks, the eval baseline.

### 5.2 `lazy` — LazyGraphRAG (JIT subgraph at query time)

`application/services/lazy_graphrag_agent.py: LazyGraphRagAgent`

Implements the Microsoft Research "LazyGraphRAG" pattern (Nov 2024): no
index-time entity/relationship extraction; the subgraph is constructed at
ask time, scoped to the chunks relevant to that question.

Two LLM calls per ask:

1. **Subgraph extraction**: `application/services/chunk_retriever.py: ChunkRetriever`
   pulls top-K chunks (BM25, default K=20) + their immediate neighbours (1
   chunk before/after in the same document). Send chunks + question to the
   LLM with prompt `lazy_subgraph_extract` (v1). LLM returns a JIT subgraph:
   `(entities, relationships, claims)` — all chunk-cited.
2. **Synthesis**: render `lazy_synthesis` (v1) with the question + chunks +
   JIT subgraph as evidence. LLM returns answer prose with inline chunk
   citations.

The returned `LazyGraphRagAnswer` includes the chunks and subgraph so callers
can audit the evidence (the webui shows them in an "Evidence" panel below
the answer).

**Use this when**: you want graph-aware quality without paying the eager
indexing cost. ~5-10 s/ask, ~3-5k tokens/ask total.

### 5.3 `ms` — Microsoft GraphRAG (production)

`application/services/ms_graphrag_agent.py: MsGraphRagAgent`

Shells out to `graphrag query --root <workdir> --method <method> --query <q>`.
Four `--method` values:

| Method   | What it does                                                          | Best for                                  |
| -------- | --------------------------------------------------------------------- | ----------------------------------------- |
| `local`  | Entity-centric: pull text units around named entities                 | Single-fact / lookup questions (~30-40 s) |
| `global` | Map-reduce over community reports (LLM per community, then aggregate) | Thematic / synthesis ("overall trend...") (~200 s) |
| `drift`  | Entity-anchored expansion ("drift") through community graph           | Multi-hop traversal                       |
| `basic`  | Vector similarity over text_units only (no graph)                     | RAG baseline                              |

`MsGraphRagAgent.ask()` auto-routes to `local` vs `global` via `_route_method`:
synthesis cues (`compare`, `trends`, `overall`, `summary`, `themes`) → `global`;
factoid leaders (`when/where/who/which`, numeric literals) → `local`;
default → `local`.

The agent uses **synchronous** `subprocess.run` (not `asyncio.create_subprocess_exec`)
because Streamlit's ScriptRunner thread on Windows doesn't reliably have a
`ProactorEventLoop`, causing silent hangs. The async path
(`MsGraphRagAgent.ask_async`) is retained for non-Streamlit callers (e.g.
the eval harness).

Pre-flight check: `_latest_workdir()` requires all five required parquets
(`_REQUIRED_QUERY_PARQUETS`) — `communities`, `community_reports`, `entities`,
`relationships`, `text_units`. If any is missing it raises
`IndexIncompleteError` with a remediation message ("run `ke graphrag index`").

**Use this when**: you want the full Microsoft GraphRAG stack —
community-aware synthesis, multi-hop entity traversal, the SOTA quality but
with the upfront indexing cost (~60 minutes for ~800 chunks).

### 5.4 `agentic` — bounded multi-step reasoning loop

`application/services/agentic_search_agent.py: AgenticSearchAgent`

A self-contained bounded loop — no external agent framework required. Pure
Python + existing service layer.

```
question
  → Planner (LLM)  — 3–5 subquestions + retrieval hints (strict JSON)
  → Searcher       — MiniGraphRagAgent.ask_multi(queries, top_k, include_graph=False)
                     returns RetrievalResult; fuses via RRF internally
  → EvidenceInspector — compress + label hits into EvidenceItem list
  → Critic (LLM)   — assesses sufficiency; emits follow_up_queries if needed
  → [loop back to Searcher if not sufficient AND round < max_rounds]
  → Synthesizer (LLM) — grounded answer with inline [Cx] citations
```

Domain models (all pydantic):

| Model | Purpose |
|---|---|
| `AgenticSearchOptions` | Loop bounds (max_rounds=2, max_subquestions=5, top_k_per_query=8, max_total_evidence_items=30) |
| `SearchPlan` | Planner output: subquestions list |
| `EvidenceItem` | Normalized hit: kind, text, score, document_id, citation_label |
| `EvidenceCritique` | Critic output: sufficient, confidence, missing_information, follow_up_queries |
| `AgenticTokenUsage` | Per-role token counts (planner, critic, synthesis) + totals |
| `AgenticSearchAnswer` | Final: answer, plan, evidence, critique, rounds, token_usage, latency_ms |

Key implementation details:

- `ask()` wraps `ask_async()` via `asyncio.run()` — same pattern as `LazyGraphRagAgent`.
- `_retrieve_evidence()` calls `self._mini.ask_multi(queries, top_k=..., include_graph=False)`
  and uses **`.hits`** on the returned `RetrievalResult` — not the result directly.
- `_merge_evidence()` deduplicates by `f"{item.kind}:{item.id}"` composite key and
  caps at `max_total_evidence_items`.
- `_safe_json(text)` strips markdown fences and parses the first valid JSON object —
  same defensive pattern as `LazyGraphRagAgent._synthesize`.
- Synthesis extracts prose from JSON wrapper via key probe (`"answer"/"response"/"result"/"text"`),
  then falls back to raw text.
- `critique` initialised to `EvidenceCritique(sufficient=True, ...)` before the loop so
  `max_rounds=0` (synthesis-only mode) doesn't crash.
- `critic_in_total` / `critic_out_total` **accumulate** across rounds (not overwrite).

Model fallback chain:
- `planner_model` / `critic_model` → `azure_openai_reasoning_model` ("o4-mini") → `azure_openai_extraction_model`
- `synthesis_model` → `azure_openai_extraction_model`

Observability events emitted: `agentic.plan`, `agentic.search`, `agentic.inspect`,
`agentic.critic`, `agentic.synthesis`, `agentic.round`, `agentic.ask` — each with
round, subquery_count, evidence_count, token totals, latency, model name.

Prompt templates (`.j2` format, `SYSTEM:` / `USER:` sections):
- `config/prompts/agentic_plan.v1.j2` — planner; produces `{"subquestions": [...]}`
- `config/prompts/agentic_critic.v1.j2` — critic; produces `{"sufficient", "confidence", "missing_information", "follow_up_queries"}`
- `config/prompts/agentic_synthesis.v1.j2` — synthesis; produces `{"answer": "..."}`

Settings added in `config/settings.py`:
```
agentic_planner_model     — default "" (falls back to reasoning_model)
agentic_critic_model      — default "" (falls back to reasoning_model)
agentic_synthesis_model   — default "" (falls back to extraction_model)
agentic_max_rounds        — default 2
agentic_max_subquestions  — default 5
agentic_top_k_per_query   — default 8
```

**Use this when**: the question is broad ("What are the major trends?"), ambiguous,
or likely requires evidence from multiple document sections that a single retrieval
pass would miss. Costs 4–10 LLM calls per ask (~20–60 s, ~8–20k tokens). Not
suitable for simple factoid lookups — use `mini` or `lazy` instead.

### 5.5 Evidence assembly (`mini` and `lazy` only)

The webui builds an "evidence" panel from the retrieval output:

- `_collect_lazy_refs(answer)` walks `answer.chunks[*].figure_refs_json` /
  `table_refs_json` (which is why the figure-linkage bug from session checkpoint
  006 was so important: without correct linkage, the panel showed nothing).
- `_first_chunk_figure_ref(chunk, figure_index)` prefers figures that have a
  real `image_path` over synthetic page placeholders (Doc Intel sometimes emits
  page-anchored figure stubs with no crop) — `cli/webui_app.py:560-580` (approx).

`ms` mode does not surface structured figure/table refs (graphrag's output is
prose only); the UI shows a hint to use mini/lazy for explicit diagram links.

---

## 6. Ontology governance

The system supports two extraction modes — pick via `ke ingest --mode <m>`:

### Discovery (`ExtractionMode.DISCOVERY`)
- Stage runner = `DiscoveryExtractionPipeline` + `SemanticClusterer` +
  `OntologyProposalPipeline`.
- Reasoning model extracts entities/relationships WITHOUT schema constraints.
- Embed entity names → cluster (HDBSCAN or k-means depending on size) → LLM
  summarizes each cluster into a candidate type.
- Output: a versioned `OntologyProposal` (NOT written to `ontology_versions`).
- The graph stage is **NOT** registered in discovery mode.

### Governed (`ExtractionMode.GOVERNED` — default)
- Loads the active approved `OntologyVersion` (`onto_service.active(...)`).
- Prompt forces use of allowed entity/relation types.
- `OntologyValidator` rejects edges violating `allowed_source` / `allowed_target`.
- Off-schema → `Entity(type=UNKNOWN)` + a `RefinementSuggestion` queued for
  human review.
- Graph stage runs (Stage 5).

Tables: `ontology_versions`, `ontology_proposals`, `ontology_rejections`,
`entity_aliases`, `entity_merges`, `drift_events`.

CLI helpers: `ke ontology list|show|diff|approve|reject|propose|migrate`.

---

## 7. Observability

`infrastructure/telemetry/observability.py` is the **only** observability
substrate. Everything else just calls into it.

- `wide_event(name, **fields)` context manager: emits one structured JSON
  line per logical operation. Bound run/document/stage context is auto-merged
  in via `bind(...)` / `bound(...)`. Records:
  - `event` name, `duration_ms`, `status` (ok/error)
  - `input_tokens_self` / `output_tokens_self` (just this op)
  - `input_tokens_total` / `output_tokens_total` (self + nested children)
  - any custom fields passed as kwargs
- A daemon heartbeat thread emits `{event}.heartbeat` every
  `OBSERVABILITY_HEARTBEAT_INTERVAL_SECONDS` (default 30 s) and a one-time
  `{event}.stalled` warning at `OBSERVABILITY_STALL_THRESHOLD_SECONDS` (120 s).
- OTEL spans mirror stage boundaries (`stage.<name>`). Enable with
  `OTEL_ENABLED=1` + `OTEL_EXPORTER_OTLP_ENDPOINT=...`. Otherwise spans land
  in `work/telemetry/spans.jsonl`.
- Per-run JSONL log at `work/logs/run-<id>.jsonl` containing every wide
  event — the canonical forensics artifact when something goes wrong.
- A `run.finish` event at the end aggregates totals: `input_tokens`,
  `output_tokens`, `total_tokens`, models touched.

Token usage rolls up hierarchically — outer spans see their own LLM cost
plus everything inside them.

---

## 8. Resilience & resume semantics

- **Stage-level checkpoints**: `.done` markers under
  `work/checkpoints/<doc_id>/<stage>/`. Implemented by
  `infrastructure/checkpointing/filesystem_checkpoint_store.py`. Skipped stages do NOT execute
  their stage closure.
- **Chunk-level checkpoint**: the `chunk_extractions` table records expected
  entity/relationship/claim counts per chunk. On resume, the extract stage
  short-circuits per-chunk if counts match.
- **Per-chunk fault isolation**: a single chunk's failure does not abort
  the pipeline; it's logged and counted in `stats.chunks_failed`. The graph
  stage only sees successful results. Hard abort only if **all** chunks fail.
- **Azure 400 content-filter retry**: 5 attempts with exponential backoff
  (1/2/4/8 s capped at 8). Many Azure RAI filter false-positives clear on
  retry.
- **`--redo-stage <stage>`**: clears checkpoints from that stage downward and
  re-runs (`run_extraction.py: _cascade_redo`).
- **`--fresh`**: nukes `work/` for this PDF before starting (full rebuild).
- **Schema migrations**: lightweight `ALTER TABLE` patches in
  `_apply_schema_patches()` run at engine creation. Forward-compatible only.

---

## 9. Configuration

`config/settings.py: Settings` (pydantic-settings). Reads `.env` then env vars.
Important fields:

| Setting                                | Purpose                                              |
| -------------------------------------- | ---------------------------------------------------- |
| `azure_openai_endpoint`                | https://<resource>.openai.azure.com                  |
| `azure_openai_api_key`                 | Only if `azure_auth_mode=key`                        |
| `azure_auth_mode`                      | `key` or `credential` (DefaultAzureCredential)       |
| `azure_openai_reasoning_model`         | Discovery mode + ontology proposal (default `o4-mini`) |
| `azure_openai_extraction_model`        | Governed extraction (default `gpt-4.1-mini`)         |
| `azure_openai_vision_model`            | Figure interpretation (default `gpt-4.1`)            |
| `azure_openai_embedding_model`         | Canonicalization + clustering (default `text-embedding-3-large`) |
| `azure_document_intelligence_endpoint` | Stage 0 ingest                                       |
| `pipeline_concurrency`                 | asyncio.Semaphore for figures + extract (default 8)  |
| `graphrag_executable`                  | Override path to `graphrag.exe` (Windows long-path)  |
| `default_mode`                         | `governed` or `discovery`                            |
| `active_ontology_version`              | Pin to a specific version, else newest approved      |
| `observability_*`                      | Heartbeat + stall thresholds                         |
| `agentic_planner_model`                | Override model for agentic planner (default: reasoning_model) |
| `agentic_critic_model`                 | Override model for agentic critic (default: reasoning_model) |
| `agentic_synthesis_model`              | Override model for agentic synthesis (default: extraction_model) |
| `agentic_max_rounds`                   | Hard cap on agentic retrieval rounds (default 2)     |
| `agentic_max_subquestions`             | Max subquestions per plan (default 5)                |
| `agentic_top_k_per_query`              | top-k chunks per subquery (default 8)                |

**Foundry key auth quirk**: if the Azure OpenAI resource has key auth disabled
by policy, you must (1) set tag `SecurityControl=Ignore` on the resource and
(2) enable key-based auth. Otherwise use `azure_auth_mode=credential`.

**Windows + graphrag**: install graphrag in a short-path venv (e.g. `C:\g`) and
set `GRAPHRAG_EXECUTABLE=C:\g\Scripts\graphrag.exe` — LiteLLM has a long-path
import bug that breaks the standard venv path.

---

## 10. CLI surface

`pyproject.toml: [project.scripts] ke = "knowledge_extraction.cli.main:app"`.
Invoke with `uv run ke <command>`.

| Command                            | Purpose                                                  |
| ---------------------------------- | -------------------------------------------------------- |
| `ke ingest [pdf]`                  | Run the full pipeline. Defaults to all PDFs in `assets/`.|
| `ke ingest --mode discovery`       | Use discovery extraction instead of governed             |
| `ke ingest --pages N`              | First-N-page slice                                       |
| `ke ingest --redo-stage <stage>`   | Clear from that stage down and re-run                    |
| `ke ingest --fresh`                | Wipe `work/` first                                       |
| `ke ingest --build-knowledge-tree` | Also build MS GraphRAG index after extraction (default ON) |
| `ke graphrag index`                | Just (re)build the MS GraphRAG index from existing chunks|
| `ke graphrag ask <q> --backend ms\|lazy\|mini\|agentic\|auto` | One-shot question (--method local\|global\|drift\|basic\|auto) |
| `ke graphrag eval --backend ms,lazy,mini,agentic` | Run an eval suite, optionally side-by-side       |
| `ke webui [--backend ms\|lazy\|mini\|agentic] [--port 8502]` | Launch the Streamlit chat UI (Telemetry + Chat pages) |
| `ke resume <pdf>`                  | Re-run; checkpointed stages are skipped                  |
| `ke clean [--yes]`                 | Wipe all derived state (keeps assets/ and config)        |
| `ke ontology list\|show\|diff\|approve\|reject\|propose\|migrate` | Manage ontology versions |

The eval framework is at `application/services/graphrag_eval.py`:
per-case metrics (MRR, precision@k, recall@k, citation_recall, top_score) plus
per-category aggregates. Adversarial cases pass when `top_score <
min_score_for_grounded`. Suite definition at
`config/evals/graphrag_eval.json`.

---

## 11. Dev workflow

```powershell
cd C:\_CODE\AdvancedRAG\knowledge_extraction
uv run ruff check .         # lint
uv run pytest -q            # all tests (~171 tests, ~30 s)
uv run pytest -q tests/unit # unit only (fast)
uv run ke webui             # local Streamlit UI
```

Tests of note:
- `tests/integration/test_smoke.py` — end-to-end pipeline smoke test
- `tests/unit/test_figure_pipeline.py` — figures stage + save_chunks
  preservation + relink_chunks_to_figures regression tests
- `tests/unit/test_ms_graphrag_agent.py` — covers both sync and async paths
- `tests/unit/test_graphrag_runner.py` — settings.yaml templating
- `tests/unit/test_preflight.py` — index-completeness checks

---

## 12. Common gotchas (read this before touching anything)

1. **`Chunk.figure_refs` is always `[]` from the chunker.** It's filled in by
   the figures stage. If you re-save a chunk anywhere, ensure you don't blow
   it away — `RelationalRepository.save_chunks` already guards against this,
   and `relink_chunks_to_figures` is called as a backstop after every
   `save_chunks` in the use case.

2. **`graphrag init --force` overwrites `prompts/`, `settings.yaml`, AND
   `.env`** every time. Custom prompts must be re-applied after init;
   `runner._overlay_custom_prompts` handles this from
   `config/graphrag_prompts/`.

3. **graphrag 2.x YAML schema is NOT the older 1.x schema.** Use
   `completion_models:` / `embedding_models:` dicts (not `models:`),
   `auth_method:` (not `auth_type:`), `azure_deployment_name:`, `chunking:`
   (not `chunks:`), top-level `input_storage:` / `output_storage:`.

4. **`auth_method: azure_managed_identity` works with graphrag 2.7.2** —
   uses `DefaultAzureCredential` via LiteLLM's `azure_ad_token_provider`. Do
   NOT also set `api_key` in CREDENTIAL mode (graphrag rejects setting both).

5. **`asyncio.run` inside Streamlit on Windows can hang silently.** The MS
   GraphRAG agent uses `subprocess.run` (sync) for `ask()`; `ask_async` is
   only for non-Streamlit callers.

6. **HAI report figure captions are empty strings** (`""`, not `None`). The
   UI uses `caption or figure_id` as label fallback.

7. **Two figure id flavours per page**: synthetic placeholders like `94.1`
   (no `image_path`, from Doc Intel layout pass) AND real hashed ids like
   `e4881d6ff3ef5ff6` (with `image_path` to PNG). Always check `image_path`
   before rendering a thumbnail.

8. **`.first1.pdf` artifacts are debug slices** from `--pages 1` runs. They
   pollute the SQLite store with stale truncated copies of documents. Not
   blocking, but `documents` table will show duplicates if you've run slice
   tests.

9. **MS GraphRAG query is slow (~30-40 s for `local`, ~200 s for `global`)**.
   Always wrap UI calls in a spinner. Default timeout is 300 s in the agent.

10. **Eval lexical scoring favours BM25 over LLM synthesis.** When comparing
    MS vs mini, build `positive_terms` keyed to the **answer prose** (not just
    keywords) — see config/evals/graphrag_eval.json synthesis-mode cases.

---

## 13. Where to start when modifying

| Goal                                    | Start at                                                              |
| --------------------------------------- | --------------------------------------------------------------------- |
| Add / reorder a pipeline stage          | `application/pipelines/stages.py` (enum) + `run_extraction.py`        |
| Add a new retrieval backend             | New service in `application/services/` + wire in `cli/webui_app.py` + `cli/main.py: graphrag_ask/eval` |
| Change ontology schema                  | `config/ontologies/<name>.yaml` + `ke ontology approve`               |
| Change MS GraphRAG config               | `graphrag_runner.py: _azure_settings_yaml()` + reindex                |
| Add a new ingestion adapter             | Implement `IngestionPort`; prepend to `ingestion_chain` in composition root |
| Tune chunk sizes                        | `SemanticChunker(target_chars, max_chars)` in `cli/main.py`           |
| Tune extraction concurrency             | `Settings.pipeline_concurrency`                                       |
| Add a new figure cropping strategy      | `FigureInterpretationPipeline._figure_specs_from_document`            |
| Add a new prompt                        | `config/prompts/<name>.v<n>.j2` + use `PromptRegistry.render(name, version, **ctx)` |
| Modify the eval scoring                 | `application/services/graphrag_eval.py`                               |
| Debug a stuck run                       | Read the latest `work/logs/run-*.jsonl` (one event per line)          |

---

## 14. File layout (essential paths only)

```
knowledge_extraction/
├── knowledge_extraction/
│   ├── domain/                       # pure dataclasses (Document, Chunk, Figure, Entity, ...)
│   ├── application/
│   │   ├── ports/__init__.py         # all Protocol definitions
│   │   ├── pipelines/
│   │   │   ├── stages.py             # Stage enum (single source of truth for stage names)
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
│   │   │   ├── chunk_retriever.py          # BM25 over chunks
│   │   │   ├── canonicalization_service.py
│   │   │   ├── ontology_governance.py
│   │   │   ├── ontology_validator.py
│   │   │   ├── drift_detector.py
│   │   │   ├── prompt_registry.py
│   │   │   ├── query_rewriter.py           # RRF + LLM/lexical rewrites
│   │   │   └── graphrag_eval.py            # eval harness
│   │   └── use_cases/run_extraction.py  # THE pipeline entry point
│   ├── infrastructure/
│   │   ├── ingestion/document_intelligence_adapter.py
│   │   ├── ingestion/docling_adapter.py         # local fallback (no Azure DI needed)
│   │   ├── ingestion/pdf_renderer.py            # PdfPageRenderer (pypdfium2)
│   │   ├── llm/azure_foundry_client.py
│   │   ├── persistence/
│   │   │   ├── sqlite/{models.py,repositories.py}
│   │   │   ├── graph/networkx_store.py
│   │   │   └── checkpoints.py
│   │   ├── graphrag/graphrag_runner.py
│   │   ├── neo4j/parquet_loader.py   # MS GraphRAG parquets → Neo4j (demo-only)
│   │   └── telemetry/observability.py
│   ├── cli/
│   │   ├── main.py                   # composition root + Typer commands
│   │   └── webui_app.py              # Streamlit UI
│   └── config/settings.py
├── assets/*.pdf                      # documents to ingest
├── config/
│   ├── ontologies/*.yaml
│   ├── prompts/<name>.v1.j2              # versioned jinja2 templates (SYSTEM: / USER: sections)
│   │   ├── agentic_plan.v1.j2            # planner prompt
│   │   ├── agentic_critic.v1.j2          # critic prompt
│   │   └── agentic_synthesis.v1.j2       # synthesis prompt
│   ├── graphrag_prompts/*.txt        # overlay templates for `graphrag init`
│   └── evals/graphrag_eval.json
├── tests/{unit,integration}/
├── work/                             # all runtime artifacts (gitignored)
├── architecture.md                   # high-level human-oriented overview
└── for_llm.md                        # this document

# one level up, at the repo root (C:/_CODE/AdvancedRAG/):
infrastructure/
└── neo4j/
    ├── docker-compose.yml            # Neo4j 5 + APOC + GDS; `ke neo4j up` uses this
    ├── .env.example
    └── demo_queries.cypher           # 9 ready-to-paste Cypher queries
```

## 15. Neo4j visualization layer (demo-only, opt-in)

**Purpose.** MS GraphRAG produces parquet files + LLM answers — the underlying
knowledge graph is invisible. For demos and ad-hoc analysis we ship an
optional Neo4j stack so you can SEE the graph: entities, relationships,
communities, and the actual paths GraphRAG traverses to answer multi-hop
questions (the killer demo is the Character.AI → Setzer ↔ Crecente subgraph,
which lexical retrieval cannot reach in any number of hops).

**Surface area** (zero impact on the base pipeline):

| Component | Path | Notes |
|---|---|---|
| Docker stack | `infrastructure/neo4j/docker-compose.yml` | Neo4j `5.24-community` + APOC + GDS plugins, persistent volumes, healthcheck, ports `7474` (Browser) / `7687` (Bolt). Default password `graphrag-demo` (override via `.env`). |
| Demo queries | `infrastructure/neo4j/demo_queries.cypher` | 9 ready-to-paste Cypher queries (Character.AI neighborhood, shortest path Setzer↔Crecente, community 193, cross-community bridges, GDS PageRank, text-unit lookup). |
| Loader | `knowledge_extraction/infrastructure/neo4j/parquet_loader.py` | Pure-Python payload builders (`build_*_payload`) + `GraphRagNeo4jLoader` driver façade. Idempotent `MERGE`-based Cypher batched via `UNWIND` (default 1000 rows / batch). Numpy/pandas scalars coerced via `_safe()`. |
| CLI | `ke neo4j {up,down,open,load,wipe}` in `cli/main.py` | `up`/`down` shell out to `docker compose`; `load` walks the latest `work/graphrag/<ver>/output/` and pushes parquets; `open` launches the browser; `wipe` truncates all nodes/relationships without dropping constraints. |
| Tests | `tests/unit/test_neo4j_loader.py` | 10 tests, all pure-Python (no live DB) — payload shapes, numpy coercion, truncation, hierarchy edges. |
| Extras | `pyproject.toml` → `[project.optional-dependencies] neo4j = ["neo4j>=5,<6"]` | `uv sync --extra neo4j` to install the driver — kept out of the base install. |

**Graph schema written to Neo4j.**

```cypher
// Constraints (created on first load)
CREATE CONSTRAINT entity_title    IF NOT EXISTS FOR (e:Entity)    REQUIRE e.title IS UNIQUE;
CREATE CONSTRAINT entity_id       IF NOT EXISTS FOR (e:Entity)    REQUIRE e.id IS UNIQUE;
CREATE CONSTRAINT community_id    IF NOT EXISTS FOR (c:Community) REQUIRE c.community IS UNIQUE;
CREATE CONSTRAINT text_unit_id    IF NOT EXISTS FOR (t:TextUnit)  REQUIRE t.id IS UNIQUE;

// Nodes
(:Entity    {id, human_id, title, type, description, frequency, degree})
(:Community {community, level, parent_id, title, size, summary, rating, rank, full_content})
(:TextUnit  {id, human_id, n_tokens})

// Edges
(:Entity)-[:RELATED_TO {weight, description}]->(:Entity)
(:Entity)-[:IN_COMMUNITY {level}]->(:Community)
(:Community)-[:PARENT_OF]->(:Community)
(:Entity)-[:MENTIONED_IN]->(:TextUnit)
```

**Loader keying — the one non-obvious detail.** MS GraphRAG's
`relationships.parquet` stores `source` and `target` as **entity TITLES**
(upper-cased, e.g. `"CHARACTER.AI"`), not UUIDs — so the loader `MATCH`es
endpoints by `title`. By contrast, `text_units.parquet`'s `entity_ids` column
contains the UUIDs, so `MENTIONED_IN` matches by `id`. Any relationship whose
endpoint title isn't present in `entities.parquet` is counted in
`skipped_relationships_missing_endpoint` (should be `0` for clean runs).

**Lifecycle.**

```bash
uv sync --extra neo4j                  # one-time: install the driver
uv run ke neo4j up                     # one-time: start Docker container
uv run ke neo4j load                   # auto-detects latest work/graphrag/<ver>/output/
uv run ke neo4j open                   # opens http://localhost:7474

# When done:
uv run ke neo4j down                   # stop container (data persisted on volume)
uv run ke neo4j wipe                   # truncate all graph data (keeps constraints)
```

Defaults: `bolt://localhost:7687`, user `neo4j`, password `graphrag-demo`,
database `neo4j`. Overrideable via env vars `NEO4J_URI`, `NEO4J_USER`,
`NEO4J_PASSWORD`, `NEO4J_DATABASE` or per-command flags.

**Why a separate sub-app rather than a stage.** The Neo4j layer is a
*post-hoc visualization*, not part of the canonical pipeline. Keeping it
behind an optional extra + dedicated CLI means:
- base install stays lean (no Java/driver footprint),
- the production retrieval path (MS GraphRAG → DuckDB → LLM) is unchanged,
- the loader is idempotent and re-runnable without touching `work/`.

**Performance.** First load on the bundled corpus (8.6k entities, 21.7k
relationships, 1.8k communities, 1k text units) completes in ~25s end-to-end
on a laptop. Subsequent reloads are similar (`MERGE` is the bottleneck, not
network).

