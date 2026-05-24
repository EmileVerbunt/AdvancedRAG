"""Load MS GraphRAG parquet output into a Neo4j instance for visualisation.

Schema written to Neo4j
-----------------------
* ``(:Entity {id, human_id, title, type, description, frequency, degree})``
  -- ``title`` is the unique key (upper-cased, as emitted by GraphRAG).
* ``(:Community {id, community, level, parent_id, title, summary,
  rating, rank, size})``
* ``(:TextUnit {id, n_tokens})`` -- optional, only when ``load_text_units`` is
  ``True``. Useful to back-trace an entity to its source paragraphs.
* ``(:Entity)-[:RELATED_TO {weight, description}]->(:Entity)`` -- one
  directed edge per parquet row; treat as undirected in queries.
* ``(:Entity)-[:IN_COMMUNITY {level}]->(:Community)``
* ``(:Community)-[:PARENT_OF]->(:Community)``
* ``(:Entity)-[:MENTIONED_IN]->(:TextUnit)`` -- optional

Notes
-----
The ``relationships.parquet`` from MS GraphRAG stores ``source`` / ``target``
as the entity *title* (already upper-cased), NOT as an entity UUID. The loader
keys entities by ``title`` so the join Just Works.

The loader is idempotent: it MERGEs on the unique key. Re-running after a
re-index updates properties without duplicating nodes. The ``wipe()`` helper
is provided for full resets.

Dependencies
------------
``pip install '.[neo4j]'`` -- pulls in ``neo4j>=5,<6`` and ``pandas`` (the
latter is already a transitive dep of ``graphrag``).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# These files MUST exist under ``output_dir`` for a loadable run.
REQUIRED_PARQUETS = ("entities", "relationships", "communities", "community_reports")
# Optional — only read when ``load_text_units=True``.
OPTIONAL_PARQUETS = ("text_units",)

# Batch size for UNWIND payloads. 1k keeps the driver under the 64MB request
# limit even with long entity descriptions.
DEFAULT_BATCH_SIZE = 1000


@dataclass(slots=True)
class LoadStats:
    """What ``GraphRagNeo4jLoader.load`` did, for the CLI summary line."""
    entities: int = 0
    relationships: int = 0
    communities: int = 0
    community_reports: int = 0
    text_units: int = 0
    parent_edges: int = 0
    in_community_edges: int = 0
    mentioned_in_edges: int = 0
    skipped_relationships_missing_endpoint: int = 0
    extras: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int]:
        out = {
            "entities": self.entities,
            "relationships": self.relationships,
            "communities": self.communities,
            "community_reports": self.community_reports,
            "text_units": self.text_units,
            "parent_edges": self.parent_edges,
            "in_community_edges": self.in_community_edges,
            "mentioned_in_edges": self.mentioned_in_edges,
            "skipped_relationships_missing_endpoint": self.skipped_relationships_missing_endpoint,
        }
        out.update(self.extras)
        return out


# ── Pure-Python payload builders (no DB, unit-testable) ────────────────────


def _safe(value: Any) -> Any:
    """Coerce numpy / pandas scalars into Neo4j-compatible Python primitives."""
    # pandas.NA / numpy nan
    try:
        import pandas as pd  # local import keeps the helper importable without pandas at module scan time
        if value is None or (not isinstance(value, list | tuple) and pd.isna(value)):
            return None
    except (ImportError, ValueError, TypeError):
        if value is None:
            return None
    if hasattr(value, "item"):  # numpy scalar
        try:
            return value.item()
        except (AttributeError, ValueError):
            pass
    return value


def _to_list(value: Any) -> list[Any]:
    """``np.ndarray`` / tuple / list -> ``list`` of Python primitives."""
    if value is None:
        return []
    if hasattr(value, "tolist"):
        return [_safe(v) for v in value.tolist()]
    if isinstance(value, list | tuple):
        return [_safe(v) for v in value]
    return [_safe(value)]


def _chunks(items: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def build_entity_payload(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the UNWIND payload for ``(:Entity)`` upserts.

    Skips rows with empty title (cannot key on it). Truncates description to
    keep request size sane.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        title = _safe(r.get("title"))
        if not title:
            continue
        out.append({
            "id": _safe(r.get("id")),
            "human_id": _safe(r.get("human_readable_id")),
            "title": str(title),
            "type": _safe(r.get("type")) or "UNKNOWN",
            "description": (str(_safe(r.get("description")) or ""))[:4000],
            "frequency": _safe(r.get("frequency")) or 0,
            "degree": _safe(r.get("degree")) or 0,
        })
    return out


def build_relationship_payload(
    rows: Iterable[dict[str, Any]],
    known_titles: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Build the UNWIND payload for ``[:RELATED_TO]`` edges.

    Returns ``(payload, n_skipped)``. Skips rows whose source/target title is
    missing from ``known_titles`` (when supplied) so we never emit edges to
    nodes that don't exist.
    """
    out: list[dict[str, Any]] = []
    skipped = 0
    for r in rows:
        src = _safe(r.get("source"))
        tgt = _safe(r.get("target"))
        if not src or not tgt:
            skipped += 1
            continue
        if known_titles is not None and (src not in known_titles or tgt not in known_titles):
            skipped += 1
            continue
        out.append({
            "source": str(src),
            "target": str(tgt),
            "weight": float(_safe(r.get("weight")) or 1.0),
            "description": (str(_safe(r.get("description")) or ""))[:1000],
        })
    return out, skipped


