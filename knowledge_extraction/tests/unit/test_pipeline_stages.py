from __future__ import annotations

import pytest

from knowledge_extraction.application.pipelines.stages import (
    PIPELINE_DISCOVERY,
    PIPELINE_GOVERNED,
    Stage,
    parse_stage,
)


def test_governed_pipeline_includes_graph_stage() -> None:
    assert PIPELINE_GOVERNED == (Stage.RENDER, Stage.FIGURES, Stage.EXTRACT, Stage.GRAPH)


def test_discovery_pipeline_stops_after_extract() -> None:
    assert PIPELINE_DISCOVERY == (Stage.RENDER, Stage.FIGURES, Stage.EXTRACT)
    assert Stage.GRAPH not in PIPELINE_DISCOVERY


def test_stage_values_are_stable_disk_paths() -> None:
    # Checkpoint folders are named after these values; treat them as a contract.
    assert Stage.RENDER.value == "render"
    assert Stage.FIGURES.value == "figures"
    assert Stage.EXTRACT.value == "extract"
    assert Stage.GRAPH.value == "graph"


def test_parse_stage_accepts_canonical_strings() -> None:
    assert parse_stage("extract") is Stage.EXTRACT
    assert parse_stage("graph") is Stage.GRAPH


def test_parse_stage_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        parse_stage("nope")
