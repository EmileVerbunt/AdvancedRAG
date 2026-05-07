"""Governed extraction pipeline: schema-guided extraction + validation + canonicalization."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

import orjson

from knowledge_extraction.application.ports import LLMPort
from knowledge_extraction.application.services.canonicalization_service import CanonicalizationService
from knowledge_extraction.application.services.drift_detector import DriftDetector
from knowledge_extraction.application.services.ontology_validator import OntologyValidator
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.domain import (
    UNKNOWN_TYPE,
    Chunk,
    Claim,
    Entity,
    Evidence,
    ExtractionResult,
    OntologySchema,
    RefinementSuggestion,
    Relationship,
)
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import RelationalRepository

log = logging.getLogger(__name__)


@dataclass(slots=True)
class GovernedExtractionStats:
    chunks_processed: int = 0
    entities_accepted: int = 0
    entities_unknown: int = 0
    relationships_accepted: int = 0
    relationships_unknown: int = 0
    violations_prevented: int = 0
    canonical_reused: int = 0
    refinement_suggestions: list[str] = field(default_factory=list)


class GovernedExtractionPipeline:
    PROMPT_NAME = "governed_extraction"
    PROMPT_VERSION = "v1"

    def __init__(
        self,
        *,
        llm: LLMPort,
        prompts: PromptRegistry,
        validator: OntologyValidator,
        canonicalizer: CanonicalizationService,
        drift: DriftDetector,
        repo: RelationalRepository,
        schema: OntologySchema,
        model: str,
    ) -> None:
        self._llm = llm
        self._prompts = prompts
        self._validator = validator
        self._canonicalizer = canonicalizer
        self._drift = drift
        self._repo = repo
        self._schema = schema
        self._model = model
        self.stats = GovernedExtractionStats()

    async def run(self, doc_title: str, chunks: list[Chunk]) -> list[ExtractionResult]:
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
        )

        from knowledge_extraction.infrastructure.telemetry.observability import bound, wide_event

        results: list[ExtractionResult] = []
        total = len(chunks)
        log.info("extract.governed.start chunks=%d model=%s", total, self._model)
        with bound(pipeline="governed", model=self._model), Progress(
            TextColumn("[bold blue]extracting chunks (governed)"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("extract", total=total)
            for chunk in chunks:
                with wide_event("extract.chunk",
                                level=logging.DEBUG,
                                chunk_id=chunk.id,
                                page_start=chunk.page_start,
                                page_end=chunk.page_end,
                                char_count=len(chunk.text)) as ev:
                    result = await self._extract_one(doc_title, chunk)
                    self._repo.save_extraction(chunk, result)
                    results.append(result)
                    self.stats.chunks_processed += 1
                    ev["entities"] = len(result.entities)
                    ev["relationships"] = len(result.relationships)
                    ev["claims"] = len(result.claims)
                    ev["input_tokens"] = result.input_tokens
                    ev["output_tokens"] = result.output_tokens
                    ev["cached"] = result.input_tokens == 0 and result.output_tokens == 0
                progress.advance(task)
        log.info(
            "extract.governed.complete chunks=%d entities=%d relationships=%d",
            self.stats.chunks_processed,
            self.stats.entities_accepted,
            self.stats.relationships_accepted,
        )
        return results

    async def _extract_one(self, doc_title: str, chunk: Chunk) -> ExtractionResult:
        prompt = self._prompts.render(
            self.PROMPT_NAME, self.PROMPT_VERSION,
            entity_types=self._schema.entity_types,
            relationship_types=self._schema.relationship_types,
            doc_title=doc_title,
            section_path=chunk.section_id or "",
            pages=f"{chunk.page_start}-{chunk.page_end}",
            chunk_text=chunk.text,
        )
        input_hash = hashlib.sha1((prompt.system + prompt.user).encode("utf-8")).hexdigest()
        cached = self._repo.cached_response(
            prompt_version=self.PROMPT_VERSION, model=self._model, input_hash=input_hash
        )
        if cached is not None:
            text, usage = cached, (0, 0, 0)
        else:
            resp = await self._llm.complete_json(
                model=self._model, system=prompt.system, user=prompt.user, max_tokens=2048,
            )
            text = resp.text
            usage = (resp.input_tokens, resp.output_tokens, resp.latency_ms)
            self._repo.log_prompt_call(
                prompt_version=self.PROMPT_VERSION, model=self._model, input_hash=input_hash,
                response_text=text, input_tokens=usage[0], output_tokens=usage[1], latency_ms=usage[2],
            )

        data = _safe_json(text)
        result = ExtractionResult(
            chunk_id=chunk.id,
            entities=_to_entities(data, chunk),
            relationships=_to_relationships(data, chunk),
            claims=_to_claims(data, chunk),
            refinement_suggestions=[RefinementSuggestion(**r) for r in data.get("refinement_suggestions", [])],
            raw_response=text,
            prompt_version=self.PROMPT_VERSION,
            model=self._model,
            input_tokens=usage[0],
            output_tokens=usage[1],
            latency_ms=usage[2],
        )

        # Canonicalize entities, then validate.
        for e in result.entities:
            before_id = e.id
            self._canonicalizer.canonicalize(e)
            if e.id != before_id:
                self.stats.canonical_reused += 1

        report = self._validator.validate(result)
        self.stats.entities_accepted += len(report.accepted_entities)
        self.stats.entities_unknown += len(report.unknown_entities)
        self.stats.relationships_accepted += len(report.accepted_relationships)
        self.stats.relationships_unknown += len(report.unknown_relationships)
        self.stats.violations_prevented += len(report.edge_constraint_violations) + len(report.off_schema_relationships)
        self.stats.refinement_suggestions.extend(s.name for s in result.refinement_suggestions)

        self._drift.observe(report, [s.name for s in result.refinement_suggestions])
        return result


def _safe_json(text: str) -> dict[str, object]:
    try:
        return orjson.loads(text)
    except Exception:
        return {}


def _evidence(chunk: Chunk, span: str) -> list[Evidence]:
    return [Evidence(chunk_id=chunk.id, page=chunk.page_start, span=span[:480])]


def _to_entities(data: dict[str, object], chunk: Chunk) -> list[Entity]:
    out: list[Entity] = []
    for raw in data.get("entities", []) or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        etype = str(raw.get("type") or UNKNOWN_TYPE)
        out.append(Entity(
            id=hashlib.sha1(f"{etype}:{name.lower()}".encode()).hexdigest()[:16],
            name=name,
            type=etype,
            aliases=list(raw.get("aliases", []) or []),
            confidence=float(raw.get("confidence", 0.5)),
            evidence=_evidence(chunk, str(raw.get("evidence_span", ""))),
        ))
    return out


def _to_relationships(data: dict[str, object], chunk: Chunk) -> list[Relationship]:
    out: list[Relationship] = []
    for raw in data.get("relationships", []) or []:
        if not isinstance(raw, dict):
            continue
        src = str(raw.get("source", "")).strip()
        tgt = str(raw.get("target", "")).strip()
        if not src or not tgt:
            continue
        rtype = str(raw.get("type") or UNKNOWN_TYPE)
        sid = hashlib.sha1(src.lower().encode()).hexdigest()[:16]
        tid = hashlib.sha1(tgt.lower().encode()).hexdigest()[:16]
        rid = hashlib.sha1(f"{sid}:{rtype}:{tid}:{chunk.id}".encode()).hexdigest()[:16]
        out.append(Relationship(
            id=rid, source_id=sid, target_id=tid, type=rtype,
            confidence=float(raw.get("confidence", 0.5)),
            evidence=_evidence(chunk, str(raw.get("evidence_span", ""))),
        ))
    return out


def _to_claims(data: dict[str, object], chunk: Chunk) -> list[Claim]:
    out: list[Claim] = []
    for raw in data.get("claims", []) or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        cid = hashlib.sha1(f"{chunk.id}:{text[:120]}".encode()).hexdigest()[:16]
        out.append(Claim(
            id=cid, text=text,
            confidence=float(raw.get("confidence", 0.5)),
            evidence=_evidence(chunk, str(raw.get("evidence_span", ""))),
        ))
    return out
