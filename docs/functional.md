# Functional Guide — Methods, Rationale, Strengths & Benefits

> **Audience:** anyone deciding *what* this system does, *which* methods it applies,
> *why* they were chosen, and *what* you get from each. This is the conceptual companion
> to the [Technical Reference](./technical.md), which covers the implementation detail.

---

## 1. The problem and the approach

Naive RAG (embed chunks → cosine search → stuff into a prompt) fails on real documents:
it can't reason across sections, can't follow multi-hop relationships, ignores tables and
figures, and gives no provenance or governance over *what kinds of things* it extracts.

This system takes a **two-stage** approach:

1. **Knowledge construction** — turn each PDF into three parallel, queryable stores: a
   relational store (entities, relationships, claims, chunks), a property graph, and an
   optional Microsoft GraphRAG community index. Tables and figures are first-class:
   figures are interpreted by a vision model, tables are preserved cell-by-cell, and both
   are linked back to the chunks that reference them.
2. **Retrieval** — five interchangeable backends query those shared stores, trading
   ingestion cost, per-query cost, latency, and answer quality differently.

The benefit of separating the two stages: you build the knowledge once and then choose the
retrieval strategy per question, per demo, or per budget — without re-ingesting.

---

## 2. Two extraction modes

Extraction is governed by an **ontology** (the allowed entity types, relationship types,
and which source/target combinations are legal). Pick the mode with `ke ingest --mode`.

### Discovery — learn the ontology from the corpus

- **Method:** a reasoning model extracts entities/relationships **without** schema
  constraints. Entity names are embedded, clustered (HDBSCAN or k-means by size), and each
  cluster is summarized by an LLM into a candidate type. Pipeline:
  `DiscoveryExtractionPipeline` + `SemanticClusterer` + `OntologyProposalPipeline`.
- **Output:** a versioned `OntologyProposal` — never written to `ontology_versions`
  directly. The graph-build stage is not registered in this mode.
- **Why / benefit:** lets you bootstrap a schema for an unfamiliar corpus without guessing
  it up front, while keeping a human in the loop. The system proposes; a person approves.

### Governed — extract against an approved schema (default)

- **Method:** loads the active approved `OntologyVersion`. The prompt forces use of allowed
  types. A three-step validation pipeline runs on every chunk's output:
  - **`OntologyValidator`** rejects edges that violate `allowed_source` / `allowed_target`.
  - **`CanonicalizationService`** deduplicates entities (alias table → embedding cosine
    similarity → rapidfuzz fallback) and records merges.
  - **`DriftDetector`** tags chunks producing many `UNKNOWN` types or off-schema attempts.
- **Off-schema handling:** unknown types become `Entity(type=UNKNOWN)` plus a
  `RefinementSuggestion` queued for human review — nothing is silently dropped, and the
  schema can evolve through proposals.
- **Why / benefit:** consistent, auditable extraction. You get canonical entities (no
  "OpenAI" vs "OpenAI Inc." duplicates), validated relationships, and an early-warning
  drift signal when the corpus diverges from the schema.

---

## 3. Ontology governance — why it matters

The ontology is versioned and auditable (`ke ontology list|show|diff|approve|reject|propose|migrate`).
Every approved version is a semver'd YAML blob with approver provenance; every proposal,
rejection, alias, merge, and drift event is recorded in SQLite.

**Benefits:**

- **Reproducibility** — each GraphRAG index is tagged with the ontology version active at
  index time. You always know which schema produced a given answer.
- **Controlled evolution** — discovery proposals and governed refinements flow through the
  same approval gate, so the schema only changes when a human approves it.
- **Quality signal** — drift events surface when extraction quality degrades or the corpus
  drifts from the schema, before it silently corrupts retrieval.
- **Entity hygiene** — canonicalization keeps the graph clean, which is what makes
  multi-hop traversal actually work.

---

## 4. Five retrieval backends — one knowledge base

All five backends share the same SQLite store + filesystem artifacts. They differ in
*when* compute is spent and *what shape* of evidence they return. Select via
`ke graphrag ask --backend …`, `ke graphrag eval`, or `ke webui --backend …`.

| Backend   | Ingestion cost | Per-query cost | Compute spent | Best for |
| --------- | -------------- | -------------- | ------------- | -------- |
| `mini`    | $0 (chunks only) | ~0 LLM, instant | n/a | Lexical baseline; offline; CI/regression tests |
| `ms`      | ~$87 / ~80 min (HAI corpus) | 1 LLM call | Upfront (index build) | SOTA: community-aware synthesis, multi-hop |
| `lazy`    | **$0** — reuses chunks | 2 LLM calls (~10–20 s) | At query time | Graph-aware quality with zero ingestion cost |
| `agentic` | $0 | 4–10 LLM calls (~20–60 s) | At query time (plan + critique) | Broad/ambiguous, multi-source questions |
| `nav`     | $0 | 3–8 LLM calls (~15–45 s) | At query time (route + navigate) | Metadata-first routing into the real document |

