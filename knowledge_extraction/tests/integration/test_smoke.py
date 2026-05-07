"""End-to-end smoke: chunk -> stub LLM extract -> validator -> graph build + export."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import orjson

from knowledge_extraction.application.pipelines.stage_1_chunking import SemanticChunker
from knowledge_extraction.application.pipelines.stage_2a_extraction_governed import (
    GovernedExtractionPipeline,
)
from knowledge_extraction.application.pipelines.stage_5_graph import GraphBuildPipeline
from knowledge_extraction.application.services.canonicalization_service import CanonicalizationService
from knowledge_extraction.application.services.drift_detector import DriftDetector
from knowledge_extraction.application.services.ontology_service import OntologyService
from knowledge_extraction.application.services.ontology_validator import OntologyValidator
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.config.settings import get_settings
from knowledge_extraction.domain import Document
from knowledge_extraction.infrastructure.persistence.graph.networkx_store import NetworkXGraphStore
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import (
    GovernanceRepository,
    RelationalRepository,
    make_engine,
    make_session_factory,
)


@dataclass
class _StubResp:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 1


class _StubLLM:
    async def complete_json(self, *, model, system, user, max_tokens=4096, temperature=0.0):
        payload = {
            "entities": [
                {"name": "GPT-4", "type": "Model", "aliases": ["gpt4"], "evidence_span": "GPT-4", "confidence": 0.9},
                {"name": "OpenAI", "type": "Organization", "aliases": [], "evidence_span": "OpenAI", "confidence": 0.95},
            ],
            "relationships": [
                {"source": "GPT-4", "target": "OpenAI", "type": "RELEASED_BY",
                 "evidence_span": "GPT-4 released by OpenAI", "confidence": 0.9},
            ],
            "claims": [
                {"text": "GPT-4 is released by OpenAI.", "evidence_span": "GPT-4 released by OpenAI", "confidence": 0.9},
            ],
            "refinement_suggestions": [],
        }
        return _StubResp(text=orjson.dumps(payload).decode())


def test_smoke_end_to_end(tmp_path) -> None:
    settings = get_settings()
    settings.sqlite_path = tmp_path / "ke.db"
    settings.graph_storage_path = tmp_path / "graph"
    settings.checkpoint_path = tmp_path / "ck"
    settings.artifact_path = tmp_path / "art"
    settings.ensure_dirs()

    engine = make_engine(settings.sqlite_path)
    sf = make_session_factory(engine)
    relational = RelationalRepository(sf)
    governance = GovernanceRepository(sf)
    onto_service = OntologyService(governance, settings.ontology_yaml_path)
    version, schema = onto_service.active()
    assert version.version == "1.0.0"

    md = "# Models\n\nGPT-4 released by OpenAI.\n\n## Benchmarks\n\nGPT-4 outperforms GPT-3 on MMLU.\n"
    document = Document(
        id="docsmoke", title="smoke", source_path=tmp_path / "x.pdf", pages=[], sections=[],
    )
    relational.save_document(document)

    chunker = SemanticChunker(target_chars=80, max_chars=200)
    _sections, chunks = chunker.chunk(document, md)
    assert chunks
    relational.save_chunks(chunks)

    prompts = PromptRegistry(settings.prompts_dir)
    validator = OntologyValidator(schema)
    canonicalizer = CanonicalizationService(governance)
    drift = DriftDetector(governance, version.version)
    pipeline = GovernedExtractionPipeline(
        llm=_StubLLM(), prompts=prompts, validator=validator, canonicalizer=canonicalizer,
        drift=drift, repo=relational, schema=schema, model="stub",
    )
    results = asyncio.run(pipeline.run(document.title, chunks))
    assert results
    assert pipeline.stats.entities_accepted >= 2
    assert pipeline.stats.relationships_accepted >= 1

    graph = NetworkXGraphStore()
    GraphBuildPipeline(graph).build(results)
    out = settings.graph_storage_path / "smoke.graphml"
    graph.export_graphml(out)
    assert out.exists() and out.stat().st_size > 0
    assert graph.stats()["nodes"] >= 2
