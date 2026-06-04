"""Agentic retrieval agent — multi-step retrieval with planning, critique, and synthesis.

Agentic search is a bounded research loop that works on the same thin knowledge
substrate as the other backends (SQLite chunks, claims, entities, relationships,
tables, figures) with no pre-built graph index required:

  question
    → plan subquestions
    → retrieve evidence for each subquestion (BM25 + RRF fusion)
    → critique evidence quality
    → optionally retrieve follow-up queries (up to max_rounds)
    → synthesize final grounded answer with citations

Three LLM calls in the common path (planner, critic, synthesizer), plus one
additional critic call per follow-up round.  All calls go through LLMPort so
the same Azure Foundry / Azure OpenAI adapter is reused.

This is a document-corpus research agent, not a general autonomous agent.
All loops are bounded; there is no filesystem or network access beyond the
approved retrieval tools (MiniGraphRagAgent.ask_multi).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import orjson

from knowledge_extraction.application.ports import LLMPort
from knowledge_extraction.application.services.graphrag_agent import MiniGraphRagAgent, RetrievalHit
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.config.settings import Settings
from knowledge_extraction.infrastructure.telemetry.observability import wide_event

log = logging.getLogger(__name__)

PLAN_PROMPT = "agentic_plan"
CRITIC_PROMPT = "agentic_critic"
SYNTH_PROMPT = "agentic_synthesis"
PROMPT_VERSION = "v1"


@dataclass(slots=True)
class AgenticSearchOptions:
    max_rounds: int = 2
    max_subquestions: int = 5
    top_k_per_query: int = 8
    max_total_evidence_items: int = 30


@dataclass(slots=True)
class SearchPlan:
    original_question: str
    subquestions: list[str]


@dataclass(slots=True)
class EvidenceItem:
    kind: str  # "chunk", "claim", "table", "figure", "entity", "relationship"
    id: str
    text: str
    score: float
    citation_label: str  # e.g. "[C1]", "[T1]", "[F1]"
    page_start: int | None = None
    page_end: int | None = None
    document_id: str | None = None


@dataclass(slots=True)
class EvidenceCritique:
    sufficient: bool
    follow_up_queries: list[str]
    confidence: float
    missing_information: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgenticTokenUsage:
    plan_input: int = 0
    plan_output: int = 0
    critic_input: int = 0
    critic_output: int = 0
    synth_input: int = 0
    synth_output: int = 0

    @property
    def total(self) -> int:
        return (
            self.plan_input
            + self.plan_output
            + self.critic_input
            + self.critic_output
            + self.synth_input
            + self.synth_output
        )


@dataclass(slots=True)
class AgenticSearchAnswer:
    """Final answer plus all agentic artefacts (plan, evidence, critique).

    Callers (eval harness, UI, CLI) can audit the reasoning trace and cite
    evidence via the ``citation_label`` on each ``EvidenceItem``.
    """

    question: str
    answer: str
    plan: SearchPlan
    evidence: list[EvidenceItem]
    critique: EvidenceCritique
    rounds: int
    tokens: AgenticTokenUsage
    duration_ms: int
    model: str

    def to_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "answer": self.answer,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "rounds": self.rounds,
            "tokens": asdict(self.tokens),
            "plan": asdict(self.plan),
            "critique": asdict(self.critique),
            "evidence": [asdict(e) for e in self.evidence],
        }


class AgenticSearchAgent:
    """Bounded agentic retrieval: plan → retrieve → critique → [loop] → synthesize.

    Uses existing BM25 retrieval infrastructure (MiniGraphRagAgent.ask_multi) for
    evidence gathering, so no vector store or pre-built graph index is required.
    Depends only on a populated ``chunks`` table (same prerequisite as lazy mode).
    """

    def __init__(
        self,
        settings: Settings,
        mini_agent: MiniGraphRagAgent,
        llm: LLMPort,
        prompts: PromptRegistry,
        *,
        planner_model: str | None = None,
        critic_model: str | None = None,
        synthesis_model: str | None = None,
    ) -> None:
        self._settings = settings
        self._mini = mini_agent
        self._llm = llm
        self._prompts = prompts
        # Model fallback chain: explicit override → reasoning model → extraction model
        extraction = settings.azure_openai_extraction_model or "gpt-4.1-mini"
        reasoning = settings.azure_openai_reasoning_model or extraction
        self._planner_model = planner_model or reasoning
        self._critic_model = critic_model or reasoning
        self._synthesis_model = synthesis_model or extraction

    # ---------------------------------------------------------------------- public API

    def ask(
        self,
        question: str,
        *,
        options: AgenticSearchOptions | None = None,
    ) -> AgenticSearchAnswer:
        """Sync wrapper around ask_async for CLI and Streamlit callers."""
        return asyncio.run(self.ask_async(question, options=options))

    async def ask_async(
        self,
        question: str,
        *,
        options: AgenticSearchOptions | None = None,
    ) -> AgenticSearchAnswer:
        opts = options or AgenticSearchOptions()
        t0 = time.perf_counter()

        with wide_event(
            "agentic.ask",
            question=question[:160],
            max_rounds=opts.max_rounds,
            model=self._planner_model,
        ) as ev:
            # Step 1: Plan
            plan, plan_in, plan_out = await self._plan(question, opts)
            ev.update(subquestion_count=len(plan.subquestions))

            # Step 2: Initial retrieval over all subquestions
            evidence = self._retrieve_evidence(plan.subquestions, opts)
            ev.update(initial_evidence=len(evidence))

            # Step 3: Critique loop (bounded by max_rounds).
            # The loop always runs at least once; this initial value just keeps
            # ``critique`` definitely-bound for the type checker.
            critique = EvidenceCritique(sufficient=True, follow_up_queries=[], confidence=1.0)
            critic_in_total = 0
            critic_out_total = 0
            actual_rounds = 0

            for round_num in range(1, max(1, opts.max_rounds) + 1):
                actual_rounds = round_num
                critique, c_in, c_out = await self._critique(question, plan, evidence, opts)
                critic_in_total += c_in
                critic_out_total += c_out
                ev.update(
                    round=round_num,
                    critic_confidence=critique.confidence,
                    evidence_sufficient=critique.sufficient,
                )
                if critique.sufficient or round_num >= opts.max_rounds:
                    break
                # Follow-up retrieval with critic-suggested queries
                if critique.follow_up_queries:
                    more = self._retrieve_evidence(
                        critique.follow_up_queries[: opts.max_subquestions], opts
                    )
                    evidence = _merge_evidence(
                        evidence, more, max_items=opts.max_total_evidence_items
                    )
                    ev.update(evidence_after_followup=len(evidence))

            # Step 4: Synthesize
            answer, synth_in, synth_out = await self._synthesize(
                question, plan, evidence, critique
            )

            tokens = AgenticTokenUsage(
                plan_input=plan_in,
                plan_output=plan_out,
                critic_input=critic_in_total,
                critic_output=critic_out_total,
                synth_input=synth_in,
                synth_output=synth_out,
            )
            ev.update(total_tokens=tokens.total, rounds=actual_rounds)

        return AgenticSearchAnswer(
            question=question,
            answer=answer,
            plan=plan,
            evidence=evidence,
            critique=critique,
            rounds=actual_rounds,
            tokens=tokens,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            model=self._planner_model,
        )

    # ---------------------------------------------------------------------- steps

    async def _plan(
        self,
        question: str,
        opts: AgenticSearchOptions,
    ) -> tuple[SearchPlan, int, int]:
        prompt = self._prompts.render(
            PLAN_PROMPT,
            PROMPT_VERSION,
            question=question,
            max_subquestions=opts.max_subquestions,
        )
        with wide_event("agentic.plan", model=self._planner_model, question=question[:160]) as ev:
            resp = await self._llm.complete_json(
                model=self._planner_model,
                system=prompt.system,
                user=prompt.user,
                max_tokens=1024,
            )
            ev.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)

        data = _safe_json(resp.text)
        raw_subq = data.get("subquestions") or []
        subquestions = [
            str(q) for q in raw_subq if isinstance(q, str) and q.strip()
        ][: opts.max_subquestions]
        # Ensure we always have at least the original question as a retrieval query
        if not subquestions:
            subquestions = [question]

        return SearchPlan(original_question=question, subquestions=subquestions), resp.input_tokens, resp.output_tokens

    def _retrieve_evidence(
        self,
        queries: list[str],
        opts: AgenticSearchOptions,
    ) -> list[EvidenceItem]:
        if not queries:
            return []
        # ask_multi returns a RetrievalResult; access .hits (list[RetrievalHit])
        result = self._mini.ask_multi(
            queries,
            top_k=opts.top_k_per_query,
            include_graph=False,
        )
        items: list[EvidenceItem] = []
        for idx, hit in enumerate(result.hits[: opts.max_total_evidence_items], start=1):
            items.append(_hit_to_evidence(hit, idx))
        return items

    async def _critique(
        self,
        question: str,
        plan: SearchPlan,
        evidence: list[EvidenceItem],
        opts: AgenticSearchOptions,
    ) -> tuple[EvidenceCritique, int, int]:
        prompt = self._prompts.render(
            CRITIC_PROMPT,
            PROMPT_VERSION,
            question=question,
            subquestions=plan.subquestions,
            evidence=[
                {"id": e.citation_label, "kind": e.kind, "text": e.text[:300]}
                for e in evidence
            ],
            max_follow_up=opts.max_subquestions,
        )
        with wide_event("agentic.critic", model=self._critic_model, n_evidence=len(evidence)) as ev:
            resp = await self._llm.complete_json(
                model=self._critic_model,
                system=prompt.system,
                user=prompt.user,
                max_tokens=512,
            )
            ev.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)

        data = _safe_json(resp.text)
        sufficient = bool(data.get("sufficient", True))
        follow_up = [
            str(q)
            for q in (data.get("follow_up_queries") or [])
            if isinstance(q, str) and q.strip()
        ]
        missing = [
            str(m)
            for m in (data.get("missing_information") or [])
            if isinstance(m, str)
        ]
        confidence = float(data.get("confidence") or 0.5)

        return (
            EvidenceCritique(
                sufficient=sufficient,
                follow_up_queries=follow_up,
                confidence=confidence,
                missing_information=missing,
            ),
            resp.input_tokens,
            resp.output_tokens,
        )

    async def _synthesize(
        self,
        question: str,
        plan: SearchPlan,
        evidence: list[EvidenceItem],
        critique: EvidenceCritique,
    ) -> tuple[str, int, int]:
        prompt = self._prompts.render(
            SYNTH_PROMPT,
            PROMPT_VERSION,
            question=question,
            subquestions=plan.subquestions,
            evidence=[
                {"id": e.citation_label, "kind": e.kind, "text": e.text}
                for e in evidence
            ],
            sufficient=critique.sufficient,
            missing=critique.missing_information,
        )
        with wide_event(
            "agentic.synthesis", model=self._synthesis_model, n_evidence=len(evidence)
        ) as ev:
            resp = await self._llm.complete_json(
                model=self._synthesis_model,
                system=prompt.system,
                user=prompt.user,
                max_tokens=2048,
            )
            ev.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)

        # complete_json forces JSON; extract prose from {"answer": "..."} or similar.
        # If the LLM returns plain prose anyway, use it directly (same pattern as lazy).
        data = _safe_json(resp.text)
        answer = ""
        if isinstance(data, dict):
            for key in ("answer", "response", "result", "text"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    answer = value.strip()
                    break
        if not answer:
            answer = resp.text.strip()

        return answer, resp.input_tokens, resp.output_tokens


# --------------------------------------------------------------------------- helpers


def _hit_to_evidence(hit: RetrievalHit, idx: int) -> EvidenceItem:
    """Convert a MiniGraphRagAgent RetrievalHit to an EvidenceItem with a citation label."""
    prefix = hit.kind[0].upper() if hit.kind else "E"
    label = f"[{prefix}{idx}]"
    meta = hit.meta or {}
    return EvidenceItem(
        kind=hit.kind,
        id=hit.id,
        text=hit.text,
        score=hit.score,
        citation_label=label,
        page_start=_as_int(meta.get("page_start")),
        page_end=_as_int(meta.get("page_end")),
        document_id=str(meta["document_id"]) if meta.get("document_id") else None,
    )


def _as_int(value: object) -> int | None:
    """Coerce a SQLite metadata value to int, or None when absent/non-numeric."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _merge_evidence(
    existing: list[EvidenceItem],
    new: list[EvidenceItem],
    *,
    max_items: int,
) -> list[EvidenceItem]:
    """Merge two evidence lists, deduplicating by composite kind:id key."""
    seen = {f"{e.kind}:{e.id}" for e in existing}
    merged = list(existing)
    for item in new:
        key = f"{item.kind}:{item.id}"
        if key not in seen and len(merged) < max_items:
            seen.add(key)
            merged.append(item)
    return merged


def _safe_json(text: str) -> dict[str, Any]:
    try:
        loaded = orjson.loads(text)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def agentic_index_available(settings: Settings) -> bool:
    """True iff the SQLite store has a non-empty ``chunks`` table (same as lazy mode)."""
    if not settings.sqlite_path.exists():
        return False
    try:
        con = sqlite3.connect(str(settings.sqlite_path))
        try:
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks' LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            count = con.execute("SELECT COUNT(*) FROM chunks").fetchone()
            return bool(count and count[0])
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False


__all__ = [
    "AgenticSearchAgent",
    "AgenticSearchAnswer",
    "AgenticSearchOptions",
    "AgenticTokenUsage",
    "EvidenceCritique",
    "EvidenceItem",
    "SearchPlan",
    "agentic_index_available",
]
