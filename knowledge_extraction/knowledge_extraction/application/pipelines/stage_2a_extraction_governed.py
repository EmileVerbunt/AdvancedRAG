"""Governed extraction pipeline: schema-guided extraction + validation + canonicalization."""
from __future__ import annotations

import asyncio
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
    Figure,
    OntologySchema,
    RefinementSuggestion,
    Relationship,
    Table,
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
        concurrency: int = 1,
    ) -> None:
        self._llm = llm
        self._prompts = prompts
        self._validator = validator
        self._canonicalizer = canonicalizer
        self._drift = drift
        self._repo = repo
        self._schema = schema
        self._model = model
        self._concurrency = max(1, concurrency)
        self.stats = GovernedExtractionStats()

    async def run(
        self,
        doc_title: str,
        chunks: list[Chunk],
        tables: list[Table] | None = None,
        figures: list[Figure] | None = None,
    ) -> list[ExtractionResult]:
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
        )

        from knowledge_extraction.infrastructure.telemetry.observability import bound, wide_event

        results: list[ExtractionResult | None] = [None] * len(chunks)
        total = len(chunks)
        log.info("extract.governed.start chunks=%d model=%s concurrency=%d",
                  total, self._model, self._concurrency)
        sem = asyncio.Semaphore(self._concurrency)
        tables_by_id = {table.id: table for table in tables or []}
        figures_by_id = {figure.id: figure for figure in figures or []}

        with bound(pipeline="governed", model=self._model), Progress(
            TextColumn("[bold blue]extracting chunks (governed)"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("extract", total=total)

            async def _process(idx: int, chunk: Chunk) -> None:
                # Resume fast-path: nothing to extract, hydrate from DB.
                if not self._repo.needs_chunk_extraction(chunk.id):
                    results[idx] = self._repo.load_extraction_for_chunk(chunk.id)
                    progress.advance(task)
                    return
                async with sem:
                    try:
                        with wide_event("extract.chunk",
                                        level=logging.DEBUG,
                                        chunk_id=chunk.id,
                                        page_start=chunk.page_start,
                                        page_end=chunk.page_end,
                                        char_count=len(chunk.text)) as ev:
                            result = await self._extract_one(doc_title, chunk, tables_by_id, figures_by_id)
                            self._repo.save_extraction(chunk, result)
                            results[idx] = result
                            self.stats.chunks_processed += 1
                            ev["entities"] = len(result.entities)
                            ev["relationships"] = len(result.relationships)
                            ev["claims"] = len(result.claims)
                            ev["input_tokens"] = result.input_tokens
                            ev["output_tokens"] = result.output_tokens
                            ev["cached"] = result.input_tokens == 0 and result.output_tokens == 0
                    except Exception as exc:  # tolerate per-chunk failures, continue pipeline
                        log.error("extract.chunk.failed chunk=%s error=%s", chunk.id, exc)
                    finally:
                        progress.advance(task)

            await asyncio.gather(*(_process(i, c) for i, c in enumerate(chunks)))

        log.info(
            "extract.governed.complete chunks=%d entities=%d relationships=%d",
            self.stats.chunks_processed,
            self.stats.entities_accepted,
            self.stats.relationships_accepted,
        )
        # Drop slots that failed (None) — graph build only sees valid results.
        return [r for r in results if r is not None]

    async def _extract_one(
        self,
        doc_title: str,
        chunk: Chunk,
        tables_by_id: dict[str, Table],
        figures_by_id: dict[str, Figure],
    ) -> ExtractionResult:
        table_context = _table_context(chunk, tables_by_id)
        figure_context = _figure_context(chunk, figures_by_id)
        prompt = self._prompts.render(
            self.PROMPT_NAME, self.PROMPT_VERSION,
            entity_types=self._schema.entity_types,
            relationship_types=self._schema.relationship_types,
            doc_title=doc_title,
            section_path=chunk.section_id or "",
            pages=f"{chunk.page_start}-{chunk.page_end}",
            chunk_text=chunk.text,
            table_context=table_context,
            table_refs=chunk.table_refs,
            figure_context=figure_context,
            figure_refs=chunk.figure_refs,
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
            claims=_to_claims(data, chunk, chunk.figure_refs, chunk.table_refs),
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


def _to_claims(
    data: dict[str, object],
    chunk: Chunk,
    chunk_figure_refs: list[str] | None = None,
    chunk_table_refs: list[str] | None = None,
) -> list[Claim]:
    out: list[Claim] = []
    allowed_figure_refs = set(chunk_figure_refs or [])
    allowed_table_refs = set(chunk_table_refs or [])
    for raw in data.get("claims", []) or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        cid = hashlib.sha1(f"{chunk.id}:{text[:120]}".encode()).hexdigest()[:16]
        supporting_figure_id = _optional_str(raw.get("supporting_figure_id"))
        if supporting_figure_id and supporting_figure_id not in allowed_figure_refs:
            supporting_figure_id = None
        supporting_table_id = _optional_str(raw.get("supporting_table_id"))
        if supporting_table_id and supporting_table_id not in allowed_table_refs:
            supporting_table_id = None
        out.append(Claim(
            id=cid, text=text,
            confidence=float(raw.get("confidence", 0.5)),
            evidence=_evidence(chunk, str(raw.get("evidence_span", ""))),
            supporting_figure_id=supporting_figure_id,
            supporting_table_id=supporting_table_id,
        ))
    return out


def _table_context(chunk: Chunk, tables_by_id: dict[str, Table]) -> str:
    if not chunk.table_refs:
        return ""
    parts: list[str] = []
    for table_id in chunk.table_refs:
        table = tables_by_id.get(table_id)
        if table is None:
            parts.append(f"- {table_id}")
            continue
        pages = f"{table.page}-{table.page_end}" if table.page_end and table.page_end != table.page else str(table.page)
        caption = f" caption={table.caption}" if table.caption else ""
        parts.append(f"- {table.id} pages={pages}{caption}\n{table.markdown}")
    return "\n".join(parts)


def _figure_context(chunk: Chunk, figures_by_id: dict[str, Figure]) -> str:
    if not chunk.figure_refs:
        return ""
    parts: list[str] = []
    for figure_id in chunk.figure_refs:
        figure = figures_by_id.get(figure_id)
        if figure is None:
            parts.append(f"- {figure_id}")
            continue
        caption = f" caption={figure.caption}" if figure.caption else ""
        parts.append(f"- {figure.id} page={figure.page}{caption}")
    return "\n".join(parts)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    parsed = str(value).strip()
    return parsed or None
