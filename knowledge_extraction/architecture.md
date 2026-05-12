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
- **Redo a stage**: `ke extract <pdf> --redo-stage extract` clears extract + graph and re-runs.
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
ke ontology list | show | diff | propose | approve | reject | migrate
ke extract --mode discovery|governed [--redo-stage STAGE] [--fresh]
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
- Migrations require reindex (`ke graphrag reindex --from-version vX`).
