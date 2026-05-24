# GraphRAG-vs-BM25 demo script

A curated set of **6 natural-language questions** where vector/BM25 RAG demonstrably
fails or under-performs, paired with the **Neo4j Cypher query** that visually proves
*why* GraphRAG gets the right answer.

Run each demo in two acts:

1. **Ask the NL question** in the webui with `--backend mini` (BM25). Note the gap.
2. **Paste the matching Cypher** into Neo4j Browser at <http://localhost:7474>
   (`neo4j` / `graphrag-demo`) to show the graph structure that makes the
   GraphRAG answer possible. Then re-ask the same question with `--backend ms`
   to land the punchline.

> Prereqs (one-time): `uv sync --extra neo4j`, `uv run ke neo4j up`, `uv run ke neo4j load`, `uv run ke neo4j open`.

---

## Demo 1 — Cross-document multi-hop bridge (THE killer)

> **Question:** *"How does Anthropic's work on computer-use agents connect to
> the Character.AI lawsuit involving Sewell Setzer III?"*

**Why BM25 fails.** These two stories live in **different PDFs**: Anthropic's
"Building Effective AI Agents" mentions Anthropic + computer use; the HAI AI
Index Report 2025 describes the Setzer lawsuit. No chunk contains both
contexts; lexical similarity is ~zero. BM25 returns chunks about Anthropic
*or* chunks about Setzer, never the connection.

**Cypher (lands the path in one screen):**

```cypher
MATCH p = shortestPath(
  (a:Entity {title: 'ANTHROPIC COMPUTER USE'})
  -[:RELATED_TO*..6]-
  (b:Entity {title: 'SEWELL SETZER III'})
)
RETURN p;
```

**What you'll see.** A 5-hop chain:
`ANTHROPIC COMPUTER USE → ANTHROPIC → GOOGLE → CHARACTER.AI → LAWSUIT AGAINST CHARACTER.AI → SEWELL SETZER III`.

**Why GraphRAG wins.** The graph was built from **both documents** at ingest
time. Entities are typed, relationships are weighted, and `shortestPath` finds
the actual narrative bridge between the two stories.

---

## Demo 2 — Two unrelated victims, one common cause

> **Question:** *"What harms have AI chatbots caused to identifiable
> individuals, and is there a common platform?"*

**Why BM25 fails.** Sewell Setzer III (page 96) and Jennifer Ann Crecente
(page 95) are described in **separate paragraphs**, in different contexts
(suicide-after-chatbot vs. impersonation-of-deceased). The only token they
share is "Character.AI". BM25 will surface one or the other depending on
phrasing, never both as a connected pattern.

**Cypher:**

```cypher
MATCH (a:Entity {title: 'SEWELL SETZER III'}),
      (b:Entity {title: 'JENNIFER ANN CRECENTE'}),
      p = shortestPath((a)-[:RELATED_TO*..5]-(b))
RETURN p;
```

