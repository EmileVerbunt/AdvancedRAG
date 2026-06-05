# Architecture

## Layering

```
domain        ──►  pure data (pydantic v2). No I/O. No third-party clients.
application   ──►  use_cases + ports + pipelines + services. Pure orchestration.
infrastructure──►  adapters implementing ports: Azure clients, SQLite, Qdrant, GraphRAG, FS.
tui / cli     ──►  presentation; depend on application only.
```

Strict rule: no module in `domain` or `application` may import from `infrastructure`. Wiring happens in `cli/main.py` (composition root) which builds an `ExtractionServices` bag and hands it to a use case.

## The pipeline at a glance

The end-to-end flow is *one file*: [`application/use_cases/run_extraction.py`](knowledge_extraction/application/use_cases/run_extraction.py).
Open it and you can read the entire pipeline top-to-bottom.

```
slice (optional) ─► ingest ─► chunk ─► render ─► figures ─► extract ─► graph
                                                  (vision)   (LLM)     (governed only)
```

Stage names live in [`application/pipelines/stages.py`](knowledge_extraction/application/pipelines/stages.py) as the `Stage` enum — the single source of truth for ordering, on-disk checkpoint paths, and `--redo-stage` validation.

Every stage:

1. Reads previous stage output from `work/checkpoints/<doc_hash>/<stage>/`.
2. Produces an output and writes a `.done` marker plus serialized artifacts.
3. Records OTEL span + structured wide-event line + token/latency metrics.

The `Orchestrator` resolves stage DAG and skips completed stages on resume. Use `--redo-stage <stage>` to clear that stage and everything downstream.

## Two extraction modes

### Discovery
- Reasoning model (configurable).
- Unconstrained extraction → candidate types, hierarchies, clusters.
- Embedding-based clustering + LLM cluster summarization.
- Output: versioned `ontology_candidate_vN.yaml` + alias mappings + confidence scores.
- **Never** writes to `ontology_versions` directly; always lands in `ontology_proposals`.

### Governed
- Loads active approved `OntologyVersion`.
- Prompt forces use of allowed types; off-schema → `UNKNOWN` + refinement proposal.
- `OntologyValidator` rejects edges whose source/target violate `RelationTypeDef.allowed_source/target`.
- Canonicalization: alias map → embedding similarity → rapidfuzz fallback.
- `DriftDetector` records UNKNOWN rate, off-schema attempts, clustered unknowns.

## Observability

- All long-running blocking ops are wrapped in `wide_event(name, **fields)`. One JSON record per logical operation, with bound run/document/stage context.
- A daemon heartbeat thread emits `{event}.heartbeat` records every `OBSERVABILITY_HEARTBEAT_INTERVAL_SECONDS`, and a one-time `{event}.stalled` warning after `OBSERVABILITY_STALL_THRESHOLD_SECONDS`.
- Token usage rolls up hierarchically: each span tracks `self` vs `total` (self + children), and a run-level `run.finish` event aggregates `input_tokens`, `output_tokens`, `total_tokens`, and the set of models touched.
- OTEL spans mirror stage boundaries (`stage.<name>`).

## Checkpointing & resilience

- **Stage-level**: file-based `.done` markers under `work/checkpoints/<doc_id>/<stage>/`.
- **Chunk-level**: SQLite table `chunk_extractions` records expected relationship/claim counts per chunk so a partial extract resume only re-processes incomplete chunks (graph build still gets a full hydrated result set).
- **Schema migration**: `make_engine()` runs lightweight `ALTER TABLE` patches for legacy DBs (no Alembic).
- **Redo a stage**: `ke ingest <pdf> --redo-stage extract` clears extract + graph and re-runs.
- **Forensics**: every run writes a JSONL file under `work/logs/run-*.jsonl` containing every wide event and heartbeat — stalls leave a clear breadcrumb trail.

## Ontology Governance

Subsystem under `application/services/ontology_governance.py` + SQLite tables:

| Table               | Purpose                                                  |
|---------------------|----------------------------------------------------------|
| `ontology_versions` | Approved versions (semver, YAML blob, status, approvals) |
| `ontology_proposals`| Discovery candidates + governed refinements              |
| `ontology_rejections`| Audit of rejected proposals                              |
| `entity_aliases`    | canonical_id ↔ alias with provenance                     |
| `entity_merges`     | Merge history for canonicalization                       |
| `drift_events`      | Drift signals tagged by version                          |

CLI:

```
ke ingest [pdf] --mode discovery|governed [--redo-stage STAGE] [--fresh]
ke webui [--backend lazy|mini|ms|agentic|nav]
```

## Persistence

| Concern            | Store                                       |
|--------------------|---------------------------------------------|
| Chunks/entities/relationships/claims/prompts | SQLite via SQLAlchemy   |
| Vectors             | Qdrant (embedded local or remote URL)      |
| Property graph      | NetworkX in-memory + GraphML/JSON-LD/Cypher exports |
| Page images, layout JSON, markdown, table/figure inventory, ontology candidates | Filesystem under `work/artifacts/` |
| GraphRAG artifacts  | parquet under `work/graphrag/<version>/`   |

## Configuration

`pydantic-settings` reads `.env`. `AZURE_AUTH_MODE` toggles between API key and `DefaultAzureCredential` for all Azure clients. Observability heartbeat thresholds are tunable (`OBSERVABILITY_HEARTBEAT_INTERVAL_SECONDS`, `OBSERVABILITY_STALL_THRESHOLD_SECONDS`).