### 4.1 `mini` — lexical baseline (BM25-style)

- **Method** (`MiniGraphRagAgent`): pure SQLite + filesystem, no LLM, no network. Tokenize
  the question, score candidates from five sources in parallel (claims, relationships,
  table summaries, figure captions+interpretations, entity names+aliases, with chunk text
  as fallback), sort, keep top-K. Optional 1-hop graph neighbours from the NetworkX export.
  `ask_multi(queries)` fuses paraphrases via **Reciprocal Rank Fusion**. Temporal-aware: a
  date-bearing question gets a bonus for hits containing parseable dates.
- **Strengths:** deterministic, offline, instant, zero cost. Returns chunks verbatim with
  exact provenance.
- **Use when:** you need a reproducible baseline — CI, air-gapped benchmarks, the eval
  reference. Not for synthesis or multi-hop reasoning.

### 4.2 `lazy` — LazyGraphRAG (JIT subgraph at query time)

- **Method** (`LazyGraphRagAgent`): implements Microsoft Research's "LazyGraphRAG" pattern
  — **no** index-time entity/relationship extraction. Two LLM calls per ask: (1) retrieve
  top-K chunks + immediate neighbours (`ChunkRetriever`, BM25), send to the LLM to extract
  a JIT subgraph `(entities, relationships, claims)`, all chunk-cited; (2) synthesize a
  grounded answer with inline chunk citations. Returns the chunks + subgraph so callers can
  audit the evidence.
- **Strengths:** graph-aware answer quality **without** paying the eager indexing cost
  (~$0 ingestion). ~10–20 s/ask, a few thousand tokens.
- **Use when:** you want better-than-lexical, graph-flavoured answers but can't justify the
  full MS GraphRAG index build. *Caveat:* currently too eager to answer out-of-scope
  questions — a refusal guard in the synthesis prompt is a known follow-up.

### 4.3 `ms` — Microsoft GraphRAG (production / SOTA)

- **Method** (`MsGraphRagAgent`): shells out to `graphrag query` over the pre-built
  community graph. Four `--method` values, auto-routed by `_route_method`:

  | Method   | What it does                                                | Best for |
  | -------- | ----------------------------------------------------------- | -------- |
  | `local`  | Entity-centric: text units around named entities            | Single-fact / lookup (~30–40 s) |
  | `global` | Map-reduce over community reports, then aggregate           | Thematic synthesis ("overall trend…") (~200 s) |
  | `drift`  | Entity-anchored expansion through the community graph        | Multi-hop traversal |
  | `basic`  | Vector similarity over text_units only (no graph)            | RAG baseline |

  Synthesis cues (`compare`, `trends`, `overall`, `summary`, `themes`) route to `global`;
  factoid leaders (`when/where/who/which`, numerics) route to `local`.
- **Strengths:** the full Microsoft GraphRAG stack — community-aware synthesis and
  multi-hop entity traversal that lexical retrieval cannot reach in any number of hops.
- **Cost / trade-off:** ~$87 and ~80 min to index ~800 chunks; queries are slow
  (~30–200 s). You pay upfront for SOTA quality.
- **Use when:** quality matters more than latency/cost and the question needs
  cross-document, community-level synthesis.

### 4.4 `agentic` — bounded multi-step reasoning loop

- **Method** (`AgenticSearchAgent`): a self-contained loop — no external agent framework.

  ```
  question
    → Planner (LLM)      — 3–5 subquestions
    → Searcher           — MiniGraphRagAgent.ask_multi (BM25 + RRF)
    → EvidenceInspector  — compress + label hits into EvidenceItem list
    → Critic (LLM)       — sufficiency verdict; emits follow-up queries if needed
    → [loop back to Searcher if insufficient AND round < max_rounds]
    → Synthesizer (LLM)  — grounded answer with inline [Cx] citations
  ```

  Loop bounds are hard-capped (`max_rounds=2`, `max_subquestions=5`,
  `top_k_per_query=8`, `max_total_evidence_items=30`) to prevent runaway cost. The three
  LLM roles have independent model settings that fall back to the reasoning/extraction
  models.
- **Strengths:** decomposes broad questions, retrieves iteratively, and self-critiques
  before answering — so a single incomplete retrieval pass doesn't sink the answer. The
  UI surfaces the plan, the critique, and every evidence item.
- **Cost / trade-off:** 4–10 LLM calls (~20–60 s, ~8–20k tokens).
- **Use when:** the question is broad ("What are the major trends?"), ambiguous, or needs
  evidence from multiple sections. **Not** for simple factoids — use `mini` or `lazy`.

### 4.5 `nav` — Agentic Navigator (metadata-first routing)

