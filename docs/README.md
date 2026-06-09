# AdvancedRAG — Documentation

Comprehensive documentation for the `knowledge_extraction` system. Start with whichever
matches your intent:

| Doc | Read this if you want to know… |
| --- | ------------------------------ |
| **[Functional Guide](./functional.md)** | *Which* methods are applied, *how* they work, *why* they were chosen, and their strengths, benefits, and trade-offs. Extraction modes, ontology governance, the five retrieval backends, benchmarks, and evaluation. |
| **[Technical Reference](./technical.md)** | *How* it works internally. Architecture (hexagonal/ports & adapters), the pipeline stage by stage, persistence, observability, checkpointing/resilience, configuration, CLI, gotchas, file layout, and the Neo4j layer. |

Other material:

- **[knowledge_extraction/README.md](../knowledge_extraction/README.md)** — quickstart,
  install, and CLI reference for the package itself.
- **[RAG_Storyline.md](./RAG_Storyline.md)** — "Beyond Naive RAG" presentation deck (Marp).

> These two guides supersede the former `architecture.md` and `for_llm.md`, which have been
> merged here.