**What you'll see.** A 3-hop path through `LAWSUIT AGAINST CHARACTER.AI →
CHARACTER.AI`. Two entirely separate tragedies provably connected through one
platform — visible at a glance.

**Why GraphRAG wins.** It synthesizes across paragraph boundaries by walking
typed edges, not token overlap.

---

## Demo 3 — Neighborhood expansion (the "social graph" view)

> **Question:** *"Show me the full ecosystem around Character.AI — who's
> involved, what events, what lawsuits, what corporate ties?"*

**Why BM25 fails.** This is a **whole-neighborhood** question. BM25 returns
the top-k chunks containing "Character.AI"; you get duplicated mentions of
the same 2–3 facts. The breadth of the actor network is invisible.

**Cypher (the visual money-shot):**

```cypher
MATCH (e:Entity {title: 'CHARACTER.AI'})
OPTIONAL MATCH path = (e)-[:RELATED_TO*1..2]-(n:Entity)
RETURN e, path, n
LIMIT 75;
```

**What you'll see.** A starburst with `CHARACTER.AI` at the center —
**SEWELL SETZER III**, **JENNIFER ANN CRECENTE**, **DREW CRECENTE**,
**LAWSUIT AGAINST CHARACTER.AI**, **NOAM SHAZEER**, **GOOGLE**,
**AUGUST 2, 2024 GOOGLE-CHARACTER.AI DEAL**, etc. Two-hop expansion brings in
adjacent regulators, articles, and policies.

**Why GraphRAG wins.** Community-aware retrieval pulls in the *typed
neighborhood*, not the top-k chunks.

---

## Demo 4 — Hierarchical drill-down (the community structure)

> **Question:** *"What's the smallest sub-cluster of entities that captures
> the chatbot-impersonation story, and what's in it?"*

**Why BM25 fails.** There's no "cluster" in BM25's world. It's a flat index of
chunks. Concepts like "the tightest community containing X" don't exist.

**Cypher (zooms from broad to specific):**

```cypher
MATCH (e:Entity {title: 'CHARACTER.AI'})-[:IN_COMMUNITY]->(c:Community)
WITH c ORDER BY c.level DESC LIMIT 1
MATCH (m:Entity)-[:IN_COMMUNITY]->(c)
RETURN c.community AS community, c.level AS level, c.size AS size,
       substring(c.summary, 0, 240) AS summary,
       collect(DISTINCT m.title) AS members;
```

**What you'll see.** Community **1627** (level 3, size 4) with members:
`CHARACTER.AI`, `AI CHATBOT APPEARANCE ON CHARACTER.AI`, `UNKNOWN USER`,
`AUGUST 2, 2024 GOOGLE-CHARACTER.AI DEAL` — and an LLM-generated summary
explaining what binds them together.

Then zoom out to level 2:

```cypher
MATCH (e:Entity {title: 'CHARACTER.AI'})-[:IN_COMMUNITY]->(c:Community {level: 2})
MATCH (m:Entity)-[:IN_COMMUNITY]->(c)
RETURN c.community, c.size, substring(c.summary, 0, 300) AS summary,
       collect(m.title) AS members;
```

Now community **857** (size 10) appears — adding `JENNIFER ANN CRECENTE`,
`DREW CRECENTE`, `CHATBOT REMOVAL FOR IMPERSONATION POLICY VIOLATION`, etc.

**Why GraphRAG wins.** Hierarchical Leiden clustering at ingest time gave us
**1,772 communities at 4 levels**. Global/coarse questions hit high levels;
specific questions hit leaves. BM25 has neither.

---

## Demo 5 — Typed enumeration (the schema-aware question)

> **Question:** *"List the safety / red-team benchmarks AI models are
> evaluated against in the report."*

**Why BM25 fails.** "Benchmark" is a category, not a string. Some entries are
named `HARMBENCH`, others `SIMPLESAFETYTESTS`, `HELM SAFETY`, `AIR-BENCH
2024` — they share **no token**. BM25 misses anything that doesn't contain
the word "benchmark" in the surrounding sentence.

**Cypher (returns 15+ in one shot):**

```cypher
MATCH (e:Entity)
WHERE toLower(e.title) ENDS WITH 'bench'
   OR toLower(e.title) CONTAINS 'safetytests'
   OR toLower(e.title) CONTAINS 'helm safety'
   OR toLower(e.title) CONTAINS 'air-bench'
   OR toLower(e.title) CONTAINS 'harmbench'