- **Method** (`AgenticNavAgent` + `DocumentNavigator`): unlike the others, `nav` does
  **not** fan out blind chunk retrieval. It routes on metadata, then reads the real
  document:

  ```
  question
    → Router (LLM)      — sees a metadata catalog only (titles, counts, captions,
                          previews) and picks ≤ max_docs candidate documents
    → Navigator (LLM)   — bounded ReAct tool loop: open_document, read_section,
                          search_document, get_table, get_figure, finish
    → Synthesizer (LLM) — grounded answer citing the documents/sections used
  ```

  `DocumentNavigator` reads directly from SQLite (raw read-only `sqlite3`) plus the
  `doc.md` artifact, and is schema/filesystem tolerant (missing tables/columns/`doc.md`
  degrade to chunk-text fallbacks). The loop is fully bounded — allowlisted tool schema,
  clamped args, a `document_id` allowlist, and invalid/no-progress streak caps.
- **Strengths:** mirrors how a human researches — pick the right document first, then drill
  into the right section/table/figure on demand. Strong when the corpus has many documents
  and the first retrieval is likely incomplete.
- **Cost / trade-off:** 3–8 LLM calls (~15–45 s).
- **Use when:** broad questions over a multi-document corpus where routing matters more
  than brute-force chunk recall.

### 4.6 Evidence assembly (UI)

For `mini` and `lazy`, the webui builds an "evidence" panel from `figure_refs_json` /
`table_refs_json` on the returned chunks (which is why correct figure linkage matters — see
the Technical Reference). It prefers figures that have a real `image_path` over synthetic
page placeholders. `ms` returns prose only, so the UI hints to use `mini`/`lazy` for
explicit diagram links.

---

## 5. Benchmarks — quality vs cost

### HAI 2025 benchmark (32-case suite)

| Backend    | Pass  | MRR  | Avg query (s) | Index ($) | Index time |
| ---------- | ----- | ---- | ------------- | --------- | ---------- |
| mini       | 28/32 | 0.69 | <1            | $0        | n/a        |
| lazy       | 12/32 | 0.78 | ~15           | **$0**    | **0**      |
| ms (local) | 5/32  | 0.41 | ~42           | $87.83    | ~80 min    |

**How to read this.** The suite scores **lexical overlap with chunk text**, which favours
`mini` (returns chunks verbatim). `lazy` and `ms` synthesize prose, so their pass-rates
*undercount* answer quality — `lazy` nonetheless beats `ms` ~2.4× here at zero ingestion
cost. Adversarial refusal: `ms` 2/2, `mini` 2/2, `lazy` 0/2 (lazy is too eager on
out-of-scope questions). The takeaway is not "mini is best" but "match the backend to the
question and to your cost budget."

### 4-way comparison (`ke graphrag bench`)

For an apples-to-apples comparison measuring quality **and** cost, `graphrag bench` runs a
curated suite (`config/evals/bench_4way.json`) and reports per-backend quality (pass rate,
MRR), per-query latency (p50/p95/total), token cost (in/out/total), the one-off ingestion
cost (from `work/logs/run-*.jsonl`), and the MS index runtime. Tokens are `0` for `mini`
(never calls an LLM) versus `n/a` for `ms` (doesn't expose counts) — the distinction
between "zero cost" and "not measured" is preserved. Results land in `work/benchmarks/` as
JSON + markdown.

---

## 6. Evaluation framework

`application/services/graphrag_eval.py` scores each case with **MRR, precision@k,
recall@k, citation_recall, and top_score**, plus per-category aggregates. Adversarial
("should refuse") cases pass when `top_score < min_score_for_grounded` — i.e. the system is
rewarded for *not* answering out-of-scope questions. Agentic runs add metadata (`rounds`,
`subquestions_count`, `follow_up_queries_count`, `critic_confidence`,
`evidence_sufficient`); nav adds `steps` and `selected_documents`.

**Benefit:** every retrieval change can be measured side-by-side across backends on a fixed
suite before it ships, including the cost dimension — not just a vibe check.

---

## 7. Why a vision pass on figures

Charts and diagrams carry information that never appears in the body text. The figures
stage crops each figure and sends it to a vision model, which returns a structured
`ChartInterpretation` (title, chart type, prose interpretation, confidence). That prose is
then **inlined into the extraction prompt** for any chunk on the figure's page, so the
extraction model effectively "sees" the chart. The result: claims and entities grounded in
figures, not just paragraphs — and an evidence panel that can show the actual diagram next
to the answer.

---

## 8. Why the optional Neo4j layer

MS GraphRAG's output is parquet + LLM prose — the underlying knowledge graph is invisible.
The opt-in Neo4j stack (`ke neo4j up|load|open`) lets you **see** the entities,
relationships, and communities, and trace the exact multi-hop paths GraphRAG follows to
answer a question — the kind of traversal lexical retrieval can't reach. It's a
post-hoc visualization (zero impact on the production path), kept behind an optional extra
so the base install stays lean. See the [Technical Reference §13](./technical.md#13-neo4j-visualization-layer-demo-only-opt-in)
for the schema and lifecycle.
