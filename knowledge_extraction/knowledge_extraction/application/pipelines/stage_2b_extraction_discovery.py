"""Discovery extraction pipeline: free-form, reasoning-model-driven."""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field

import orjson

from knowledge_extraction.application.ports import LLMPort
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.domain import Chunk
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import RelationalRepository


@dataclass(slots=True)
class DiscoveryFindings:
    entity_type_counter: Counter[str] = field(default_factory=Counter)
    relationship_type_counter: Counter[str] = field(default_factory=Counter)
    entity_examples: dict[str, list[str]] = field(default_factory=dict)
    relationship_examples: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    type_hierarchy: list[tuple[str, str]] = field(default_factory=list)
    emerging_concepts: list[str] = field(default_factory=list)
    raw_entities: list[dict] = field(default_factory=list)


class DiscoveryExtractionPipeline:
    PROMPT_NAME = "discovery_extraction"
    PROMPT_VERSION = "v1"

    def __init__(self, *, llm: LLMPort, prompts: PromptRegistry,
                 repo: RelationalRepository, model: str) -> None:
        self._llm = llm
        self._prompts = prompts
        self._repo = repo
        self._model = model
        self.findings = DiscoveryFindings()

    async def run(self, doc_title: str, chunks: list[Chunk]) -> DiscoveryFindings:
        import logging

        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
        )

        from knowledge_extraction.infrastructure.telemetry.observability import bound, wide_event

        total = len(chunks)
        with bound(pipeline="discovery", model=self._model), Progress(
            TextColumn("[bold blue]extracting chunks (discovery)"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("discovery", total=total)
            for chunk in chunks:
                with wide_event("extract.chunk",
                                level=logging.DEBUG,
                                chunk_id=chunk.id,
                                page_start=chunk.page_start,
                                page_end=chunk.page_end,
                                char_count=len(chunk.text)) as ev:
                    before_e = sum(self.findings.entity_type_counter.values())
                    before_r = sum(self.findings.relationship_type_counter.values())
                    await self._discover_one(doc_title, chunk)
                    ev["new_entities"] = sum(self.findings.entity_type_counter.values()) - before_e
                    ev["new_relationships"] = sum(self.findings.relationship_type_counter.values()) - before_r
                progress.advance(task)
        return self.findings

    async def _discover_one(self, doc_title: str, chunk: Chunk) -> None:
        prompt = self._prompts.render(
            self.PROMPT_NAME, self.PROMPT_VERSION,
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
            text = cached
        else:
            resp = await self._llm.complete_json(
                model=self._model, system=prompt.system, user=prompt.user, max_tokens=3072,
            )
            text = resp.text
            self._repo.log_prompt_call(
                prompt_version=self.PROMPT_VERSION, model=self._model, input_hash=input_hash,
                response_text=text, input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens, latency_ms=resp.latency_ms,
            )

        try:
            data = orjson.loads(text)
        except Exception:
            return

        for e in data.get("entities", []) or []:
            if not isinstance(e, dict):
                continue
            t = str(e.get("candidate_type", "Unknown"))
            name = str(e.get("name", "")).strip()
            if not name:
                continue
            self.findings.entity_type_counter[t] += 1
            self.findings.entity_examples.setdefault(t, []).append(name)
            self.findings.raw_entities.append({"name": name, "type": t,
                                               "aliases": e.get("aliases", [])})

        for r in data.get("relationships", []) or []:
            if not isinstance(r, dict):
                continue
            rt = str(r.get("candidate_type", "RELATED_TO"))
            src = str(r.get("source", "")).strip()
            tgt = str(r.get("target", "")).strip()
            if not src or not tgt:
                continue
            self.findings.relationship_type_counter[rt] += 1
            self.findings.relationship_examples.setdefault(rt, []).append((src, tgt))

        for h in data.get("type_hierarchy", []) or []:
            if isinstance(h, dict) and h.get("parent") and h.get("child"):
                self.findings.type_hierarchy.append((str(h["parent"]), str(h["child"])))

        for c in data.get("emerging_concepts", []) or []:
            if isinstance(c, dict) and c.get("concept"):
                self.findings.emerging_concepts.append(str(c["concept"]))