RETURN e.title AS benchmark, e.type AS type, e.degree AS connections
ORDER BY e.degree DESC LIMIT 20;
```

**What you'll see.** A complete, ranked list:
`VISUALAGENTBENCH, MVBENCH, AIR-BENCH 2024, WILDBENCH, BIGCODEBENCH,
HARMBENCH, SIMPLESAFETYTESTS, AGENTBENCH, SWE-BENCH, HELM SAFETY, PLANBENCH,
RE-BENCH, BETTERBENCH, …`

**Why GraphRAG wins.** Entities are **extracted and typed once** at ingest
time. Aggregation across many chunks is a graph query, not a search.

---

## Demo 6 — Cross-cluster bridge entities (the "connectors")

> **Question:** *"Which AI models or benchmarks act as integrative reference
> points across the most distinct topical sub-areas of the report?"*

**Why BM25 fails.** This question requires identifying entities whose
**neighborhood spans multiple distinct topical clusters**. BM25 has no notion
of topical clusters, let alone cluster-spanning entities.

**Cypher (finds the actual connectors):**

```cypher
MATCH (e:Entity)-[:RELATED_TO]-(n:Entity)-[:IN_COMMUNITY]->(c:Community {level: 0})
WITH e, count(DISTINCT c) AS bridged_clusters, count(DISTINCT n) AS neighbors
WHERE bridged_clusters >= 5
  AND neighbors >= 20
  // exclude meta-entities (the report, generic years, "AI" itself)
  AND NOT toLower(e.title) CONTAINS 'index report'
  AND NOT toLower(e.title) IN ['ai', 'llms', '2023', '2024', '2025', 'ai index']
RETURN e.title AS bridge_entity, e.type AS type,
       bridged_clusters, neighbors
ORDER BY bridged_clusters DESC, neighbors DESC
LIMIT 15;
```

**What you'll see.** A ranked list led by `UNITED STATES`, `ARXIV`, `GPT-4`,
`MMLU`, `OPENAI`, `GOOGLE`, `META AI` — the entities the AI Index report
keeps circling back to because they sit at the **intersection** of multiple
sub-topics (model evals, safety, governance, industry, academia).

**Why GraphRAG wins.** GraphRAG's **global search** mode explicitly walks
these high-betweenness bridges to assemble cross-cluster answers ("compare
GPT-4 across all the contexts the report discusses it in"). BM25 returns
isolated mentions per cluster, never the connective tissue.

---

## Quick-reference table

| # | NL question | BM25 failure mode | Cypher returns |
|---|---|---|---|
| 1 | "Anthropic computer-use ↔ Setzer lawsuit?" | Different PDFs, zero token overlap | 5-hop cross-doc path |
| 2 | "Common platform behind chatbot-harm victims?" | Stories in different paragraphs | 3-hop shared root |
| 3 | "Full Character.AI ecosystem?" | Returns top-k duplicates | 50+ node neighborhood |
| 4 | "Smallest cluster around chatbot impersonation?" | No cluster concept | 4-node leaf community |
| 5 | "List all safety benchmarks evaluated" | No token overlap across benchmark names | 15+ typed entities |
| 6 | "Org bridges between safety and FM-dev topics?" | No topical-cluster concept | Top connector orgs |

---

## Demo-day flow (≈8 minutes total)

| Time | Action |
|------|--------|
| 0:00 | `uv run ke webui --backend mini` — ask Demo 1's NL question. Show weak answer. |
| 0:45 | Open Neo4j Browser, paste Demo 1 Cypher. **"This is the bridge BM25 can't see."** |
| 2:00 | Paste Demo 2 Cypher. "Two separate tragedies, one root cause — visible in 3 hops." |
| 3:30 | Paste Demo 3 Cypher. "The whole social graph around one platform." |
| 5:00 | Paste Demo 4 Cypher. Drill down through community levels 3 → 2 → 1. |
| 6:30 | Paste Demo 5 Cypher. "Try this with BM25." |
| 7:30 | Re-run webui with `--backend ms`. Same questions, real answers. |

---

## Teardown

```powershell
uv run ke neo4j down    # stops container; data persists for next demo
```

All raw queries (plus PageRank/GDS extras) are in
[`demo_queries.cypher`](./demo_queries.cypher).
