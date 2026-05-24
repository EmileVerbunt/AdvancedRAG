// Demo Cypher for the MS GraphRAG knowledge graph loaded by `ke neo4j load`.
//
// Open http://localhost:7474, log in (neo4j / graphrag-demo), then paste the
// section you want into the editor. Each section is independent.
//
// Schema (loaded from work/graphrag/<ver>/output/*.parquet):
//   (:Entity     {title, type, description, degree, frequency, human_id})
//   (:Community  {community, level, title, summary, rating, rank, size})
//   (:TextUnit   {id, n_tokens})              -- optional, loaded with --text-units
//   (:Entity)-[:RELATED_TO   {weight, description}]->(:Entity)
//   (:Entity)-[:IN_COMMUNITY {level}]->(:Community)
//   (:Community)-[:PARENT_OF]->(:Community)
//   (:Entity)-[:MENTIONED_IN]->(:TextUnit)    -- optional
//
// All entity titles are upper-cased by MS GraphRAG. Match accordingly.


// ── 1. Sanity check: how big is the graph? ─────────────────────────────────
MATCH (e:Entity)        WITH count(e) AS entities
MATCH (c:Community)     WITH entities, count(c) AS communities
MATCH ()-[r:RELATED_TO]-() WITH entities, communities, count(r)/2 AS relationships
RETURN entities, relationships, communities;


// ── 2. The killer demo: Character.AI subgraph ──────────────────────────────
// Shows the chatbot-harm story (Sewell Setzer III + Crecente) visually.
// Expand to 2 hops to surface the full social network around the lawsuits.
MATCH (e:Entity {title: 'CHARACTER.AI'})
OPTIONAL MATCH path = (e)-[:RELATED_TO*1..2]-(n:Entity)
RETURN e, path, n
LIMIT 75;


// ── 3. Shortest path: Sewell Setzer III ↔ Jennifer Ann Crecente ────────────
// Two separate real-world harm cases. GraphRAG knows they connect via
// Character.AI even though they live in different paragraphs of the source.
MATCH (a:Entity {title: 'SEWELL SETZER III'}),
      (b:Entity {title: 'JENNIFER ANN CRECENTE'}),
      p = shortestPath((a)-[:RELATED_TO*..5]-(b))
RETURN p;


// ── 4. Top-degree entities (the "celebrities" of the corpus) ───────────────
MATCH (e:Entity)
RETURN e.title AS title, e.type AS type, e.degree AS degree, e.frequency AS frequency
ORDER BY e.degree DESC, e.frequency DESC
LIMIT 25;


// ── 5. Find the chatbot-harm community + its members ───────────────────────
// MS GraphRAG put the Character.AI story into a small leaf-level community.
// This finds it dynamically and pulls in everyone who lives there.
MATCH (e:Entity {title: 'CHARACTER.AI'})-[:IN_COMMUNITY]->(c:Community)
WITH c ORDER BY c.level DESC LIMIT 1
MATCH (member:Entity)-[:IN_COMMUNITY]->(c)
RETURN c.title AS community, c.summary AS summary,
       collect(DISTINCT member.title) AS members,
       count(DISTINCT member) AS member_count;


// ── 6. Community hierarchy at the top ──────────────────────────────────────
// MS GraphRAG builds a hierarchical clustering. This shows the root
// communities and how many entities each contains.
MATCH (c:Community {level: 0})
RETURN c.community AS id, c.title AS title, c.size AS size, c.rating AS rating
ORDER BY c.size DESC LIMIT 20;


// ── 7. Cross-community bridges (entities that link clusters) ───────────────
// Entities that sit in multiple level-0 communities tend to be the
// integrative concepts — these are exactly what GraphRAG's global queries
// exploit and what BM25 cannot find.
MATCH (e:Entity)-[:IN_COMMUNITY]->(c:Community {level: 0})
WITH e, count(DISTINCT c) AS communities
WHERE communities >= 2
RETURN e.title AS bridge_entity, e.type AS type, communities, e.degree AS degree
ORDER BY communities DESC, degree DESC LIMIT 25;


// ── 8. GDS PageRank: most central entities globally ────────────────────────
// Requires the Graph Data Science plugin (bundled in the docker-compose).
// First time only: project the graph, then run pagerank.
CALL gds.graph.project.cypher(
  'entities_g',
  'MATCH (e:Entity) RETURN id(e) AS id',
  'MATCH (a:Entity)-[r:RELATED_TO]-(b:Entity) RETURN id(a) AS source, id(b) AS target, r.weight AS weight'
) YIELD graphName;
//
CALL gds.pageRank.stream('entities_g', {relationshipWeightProperty: 'weight'})
YIELD nodeId, score
MATCH (e:Entity) WHERE id(e) = nodeId
RETURN e.title AS title, e.type AS type, round(score * 1000) / 1000 AS pagerank
ORDER BY pagerank DESC LIMIT 25;


// ── 9. Trace an entity back to its source paragraphs (optional) ────────────
// Only useful if you loaded text_units (`ke neo4j load --text-units`).
MATCH (e:Entity {title: 'SEWELL SETZER III'})-[:MENTIONED_IN]->(t:TextUnit)
RETURN e.title, t.id, t.n_tokens
ORDER BY t.n_tokens DESC LIMIT 5;