def build_community_payload(
    community_rows: Iterable[dict[str, Any]],
    report_rows: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build payloads for ``(:Community)`` nodes, ``IN_COMMUNITY`` and
    ``PARENT_OF`` edges.

    Returns ``(community_nodes, in_community_edges, parent_edges)``.
    Community-report properties (summary, rating, rank) are merged in by
    community id so the demo queries can show the report alongside the node.
    """
    reports_by_id: dict[int, dict[str, Any]] = {}
    if report_rows is not None:
        for r in report_rows:
            cid = _safe(r.get("community"))
            if cid is None:
                continue
            reports_by_id[int(cid)] = {
                "summary": (str(_safe(r.get("summary")) or ""))[:8000],
                "rating": _safe(r.get("rating")) or 0.0,
                "rank": _safe(r.get("rank")) or 0.0,
                "full_content": (str(_safe(r.get("full_content")) or ""))[:16000],
            }

    nodes: list[dict[str, Any]] = []
    in_community: list[dict[str, Any]] = []
    parent_edges: list[dict[str, Any]] = []

    for r in community_rows:
        cid_raw = _safe(r.get("community"))
        if cid_raw is None:
            continue
        cid = int(cid_raw)
        level = int(_safe(r.get("level")) or 0)
        parent_raw = _safe(r.get("parent"))
        parent_id: int | None = None
        if parent_raw is not None and int(parent_raw) >= 0:
            parent_id = int(parent_raw)

        report = reports_by_id.get(cid, {})
        nodes.append({
            "id": str(_safe(r.get("id")) or f"community-{cid}"),
            "community": cid,
            "level": level,
            "parent_id": parent_id,
            "title": str(_safe(r.get("title")) or f"Community {cid}"),
            "size": int(_safe(r.get("size")) or 0),
            "summary": report.get("summary", ""),
            "rating": float(report.get("rating", 0.0)),
            "rank": float(report.get("rank", 0.0)),
            "full_content": report.get("full_content", ""),
        })

        if parent_id is not None:
            parent_edges.append({"child": cid, "parent": parent_id})

        for entity_id in _to_list(r.get("entity_ids")):
            if entity_id:
                in_community.append({
                    "entity_id": str(entity_id),
                    "community": cid,
                    "level": level,
                })

    return nodes, in_community, parent_edges


def build_text_unit_payload(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build payloads for ``(:TextUnit)`` nodes and ``[:MENTIONED_IN]`` edges.

    Returns ``(text_unit_nodes, mentioned_in_edges)``. Each edge keys the
    entity by its UUID (the ``entity_ids`` list inside a text_unit row holds
    entity UUIDs, not titles).
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for r in rows:
        tid = _safe(r.get("id"))
        if not tid:
            continue
        nodes.append({
            "id": str(tid),
            "n_tokens": int(_safe(r.get("n_tokens")) or 0),
            "human_id": _safe(r.get("human_readable_id")),
        })
        for eid in _to_list(r.get("entity_ids")):
            if eid:
                edges.append({"entity_id": str(eid), "text_unit_id": str(tid)})
    return nodes, edges


# ── Cypher statements ─────────────────────────────────────────────────────


CYPHER_CONSTRAINTS = [
    "CREATE CONSTRAINT entity_title IF NOT EXISTS FOR (e:Entity) REQUIRE e.title IS UNIQUE",
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT community_id IF NOT EXISTS FOR (c:Community) REQUIRE c.community IS UNIQUE",
    "CREATE CONSTRAINT text_unit_id IF NOT EXISTS FOR (t:TextUnit) REQUIRE t.id IS UNIQUE",
]

CYPHER_UPSERT_ENTITY = """
UNWIND $rows AS row
MERGE (e:Entity {title: row.title})
SET   e.id          = row.id,
      e.human_id    = row.human_id,
      e.type        = row.type,
      e.description = row.description,
      e.frequency   = row.frequency,
      e.degree      = row.degree
"""

CYPHER_UPSERT_RELATIONSHIP = """
UNWIND $rows AS row
MATCH (a:Entity {title: row.source})
MATCH (b:Entity {title: row.target})
MERGE (a)-[r:RELATED_TO]->(b)
SET   r.weight      = row.weight,
      r.description = row.description
"""

CYPHER_UPSERT_COMMUNITY = """
UNWIND $rows AS row
MERGE (c:Community {community: row.community})
SET   c.id          = row.id,
      c.level       = row.level,
      c.parent_id   = row.parent_id,
      c.title       = row.title,
      c.size        = row.size,
      c.summary     = row.summary,
      c.rating      = row.rating,
      c.rank        = row.rank,
      c.full_content = row.full_content
"""

CYPHER_UPSERT_IN_COMMUNITY = """
UNWIND $rows AS row
MATCH (e:Entity {id: row.entity_id})
MATCH (c:Community {community: row.community})
MERGE (e)-[r:IN_COMMUNITY]->(c)
SET   r.level = row.level
"""

CYPHER_UPSERT_PARENT_OF = """
UNWIND $rows AS row
MATCH (child:Community  {community: row.child})
MATCH (parent:Community {community: row.parent})
MERGE (parent)-[:PARENT_OF]->(child)
"""

CYPHER_UPSERT_TEXT_UNIT = """
UNWIND $rows AS row
MERGE (t:TextUnit {id: row.id})
SET   t.n_tokens = row.n_tokens,
      t.human_id = row.human_id
"""

CYPHER_UPSERT_MENTIONED_IN = """
UNWIND $rows AS row
MATCH (e:Entity {id: row.entity_id})
MATCH (t:TextUnit {id: row.text_unit_id})
MERGE (e)-[:MENTIONED_IN]->(t)
"""

CYPHER_WIPE = "MATCH (n) DETACH DELETE n"


# ── Driver façade ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class Neo4jConfig:
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "graphrag-demo"
    database: str = "neo4j"


class GraphRagNeo4jLoader:
    """Reads MS GraphRAG parquet outputs and pushes them into Neo4j.

    Parameters
    ----------
    output_dir
        Directory containing the parquet files (typically
        ``work/graphrag/<ver>/output``).
    config
        Connection details. Defaults to local docker-compose defaults.
    """

    def __init__(self, output_dir: Path, config: Neo4jConfig | None = None) -> None:
        self.output_dir = Path(output_dir)
        self.config = config or Neo4jConfig()

    # -- public API -------------------------------------------------------

    def load(
        self,
        *,
        wipe: bool = False,
        load_text_units: bool = True,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> LoadStats:
        """Idempotent load: MERGEs nodes/edges, optional ``wipe=True`` first.

        Returns counters for the CLI summary line. Connection errors propagate
        as ``RuntimeError`` so the CLI can show a clean message.
        """
        self._assert_output_dir()
        driver = self._connect()
        try:
            with driver.session(database=self.config.database) as session:
                if wipe:
                    session.run(CYPHER_WIPE).consume()
                for stmt in CYPHER_CONSTRAINTS:
                    session.run(stmt).consume()

                stats = LoadStats()

                # Order matters: entities first (relationships + IN_COMMUNITY
                # match against entity titles/ids), then communities, then
                # community-internal edges, then text_units.
                entity_rows = self._read_parquet("entities").to_dict(orient="records")
                entity_payload = build_entity_payload(entity_rows)
                self._batched_run(session, CYPHER_UPSERT_ENTITY, entity_payload, batch_size)
                stats.entities = len(entity_payload)

                known_titles = {row["title"] for row in entity_payload}
                rel_rows = self._read_parquet("relationships").to_dict(orient="records")
                rel_payload, skipped = build_relationship_payload(rel_rows, known_titles)
                self._batched_run(session, CYPHER_UPSERT_RELATIONSHIP, rel_payload, batch_size)
                stats.relationships = len(rel_payload)
                stats.skipped_relationships_missing_endpoint = skipped

                community_rows = self._read_parquet("communities").to_dict(orient="records")
                report_rows = self._read_parquet("community_reports").to_dict(orient="records")
                comm_nodes, in_community, parent_edges = build_community_payload(
                    community_rows, report_rows
                )
                self._batched_run(session, CYPHER_UPSERT_COMMUNITY, comm_nodes, batch_size)
                self._batched_run(session, CYPHER_UPSERT_IN_COMMUNITY, in_community, batch_size)
                self._batched_run(session, CYPHER_UPSERT_PARENT_OF, parent_edges, batch_size)
                stats.communities = len(comm_nodes)
                stats.community_reports = len(report_rows)
                stats.in_community_edges = len(in_community)
                stats.parent_edges = len(parent_edges)

                if load_text_units:
                    try:
                        tu_rows = self._read_parquet("text_units").to_dict(orient="records")
                    except FileNotFoundError:
                        logger.info("text_units.parquet not found — skipping")
                    else:
                        tu_nodes, mentioned = build_text_unit_payload(tu_rows)
                        self._batched_run(session, CYPHER_UPSERT_TEXT_UNIT, tu_nodes, batch_size)
                        self._batched_run(
                            session, CYPHER_UPSERT_MENTIONED_IN, mentioned, batch_size
                        )
                        stats.text_units = len(tu_nodes)
                        stats.mentioned_in_edges = len(mentioned)

                return stats
        finally:
            driver.close()

    def wipe(self) -> None:
        """``DETACH DELETE`` everything in the configured database."""
        driver = self._connect()
        try:
            with driver.session(database=self.config.database) as session:
                session.run(CYPHER_WIPE).consume()
        finally:
            driver.close()

    # -- helpers ----------------------------------------------------------

    def _assert_output_dir(self) -> None:
        if not self.output_dir.exists():
            raise FileNotFoundError(
                f"GraphRAG output dir {self.output_dir} does not exist. "
                "Run `ke graphrag ingest <pdf>` first."
            )
        missing = [
            name
            for name in REQUIRED_PARQUETS
            if not (self.output_dir / f"{name}.parquet").exists()
        ]
        if missing:
            raise FileNotFoundError(
                f"Required parquet(s) missing from {self.output_dir}: {missing}"
            )

    def _read_parquet(self, name: str):
        import pandas as pd

        path = self.output_dir / f"{name}.parquet"
        if not path.exists():
            raise FileNotFoundError(path)
        return pd.read_parquet(path)

    def _connect(self):
        try:
            from neo4j import GraphDatabase
            from neo4j.exceptions import ServiceUnavailable
        except ImportError as exc:
            raise RuntimeError(
                "Neo4j driver not installed. Run `pip install '.[neo4j]'` "
                "(or `uv sync --extra neo4j`)."
            ) from exc
        try:
            driver = GraphDatabase.driver(
                self.config.uri, auth=(self.config.user, self.config.password)
            )
            driver.verify_connectivity()
        except ServiceUnavailable as exc:
            raise RuntimeError(
                f"Cannot reach Neo4j at {self.config.uri}: {exc}. "
                "Did you run `ke neo4j up`?"
            ) from exc
        return driver

    def _batched_run(
        self,
        session,
        cypher: str,
        payload: list[dict[str, Any]],
        batch_size: int,
    ) -> None:
        if not payload:
            return
        for batch in _chunks(payload, batch_size):
            session.run(cypher, rows=batch).consume()


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "GraphRagNeo4jLoader",
    "LoadStats",
    "Neo4jConfig",
    "build_community_payload",
    "build_entity_payload",
    "build_relationship_payload",
    "build_text_unit_payload",
]