## Interface readiness

The pipeline is exposed as a single `RunExtractionUseCase.execute(ExtractionRequest)` call. Wrapping it as an MCP tool, a Foundry skill, or an HTTP handler is a thin shim — you build an `ExtractionServices` once at startup and call `execute()` per request.

## TUI

`tui/app.py` consumes events from a pipeline event bus and renders a Rich `Live` dashboard. Mode-aware panels:

- **Discovery**: proposed types, clusters detected, ontology growth.
- **Governed**: canonical reuse rate, UNKNOWN count, validations prevented, drift score, refinement queue.

Common panels: stage + per-stage progress, token/cost metrics, failure queue.

## Retrieval

- GraphRAG indexes are tagged with the `OntologyVersion` active at index time.
- Retrieval supports type/relationship filters and claim→evidence traversal.
- Ontology migrations require rebuilding downstream retrieval indexes.

## Five retrieval backends

| Backend   | Class | Compute placement | Description |
|-----------|-------|-------------------|-------------|
| `mini`    | `MiniGraphRagAgent` | Zero (instant) | BM25/lexical baseline over chunks, claims, entities, tables, figures with Reciprocal Rank Fusion |
| `ms`      | `MsGraphRagAgent` | Upfront (index build) | Microsoft GraphRAG CLI — pre-built community graph, local/global/drift search |
| `lazy`    | `LazyGraphRagAgent` | At query time (graph construction) | LazyGraphRAG — JIT ego-graph from BM25 hits, single synthesized answer, zero ingestion cost |
| `agentic` | `AgenticSearchAgent` | At query time (planning + reasoning + critique) | Bounded multi-step loop: plan → retrieve → inspect → critique → optional follow-up → synthesize |
| `nav`     | `AgenticNavAgent` | At query time (routing + document navigation) | Agentic Navigator — metadata-first document routing, then a bounded ReAct tool loop that opens the actual document (`doc.md`) and drills into sections/tables/figures on demand |

### Agentic loop

```
question
  → Planner (LLM)  — produces 3–5 subquestions
  → Searcher       — BM25 + RRF via MiniGraphRagAgent.ask_multi()
  → Critic  (LLM)  — assesses sufficiency; emits follow-up queries if needed
  → [loop back to Searcher if insufficient and round < max_rounds]
  → Synthesizer (LLM) — grounded answer with inline citations
```

All three LLM roles (planner, critic, synthesizer) use independent model settings that fall back to `reasoning_model` / `extraction_model`. Loop bounds (`max_rounds`, `max_subquestions`, `max_total_evidence_items`) are hard-capped to prevent runaway cost.

The agentic backend emits `agentic.*` wide events for every stage (plan, search, inspect, critic, synthesis, round, answer) including round number, subquery count, evidence count, token usage, and latency.

### CLI integration

```bash
uv run ke graphrag ask "What are the major AI trends?" --backend agentic
uv run ke graphrag eval config/evals/graphrag_eval.json --backend mini,lazy,ms,agentic
```

Eval result includes agentic-specific metadata: `rounds`, `subquestions_count`, `follow_up_queries_count`, `critic_confidence`, `evidence_sufficient`.

### Agentic Navigator (`nav`) loop

Unlike the other backends, `nav` does **not** fan out blind chunk retrieval ahead
of time. It routes on metadata, then navigates the real document:

```
question
  → Router (LLM)     — sees a metadata catalog only (titles, counts, captions,
                       previews) and picks ≤ max_docs candidate documents
  → Navigator (LLM)  — bounded ReAct tool loop over DocumentNavigator tools:
                       open_document, read_section, search_document,
                       get_table, get_figure, finish
  → Synthesizer (LLM)— grounded answer citing the documents/sections used
```

`DocumentNavigator` (`application/services/document_navigator.py`) reads directly
from the SQLite store (raw read-only `sqlite3`) plus the `doc.md` artifact on
disk, and is schema/filesystem tolerant (missing tables/columns/`doc.md` degrade
to chunk-text fallbacks). The loop is fully bounded: an allowlisted tool schema,
clamped args, a `document_id` allowlist, and invalid/no-progress streak caps stop
runaway loops and keep the transcript inside the context window. Emits `nav.*`
wide events (`route`, `step`, `synthesis`, `ask`). Eval metadata: `steps`,
`selected_documents`.

### 4-way benchmark

`ke graphrag bench` reuses the eval runners to compare all four backends on a
curated suite (`config/evals/bench_4way.json`), reporting quality (pass rate,
MRR) **and** cost: per-query latency (p50/p95/total) and token usage, plus the
one-off ingestion cost (read from `work/logs/run-*.jsonl`) and the MS GraphRAG
index runtime (`stats.json`). Pure rollup logic lives in
`application/services/benchmark.py`; results are written to `work/benchmarks/`
as JSON + markdown. Token cost is `None` (`n/a`) for backends that do not expose
counts (`ms`), distinct from `0` for the LLM-free `mini` baseline.

```bash
uv run ke graphrag bench --backend mini,lazy,ms,agentic
```

### UI integration

`ke webui --backend agentic` shows an agentic-specific rendering in the Chat page:

- **Plan** expander — subquestions generated by the planner
- **Evidence critique** expander — sufficiency verdict, missing information, follow-up queries, confidence
- **Evidence items** expander — all retrieved chunks/tables/figures with citations
- Caption — rounds, evidence count, token usage, latency
