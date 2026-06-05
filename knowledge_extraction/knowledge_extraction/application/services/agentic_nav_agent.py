"""Agentic Navigator (``nav``) — metadata-first routing + tool-based navigation.

Unlike the other backends, ``nav`` does **not** fan out blind chunk retrieval
ahead of time. It mirrors how a human researcher works:

  question
    → ROUTE: inspect a lightweight metadata catalog (titles, counts, captions,
      previews) and pick the few candidate documents worth opening
    → NAVIGATE: a bounded ReAct tool loop that opens the actual document
      (``doc.md``), reads the relevant sections, searches within the document,
      and inspects tables / figures on demand
    → SYNTHESIZE: a grounded answer citing the documents / sections used

Because :class:`LLMPort` only exposes a single-shot ``complete_json`` (no native
tool-calling / message history), the navigation loop is implemented as a ReAct
JSON protocol: at each step the LLM returns
``{"thought": ..., "tool": ..., "args": {...}}`` and we re-prompt with an
appended, **bounded** transcript of observations.

All loops are bounded and all tool calls are validated against an allowlist with
clamped arguments, so the agent cannot run away, blow the context window, or call
unknown tools. The only side-effect-free tools available are those on
:class:`DocumentNavigator` (read-only SQLite + ``doc.md``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

import orjson

from knowledge_extraction.application.ports import LLMPort
from knowledge_extraction.application.services.document_navigator import (
    DocMeta,
    DocumentNavigator,
)
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.config.settings import Settings
from knowledge_extraction.infrastructure.telemetry.observability import wide_event

log = logging.getLogger(__name__)

ROUTE_PROMPT = "agentic_nav_route"
STEP_PROMPT = "agentic_nav_step"
SYNTH_PROMPT = "agentic_nav_synthesis"
PROMPT_VERSION = "v1"

# Tools the navigator LLM may call, with their required/optional argument keys.
NAV_TOOLS: dict[str, set[str]] = {
    "open_document": {"document_id"},
    "read_section": {"document_id", "query"},
    "search_document": {"document_id", "query"},
    "get_table": {"document_id", "table"},
    "get_figure": {"document_id", "figure"},
    "finish": set(),
}

_MAX_ARG_LEN = 240
_MAX_INVALID_STREAK = 3
_MAX_NOPROGRESS_STREAK = 3
_TRANSCRIPT_RENDER_WINDOW = 8


@dataclass(slots=True)
class AgenticNavOptions:
    max_docs: int = 3
    max_steps: int = 6
    search_top_k: int = 3


@dataclass(slots=True)
class NavStep:
    """One ReAct step: the chosen tool, its args, and the observation produced."""

    thought: str
    tool: str
    args: dict[str, str]
    observation: str

    def to_dict(self) -> dict[str, object]:
        return {
            "thought": self.thought,
            "tool": self.tool,
            "args": dict(self.args),
            "observation": self.observation,
        }


@dataclass(slots=True)
class NavTokenUsage:
    route_input: int = 0
    route_output: int = 0
    nav_input: int = 0
    nav_output: int = 0
    synth_input: int = 0
    synth_output: int = 0

    @property
    def total(self) -> int:
        return (
            self.route_input
            + self.route_output
            + self.nav_input
            + self.nav_output
            + self.synth_input
            + self.synth_output
        )


@dataclass(slots=True)
class AgenticNavAnswer:
    """Final answer plus the full navigation trace (route + tool transcript)."""

    question: str
    answer: str
    selected_documents: list[str]
    route_reasoning: str
    transcript: list[NavStep]
    steps: int
    tokens: NavTokenUsage
    duration_ms: int
    models: dict[str, str]

    @property
    def model(self) -> str:
        return self.models.get("navigator", "")

    def to_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "answer": self.answer,
            "selected_documents": list(self.selected_documents),
            "route_reasoning": self.route_reasoning,
            "models": dict(self.models),
            "duration_ms": self.duration_ms,
            "steps": self.steps,
            "tokens": asdict(self.tokens),
            "transcript": [s.to_dict() for s in self.transcript],
        }


class AgenticNavAgent:
    """Bounded agentic navigation: route → [tool loop] → synthesize."""

    def __init__(
        self,
        settings: Settings,
        navigator: DocumentNavigator,
        llm: LLMPort,
        prompts: PromptRegistry,
        *,
        router_model: str | None = None,
        navigator_model: str | None = None,
        synthesis_model: str | None = None,
    ) -> None:
        self._settings = settings
        self._nav = navigator
        self._llm = llm
        self._prompts = prompts
        extraction = settings.azure_openai_extraction_model or "gpt-4.1-mini"
        reasoning = settings.azure_openai_reasoning_model or extraction
        self._router_model = router_model or reasoning
        self._navigator_model = navigator_model or reasoning
        self._synthesis_model = synthesis_model or extraction

    # -------------------------------------------------------------- public API

    def ask(
        self,
        question: str,
        *,
        options: AgenticNavOptions | None = None,
    ) -> AgenticNavAnswer:
        return asyncio.run(self.ask_async(question, options=options))

    async def ask_async(
        self,
        question: str,
        *,
        options: AgenticNavOptions | None = None,
    ) -> AgenticNavAnswer:
        opts = options or AgenticNavOptions()
        t0 = time.perf_counter()
        tokens = NavTokenUsage()

        with wide_event(
            "nav.ask",
            question=question[:160],
            max_docs=opts.max_docs,
            max_steps=opts.max_steps,
            model=self._navigator_model,
        ) as ev:
            catalog = self._nav.catalog()
            ev.update(catalog_size=len(catalog))

            selected, reasoning, r_in, r_out = await self._route(question, catalog, opts)
            tokens.route_input += r_in
            tokens.route_output += r_out
            ev.update(selected_documents=len(selected))

            transcript: list[NavStep] = []
            if selected:
                transcript = await self._navigate(question, selected, catalog, opts, tokens)
            ev.update(steps=len(transcript))

            answer, s_in, s_out = await self._synthesize(question, selected, transcript)
            tokens.synth_input += s_in
            tokens.synth_output += s_out
            ev.update(total_tokens=tokens.total)

        return AgenticNavAnswer(
            question=question,
            answer=answer,
            selected_documents=selected,
            route_reasoning=reasoning,
            transcript=transcript,
            steps=len(transcript),
            tokens=tokens,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            models={
                "router": self._router_model,
                "navigator": self._navigator_model,
                "synthesis": self._synthesis_model,
            },
        )

    # ------------------------------------------------------------------- route

    async def _route(
        self,
        question: str,
        catalog: list[DocMeta],
        opts: AgenticNavOptions,
    ) -> tuple[list[str], str, int, int]:
        valid_ids = [m.document_id for m in catalog]
        if not catalog:
            return [], "no documents in catalog", 0, 0

        prompt = self._prompts.render(
            ROUTE_PROMPT,
            PROMPT_VERSION,
            question=question,
            max_docs=opts.max_docs,
            documents=[m.to_dict() for m in catalog],
        )
        with wide_event("nav.route", model=self._router_model, n_docs=len(catalog)) as ev:
            resp = await self._llm.complete_json(
                model=self._router_model,
                system=prompt.system,
                user=prompt.user,
                max_tokens=1536,
            )
            ev.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)

        data = _safe_json(resp.text)
        raw_ids = data.get("document_ids") or []
        valid_set = set(valid_ids)
        selected = [str(d) for d in raw_ids if isinstance(d, str) and d in valid_set]
        # Preserve order + de-duplicate, then cap.
        seen: set[str] = set()
        deduped: list[str] = []
        for d in selected:
            if d not in seen:
                seen.add(d)
                deduped.append(d)
        deduped = deduped[: max(1, opts.max_docs)]
        if not deduped:
            # Router gave nothing usable — fall back to the first few documents so
            # navigation can still proceed instead of dead-ending.
            deduped = valid_ids[: max(1, opts.max_docs)]
        reasoning = str(data.get("reasoning") or "").strip()
        return deduped, reasoning, resp.input_tokens, resp.output_tokens

    # ---------------------------------------------------------------- navigate

    async def _navigate(
        self,
        question: str,
        selected: list[str],
        catalog: list[DocMeta],
        opts: AgenticNavOptions,
        tokens: NavTokenUsage,
    ) -> list[NavStep]:
        meta_by_id = {m.document_id: m for m in catalog}
        selected_meta = [
            {"document_id": d, "title": meta_by_id[d].title if d in meta_by_id else d}
            for d in selected
        ]
        selected_set = set(selected)
        transcript: list[NavStep] = []
        seen_calls: dict[str, str] = {}
        invalid_streak = 0
        noprogress_streak = 0

        for step_num in range(1, max(1, opts.max_steps) + 1):
            remaining = opts.max_steps - step_num + 1
            action, a_in, a_out = await self._step(
                question, selected_meta, transcript, remaining
            )
            tokens.nav_input += a_in
            tokens.nav_output += a_out

            tool = str(action.get("tool") or "").strip()
            thought = str(action.get("thought") or "").strip()[:_MAX_ARG_LEN]
            raw_args = action.get("args")
            if not isinstance(raw_args, dict):
                raw_args = {}
            args = {str(k): str(v)[:_MAX_ARG_LEN] for k, v in raw_args.items()}

            if tool == "finish":
                break

            if tool not in NAV_TOOLS:
                if tool:
                    observation = (
                        f"error: unknown tool {tool!r}. "
                        f"Valid tools: {', '.join(sorted(NAV_TOOLS))}."
                    )
                else:
                    observation = (
                        "error: no tool was returned. Respond with a single JSON object "
                        '{"thought": "...", "tool": "<one of '
                        f"{', '.join(sorted(NAV_TOOLS))}>\", \"args\": {{...}}}}."
                    )
                invalid_streak += 1
                transcript.append(NavStep(thought, tool or "(none)", args, observation))
                if invalid_streak >= _MAX_INVALID_STREAK:
                    break
                continue

            valid, observation = self._validate_args(tool, args, selected_set)
            if not valid:
                invalid_streak += 1
                transcript.append(NavStep(thought, tool, args, observation))
                if invalid_streak >= _MAX_INVALID_STREAK:
                    break
                continue
            invalid_streak = 0

            call_key = _canonical_call(tool, args)
            if call_key in seen_calls:
                observation = "(already retrieved earlier — try a different tool or query)"
                noprogress_streak += 1
            else:
                observation = self._dispatch(tool, args)
                seen_calls[call_key] = observation
                if observation.startswith("error") or observation.startswith("("):
                    noprogress_streak += 1
                else:
                    noprogress_streak = 0

            transcript.append(NavStep(thought, tool, args, observation))
            if noprogress_streak >= _MAX_NOPROGRESS_STREAK:
                break

        return transcript

    def _validate_args(
        self, tool: str, args: dict[str, str], selected_set: set[str]
    ) -> tuple[bool, str]:
        required = NAV_TOOLS[tool]
        missing = [k for k in required if not args.get(k)]
        if missing:
            return False, f"error: tool {tool!r} missing required args: {', '.join(missing)}"
        doc = args.get("document_id")
        if "document_id" in required and doc not in selected_set:
            return (
                False,
                f"error: document_id {doc!r} is not one of the selected documents "
                f"({', '.join(sorted(selected_set))}).",
            )
        return True, ""

    def _dispatch(self, tool: str, args: dict[str, str]) -> str:
        try:
            if tool == "open_document":
                return self._nav.open_document(args["document_id"])
            if tool == "read_section":
                return self._nav.read_section(args["document_id"], args["query"])
            if tool == "search_document":
                return self._nav.search_document(args["document_id"], args["query"])
            if tool == "get_table":
                return self._nav.get_table(args["document_id"], args["table"])
            if tool == "get_figure":
                return self._nav.get_figure(args["document_id"], args["figure"])
        except Exception as exc:  # navigator tools should not raise, but be safe
            log.debug("nav.tool.error tool=%s error=%s", tool, exc)
            return f"error: tool {tool!r} failed: {exc}"
        return f"error: unknown tool {tool!r}"

    async def _step(
        self,
        question: str,
        selected_meta: list[dict[str, str]],
        transcript: list[NavStep],
        remaining: int,
    ) -> tuple[dict[str, Any], int, int]:
        rendered_transcript = [
            {"tool": s.tool, "args": s.args, "observation": s.observation}
            for s in transcript[-_TRANSCRIPT_RENDER_WINDOW:]
        ]
        prompt = self._prompts.render(
            STEP_PROMPT,
            PROMPT_VERSION,
            question=question,
            documents=selected_meta,
            transcript=rendered_transcript,
            remaining_steps=remaining,
            tools=sorted(NAV_TOOLS),
        )
        with wide_event(
            "nav.step", model=self._navigator_model, transcript_len=len(transcript)
        ) as ev:
            resp = await self._llm.complete_json(
                model=self._navigator_model,
                system=prompt.system,
                user=prompt.user,
                max_tokens=2048,
            )
            ev.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)
        return _safe_json(resp.text), resp.input_tokens, resp.output_tokens

    # --------------------------------------------------------------- synthesize

    async def _synthesize(
        self,
        question: str,
        selected: list[str],
        transcript: list[NavStep],
    ) -> tuple[str, int, int]:
        evidence = [
            {"document_id": s.args.get("document_id", ""), "tool": s.tool, "text": s.observation}
            for s in transcript
            if not s.observation.startswith("error") and not s.observation.startswith("(")
        ]
        prompt = self._prompts.render(
            SYNTH_PROMPT,
            PROMPT_VERSION,
            question=question,
            selected_documents=selected,
            evidence=evidence,
            has_evidence=bool(evidence),
        )
        with wide_event(
            "nav.synthesis", model=self._synthesis_model, n_evidence=len(evidence)
        ) as ev:
            resp = await self._llm.complete_json(
                model=self._synthesis_model,
                system=prompt.system,
                user=prompt.user,
                max_tokens=2048,
            )
            ev.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)

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


# --------------------------------------------------------------------- helpers


def _canonical_call(tool: str, args: dict[str, str]) -> str:
    items = sorted((k, v.strip().lower()) for k, v in args.items())
    return tool + "|" + "|".join(f"{k}={v}" for k, v in items)


def _safe_json(text: str) -> dict[str, Any]:
    s = text.strip()
    try:
        loaded = orjson.loads(s)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        pass
    # Some models emit the object more than once or append trailing data; parse the
    # first complete JSON object and ignore the rest.
    start = s.find("{")
    if start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[start:])
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


__all__ = [
    "AgenticNavAgent",
    "AgenticNavAnswer",
    "AgenticNavOptions",
    "NavStep",
    "NavTokenUsage",
    "_canonical_call",
    "_safe_json",
]
