# AdvancedRAG

Production-grade PDF → ontology + property graph → Microsoft GraphRAG, with five
interchangeable retrieval backends. The system turns PDFs into queryable knowledge stores
(relational + property graph + community index), treats tables and figures as first-class
evidence, and governs *what* gets extracted through a versioned ontology.

## Where to go

| You want to… | Go to |
| ------------ | ----- |
| **Run it** — install, ingest a PDF, query it | **[knowledge_extraction/README.md](./knowledge_extraction/README.md)** |
| Understand *which methods* are used, *why*, and their strengths/benefits | **[docs/functional.md](./docs/functional.md)** |
| Understand *how it works* internally | **[docs/technical.md](./docs/technical.md)** |
| Browse all docs | **[docs/README.md](./docs/README.md)** |

## Repository layout

```
knowledge_extraction/   # the system: uv-managed Python 3.12 package (start here to run)
docs/                   # functional + technical documentation
infrastructure/neo4j/   # optional Neo4j visualization stack (docker-compose + Cypher)
reports/                # generated reports / artifacts
```

## Quickstart

```bash
cd knowledge_extraction
uv sync
cp .env.example .env       # fill in Azure endpoints / auth
uv run ke preflight        # config / auth checks
uv run ke ingest           # full ingest + extract for all PDFs in assets/
uv run ke webui --backend lazy
```

See [knowledge_extraction/README.md](./knowledge_extraction/README.md) for the full CLI
reference and the four/five retrieval modes.
