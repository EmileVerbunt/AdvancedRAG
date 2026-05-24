"""Pure-Python tests for the Neo4j loader's payload builders.

These tests deliberately avoid a live Neo4j connection — they exercise the
shape, normalisation, and edge-skipping logic that decides what we send to
the driver. The integration with Neo4j itself is covered by manual demo runs
(``ke neo4j up`` + ``ke neo4j load``).
"""
from __future__ import annotations

import numpy as np

from knowledge_extraction.infrastructure.neo4j.parquet_loader import (
    build_community_payload,
    build_entity_payload,
    build_relationship_payload,
    build_text_unit_payload,
)


def test_entity_payload_skips_rows_with_missing_title() -> None:
    rows = [
        {"id": "u1", "human_readable_id": 0, "title": "CLAUDE", "type": "PERSON",
         "description": "An AI model", "frequency": 12, "degree": 80},
        {"id": "u2", "human_readable_id": 1, "title": None, "type": "PERSON"},
        {"id": "u3", "human_readable_id": 2, "title": "", "type": "PERSON"},
    ]
    payload = build_entity_payload(rows)
    titles = [p["title"] for p in payload]
    assert titles == ["CLAUDE"]


def test_entity_payload_coerces_numpy_scalars() -> None:
    rows = [{
        "id": "u1", "human_readable_id": np.int64(7), "title": "GPT-4",
        "type": "TECHNOLOGY",
        "description": "x" * 5000,  # gets truncated to 4000
        "frequency": np.int64(3), "degree": np.int64(42),
    }]
    out = build_entity_payload(rows)[0]
    assert out["frequency"] == 3
    assert isinstance(out["frequency"], int)
    assert out["degree"] == 42
    assert len(out["description"]) == 4000


def test_entity_payload_defaults_type_to_unknown() -> None:
    rows = [{"id": "u1", "title": "DANY", "type": None}]
    assert build_entity_payload(rows)[0]["type"] == "UNKNOWN"


def test_relationship_payload_skips_unknown_endpoints() -> None:
    rows = [
        {"source": "A", "target": "B", "weight": 1.0, "description": "ok"},
        {"source": "A", "target": "Z", "weight": 2.0, "description": "dangling"},
        {"source": None, "target": "B"},
        {"source": "A", "target": ""},
    ]
    payload, skipped = build_relationship_payload(rows, known_titles={"A", "B"})
    assert len(payload) == 1
    assert payload[0]["source"] == "A" and payload[0]["target"] == "B"
    assert skipped == 3


def test_relationship_payload_keeps_all_when_known_titles_is_none() -> None:
    rows = [
        {"source": "A", "target": "B", "weight": 1.0, "description": "x"},
        {"source": "X", "target": "Y", "weight": 3.0, "description": "y"},
    ]
    payload, skipped = build_relationship_payload(rows, known_titles=None)
    assert len(payload) == 2
    assert skipped == 0


def test_relationship_payload_defaults_weight_to_one() -> None:
    rows = [{"source": "A", "target": "B", "weight": None, "description": None}]
    out, _ = build_relationship_payload(rows, known_titles={"A", "B"})
    assert out[0]["weight"] == 1.0
    assert out[0]["description"] == ""


def test_community_payload_builds_nodes_in_community_and_parent_edges() -> None:
    communities = [
        {"id": "c-root", "community": 0, "level": 0, "parent": -1,
         "title": "Root", "size": 100,
         "entity_ids": ["u1", "u2", "u3"]},
        {"id": "c-child", "community": 7, "level": 1, "parent": 0,
         "title": "Child", "size": 12,
         "entity_ids": np.array(["u4", "u5"], dtype=object)},
    ]
    reports = [
        {"community": 0, "summary": "root summary", "rating": 8.5,
         "rank": 100.0, "full_content": "details"},
        {"community": 7, "summary": "child summary", "rating": 6.0,
         "rank": 10.0, "full_content": "more details"},
    ]
    nodes, in_community, parent_edges = build_community_payload(communities, reports)

    assert {n["community"] for n in nodes} == {0, 7}
    root = next(n for n in nodes if n["community"] == 0)
    child = next(n for n in nodes if n["community"] == 7)
    assert root["parent_id"] is None  # parent == -1 means no parent
    assert child["parent_id"] == 0
    assert root["summary"] == "root summary"
    assert child["rating"] == 6.0

    assert sorted((r["entity_id"], r["community"]) for r in in_community) == [
        ("u1", 0), ("u2", 0), ("u3", 0), ("u4", 7), ("u5", 7),
    ]
    assert parent_edges == [{"child": 7, "parent": 0}]


def test_community_payload_handles_missing_report() -> None:
    communities = [{"community": 5, "level": 2, "parent": -1, "entity_ids": []}]
    nodes, _, _ = build_community_payload(communities, report_rows=None)
    assert nodes[0]["summary"] == ""
    assert nodes[0]["rating"] == 0.0
    assert nodes[0]["title"] == "Community 5"  # default when title missing


def test_community_payload_truncates_long_summary() -> None:
    communities = [{"community": 1, "level": 0, "parent": -1, "entity_ids": []}]
    reports = [{"community": 1, "summary": "x" * 10000, "full_content": "y" * 20000}]
    nodes, _, _ = build_community_payload(communities, reports)
    assert len(nodes[0]["summary"]) == 8000
    assert len(nodes[0]["full_content"]) == 16000


def test_text_unit_payload_builds_nodes_and_mentioned_edges() -> None:
    rows = [
        {"id": "t1", "human_readable_id": 0, "n_tokens": 1200,
         "entity_ids": np.array(["u1", "u2"], dtype=object)},
        {"id": "t2", "human_readable_id": 1, "n_tokens": 800,
         "entity_ids": ["u2", "u3"]},
        {"id": None, "n_tokens": 0, "entity_ids": []},  # skipped
    ]
    nodes, edges = build_text_unit_payload(rows)
    assert {n["id"] for n in nodes} == {"t1", "t2"}
    assert sorted((e["entity_id"], e["text_unit_id"]) for e in edges) == [
        ("u1", "t1"), ("u2", "t1"), ("u2", "t2"), ("u3", "t2"),
    ]
