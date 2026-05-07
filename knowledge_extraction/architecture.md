# Architecture

## Layering

```
domain  ──►  pure data (pydantic v2). No I/O. No third-party clients.
application  ──►  ports (Protocols) + pipelines + services. Pure orchestration.
infrastructure  ──►  adapters implementing ports: Azure clients, SQLite, Qdrant, GraphRAG, FS.
tui / cli  ──►  presentation; depend on application only.
```

Strict rule: no module in `domain` or `application` may import from `infrastructure`. Wiring happens in `cli/main.py` (composition root).

## Pipelines

```
ingest ─► layout ─► chunk ─► figures ─► extraction ─► graph ─► graphrag
                                          │
                                          ├─ Discovery: free-form  → ontology proposals
                                          └─ Governed:  schema-guided + validation + canonicalization
```

Every stage:

1. Reads previous stage output from `work/checkpoints/<doc_hash>/<stage>/`.
2. Produces an output and writes a `.done` marker plus serialized artifacts.
3. Records OTEL span + structured log line + token/latency metrics.

The `Orchestrator` resolves stage DAG and skips completed stages on resume.

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
ke extract --mode discovery|governed
```

## Persistence

| Concern            | Store                                       |
|--------------------|---------------------------------------------|
| Chunks/entities/relationships/claims/prompts | SQLite via SQLAlchemy   |
| Vectors             | Qdrant (embedded local or remote URL)      |
| Property graph      | NetworkX in-memory + GraphML/JSON-LD/Cypher exports |
| Page images, layout JSON, markdown, ontology candidates | Filesystem under `work/artifacts/` |
| GraphRAG artifacts  | parquet under `work/graphrag/<version>/`   |

## Configuration

`pydantic-settings` reads `.env`. `AZURE_AUTH_MODE` toggles between API key and `DefaultAzureCredential` for all Azure clients.

## TUI

`tui/app.py` consumes events from a pipeline event bus and renders a Rich `Live` dashboard. Mode-aware panels:

- **Discovery**: proposed types, clusters detected, ontology growth.
- **Governed**: canonical reuse rate, UNKNOWN count, validations prevented, drift score, refinement queue.

Common panels: stage + per-stage progress, token/cost metrics, failure queue.

## Retrieval

- GraphRAG indexes are tagged with the `OntologyVersion` active at index time.
- Retrieval supports type/relationship filters and claim→evidence traversal.
- Migrations require reindex (`ke graphrag reindex --from-version vX`).
