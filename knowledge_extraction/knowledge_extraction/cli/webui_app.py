"""Unified Streamlit Web UI with telemetry and chat pages."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamlit as st

from knowledge_extraction.application.services.agentic_nav_agent import (
    AgenticNavAgent,
    AgenticNavOptions,
)
from knowledge_extraction.application.services.agentic_search_agent import (
    AgenticSearchAgent,
    AgenticSearchOptions,
)
from knowledge_extraction.application.services.chunk_retriever import ChunkRetriever
from knowledge_extraction.application.services.document_navigator import DocumentNavigator
from knowledge_extraction.application.services.graphrag_agent import (
    MiniGraphRagAgent,
    RetrievalHit,
    RetrievalResult,
)
from knowledge_extraction.application.services.lazy_graphrag_agent import (
    LazyGraphRagAgent,
    LazyGraphRagAnswer,
)
from knowledge_extraction.application.services.ms_graphrag_agent import (
    IndexNotFoundError,
    MsGraphRagAgent,
)
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.application.services.text_dedup import dedupe_by_text
from knowledge_extraction.config.settings import AzureAuthMode, Settings, get_settings
from knowledge_extraction.infrastructure.llm.azure_foundry_client import AzureFoundryLLM

DEFAULT_PRICES = {
    "gpt-5.4": {"in": 1.25, "out": 10.00},
    "gpt-5.4-mini": {"in": 0.25, "out": 2.00},
    "text-embedding-ada-002": {"in": 0.10, "out": 0.0},
    "text-embedding-3-large": {"in": 0.13, "out": 0.0},
}

BACKEND_LABELS = {
    "mini": "Evidence Retriever (Mini, no synthesis)",
    "lazy": "LazyGraphRAG (synthesized answer + citations)",
    "ms": "Microsoft GraphRAG (indexed synthesis)",
    "agentic": "Agentic RAG (plan → retrieve → critique → synthesize)",
    "nav": "Agentic Navigator (metadata routing → on-demand document reading)",
}


def _load_demo_queries(settings: Settings) -> list[dict[str, Any]]:
    """Load the curated demo-query ladder, returning [] if the file is absent or malformed."""
    path = settings.project_root / "config" / "evals" / "demo_queries.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tiers = data.get("tiers")
    return tiers if isinstance(tiers, list) else []


@dataclass(slots=True)
class FigureRef:
    id: str
    page: int | None
    caption: str
    image_path: Path | None
    document_title: str
    source_path: Path | None


@dataclass(slots=True)
class TableRef:
    id: str
    page: int | None
    caption: str
    document_title: str
    source_path: Path | None


@dataclass(slots=True)
class ChunkRef:
    id: str
    text: str
    page_start: int | None
    page_end: int | None
    document_title: str
    source_path: Path | None
    figure_refs_json: str | None
    table_refs_json: str | None


def _parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--backend", default="lazy")
    return parser.parse_known_args()[0]


def _cost_usd(model: str, in_tok: int, out_tok: int, prices: dict[str, dict[str, float]]) -> float:
    p = prices.get(model)
    if not p:
        return 0.0
    return (in_tok / 1_000_000) * p["in"] + (out_tok / 1_000_000) * p["out"]


def _fmt_ms(ms: int | float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{int(ms)} ms"
    return f"{ms / 1000:.2f} s"


@st.cache_data(show_spinner=False)
def _list_runs(log_dir: str) -> list[Path]:
    directory = Path(log_dir)
    if not directory.exists():
        return []
    return sorted(directory.glob("run-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


@st.cache_data(show_spinner=False)
def _load_run(path: str) -> dict[str, Any]:
    run_file = Path(path)
    records: list[dict[str, Any]] = []
    for line in run_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    by_event: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        event = record.get("event")
        if event:
            by_event.setdefault(event, []).append(record)

    start = next((r for r in records if r.get("event") == "run.start"), {})
    finish = next((r for r in records if r.get("event") == "run.finish"), {})
    return {
        "path": run_file,
        "records": records,
        "by_event": by_event,
        "run_id": start.get("run_id") or (records[0].get("run_id") if records else None),
        "command": start.get("command", "—"),
        "mode": next((r.get("mode") for r in records if r.get("mode")), None),
        "pdf": next((r.get("pdf") for r in records if r.get("pdf")), None),
        "duration_ms": finish.get("duration_ms"),
    }


def _build_lazy_agent(settings: Settings) -> LazyGraphRagAgent:
    return LazyGraphRagAgent(
        settings=settings,
        chunk_retriever=ChunkRetriever(settings.sqlite_path),
        llm=AzureFoundryLLM(settings),
        prompts=PromptRegistry(settings.prompts_dir),
        model=settings.azure_openai_extraction_model,
    )


def _build_agentic_nav_agent(settings: Settings) -> AgenticNavAgent:
    navigator = DocumentNavigator(
        settings.sqlite_path,
        settings.artifact_path,
        max_chars=settings.agentic_nav_max_chars,
    )
    return AgenticNavAgent(
        settings=settings,
        navigator=navigator,
        llm=AzureFoundryLLM(settings),
        prompts=PromptRegistry(settings.prompts_dir),
        router_model=settings.agentic_nav_router_model or None,
        navigator_model=settings.agentic_nav_navigator_model or None,
        synthesis_model=settings.agentic_nav_synthesis_model or None,
    )


def _build_agentic_agent(settings: Settings) -> AgenticSearchAgent:
    from knowledge_extraction.application.services.graphrag_agent import MiniGraphRagAgent

    mini = MiniGraphRagAgent(settings.sqlite_path, settings.graph_storage_path)
    return AgenticSearchAgent(
        settings=settings,
        mini_agent=mini,
        llm=AzureFoundryLLM(settings),
        prompts=PromptRegistry(settings.prompts_dir),
        planner_model=settings.agentic_planner_model or None,
        critic_model=settings.agentic_critic_model or None,
        synthesis_model=settings.agentic_synthesis_model or None,
    )


def _normalize_path(raw: str | None, *, project_root: Path) -> Path | None:
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        return p if p.exists() else None
    candidate = (project_root / p).resolve()
    return candidate if candidate.exists() else None


def _load_figure_refs(settings: Settings) -> dict[str, FigureRef]:
    if not settings.sqlite_path.exists():
        return {}
    con = sqlite3.connect(str(settings.sqlite_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT
                f.id,
                f.page,
                f.caption,
                f.image_path,
                COALESCE(d.title, '') AS document_title,
                d.source_path AS source_path
            FROM figures f
            LEFT JOIN documents d ON d.id = f.document_id
            """
        ).fetchall()
        return {
            str(r["id"]): FigureRef(
                id=str(r["id"]),
                page=int(r["page"]) if r["page"] is not None else None,
                caption=str(r["caption"] or ""),
                image_path=_normalize_path(str(r["image_path"]) if r["image_path"] else None, project_root=settings.project_root),
                document_title=str(r["document_title"] or ""),
                source_path=_normalize_path(str(r["source_path"]) if r["source_path"] else None, project_root=settings.project_root),
            )
            for r in rows
        }
    finally:
        con.close()


def _load_table_refs(settings: Settings) -> dict[str, TableRef]:
    if not settings.sqlite_path.exists():
        return {}
    con = sqlite3.connect(str(settings.sqlite_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT t.id, t.page, t.caption, COALESCE(d.title, '') AS document_title, d.source_path AS source_path
            FROM tables t
            LEFT JOIN documents d ON d.id = t.document_id
            """
        ).fetchall()
        return {
            str(r["id"]): TableRef(
                id=str(r["id"]),
                page=int(r["page"]) if r["page"] is not None else None,
                caption=str(r["caption"] or ""),
                document_title=str(r["document_title"] or ""),
                source_path=_normalize_path(str(r["source_path"]) if r["source_path"] else None, project_root=settings.project_root),
            )
            for r in rows
        }
    finally:
        con.close()


def _json_list(raw: object) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(str(raw))
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str)]


def _short_text(text: str, max_chars: int = 300) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return f"{cleaned[:max_chars - 1].rstrip()}…"


def _load_chunk_refs(settings: Settings) -> dict[str, ChunkRef]:
    if not settings.sqlite_path.exists():
        return {}
    con = sqlite3.connect(str(settings.sqlite_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT
                c.id,
                c.text,
                c.page_start,
                c.page_end,
                c.figure_refs_json,
                c.table_refs_json,
                COALESCE(d.title, '') AS document_title,
                d.source_path AS source_path
            FROM chunks c
            LEFT JOIN documents d ON d.id = c.document_id
            """
        ).fetchall()
        return {
            str(r["id"]): ChunkRef(
                id=str(r["id"]),
                text=str(r["text"] or ""),
                page_start=int(r["page_start"]) if r["page_start"] is not None else None,
                page_end=int(r["page_end"]) if r["page_end"] is not None else None,
                document_title=str(r["document_title"] or ""),
                source_path=_normalize_path(str(r["source_path"]) if r["source_path"] else None, project_root=settings.project_root),
                figure_refs_json=str(r["figure_refs_json"]) if r["figure_refs_json"] else None,
                table_refs_json=str(r["table_refs_json"]) if r["table_refs_json"] else None,
            )
            for r in rows
        }
    finally:
        con.close()


def _page_preview_path(settings: Settings, source_path: Path | None, page: int | None) -> Path | None:
    if source_path is None or page is None:
        return None
    candidate = settings.artifact_path / source_path.stem / "pages" / f"page_{page:04d}.png"
    return candidate if candidate.exists() else None


def _render_source_links(
    *,
    settings: Settings,
    source_path: Path | None,
    page: int | None,
    page_end: int | None = None,
) -> None:
    if source_path is not None:
        st.markdown(f"[Open source PDF]({source_path.resolve().as_uri()})")
    page_preview = _page_preview_path(settings, source_path, page)
    if page_preview is not None:
        caption = f"Source page preview p.{page}" if page_end in {None, page} else f"Source page preview p.{page}-{page_end}"
        st.image(str(page_preview), caption=caption, width=520)


def _citation_anchor(citation_index: int) -> str:
    return f"citation-{citation_index}"


def _first_chunk_figure_ref(chunk: ChunkRef, figure_index: dict[str, FigureRef]) -> FigureRef | None:
    """Pick the first figure for this chunk that has a real image on disk.

    Layout extraction produces both synthetic placeholders (id like "94.1" with
    no image_path) and real cropped figures (hashed id with image_path). Prefer
    real images so the evidence panel actually shows the diagram, falling back
    to a placeholder only if no real image exists.
    """
    first_any: FigureRef | None = None
    for fid in _json_list(chunk.figure_refs_json):
        ref = figure_index.get(fid)
        if ref is None:
            continue
        if ref.image_path is not None:
            return ref
        if first_any is None:
            first_any = ref
    return first_any


def _render_evidence_panel(
    *,
    settings: Settings,
    evidence: list[dict[str, object]],
) -> None:
    if not evidence:
        return
    st.markdown("**Citations**")
    chips = " ".join(
        f"<a href='#{_citation_anchor(i)}'>[{i}]</a>"
        for i in range(1, len(evidence) + 1)
    )
    st.markdown(chips, unsafe_allow_html=True)
    st.markdown("**Evidence**")
    for idx, item in enumerate(evidence, start=1):
        st.markdown(f"<a id='{_citation_anchor(idx)}'></a>", unsafe_allow_html=True)
        kind = str(item.get("kind", "evidence"))
        title = str(item.get("title", "")).strip() or str(item.get("id", ""))
        with st.expander(f"[{idx}] {kind.upper()} · {title}", expanded=False):
            snippet = str(item.get("snippet", "")).strip()
            if snippet:
                st.write(snippet)
            score = item.get("score")
            if isinstance(score, float):
                st.caption(f"score={score:.3f}")

            source_path = _normalize_path(str(item.get("source_path")) if item.get("source_path") else None, project_root=settings.project_root)
            page = int(item["page"]) if isinstance(item.get("page"), int) else None
            page_end = int(item["page_end"]) if isinstance(item.get("page_end"), int) else None
            _render_source_links(settings=settings, source_path=source_path, page=page, page_end=page_end)

            image_path = _normalize_path(str(item.get("image_path")) if item.get("image_path") else None, project_root=settings.project_root)
            if image_path is not None:
                st.markdown(f"[Open diagram file]({image_path.resolve().as_uri()})")
                st.image(str(image_path), caption=title or "Diagram", width=520)


def _render_ms_debug_panel(
    *,
    question: str,
    method: str,
    community_level: int,
    response_type: str,
    answer_payload: dict[str, object],
) -> None:
    st.markdown("**GraphRAG query debug**")
    cmd = (
        f'graphrag query --root "{answer_payload.get("workdir", "")}" '
        f'--method {method} --community-level {community_level} '
        f'--response-type "{response_type}" --query "{question}"'
    )
    st.code(cmd, language="bash")
    st.caption(
        f"method={answer_payload.get('method', method)} · "
        f"duration={answer_payload.get('duration_ms', 0)} ms · "
        f"exit_code={answer_payload.get('exit_code', 0)}"
    )
    raw_output = str(answer_payload.get("raw_output", "") or "")
    if raw_output:
        with st.expander("Raw graphrag output"):
            st.text(raw_output)


def _build_lazy_evidence(
    *,
    answer: LazyGraphRagAnswer,
    figure_ids: set[str],
    table_ids: set[str],
    figure_index: dict[str, FigureRef],
    table_index: dict[str, TableRef],
    chunk_index: dict[str, ChunkRef],
) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for chunk in answer.chunks[:8]:
        key = ("chunk", chunk.id)
        if key in seen:
            continue
        seen.add(key)
        chunk_ref = chunk_index.get(chunk.id)
        linked_figure = _first_chunk_figure_ref(chunk_ref, figure_index) if chunk_ref is not None else None
        page_hint = f"(estimated chunk pages p.{chunk.page_start}-{chunk.page_end})"
        evidence.append(
            {
                "kind": "chunk",
                "id": chunk.id,
                "title": f"{chunk.document_title} · {page_hint}",
                "snippet": _short_text(chunk.text, 420),
                "score": float(chunk.score),
                "source_path": str(chunk_index[chunk.id].source_path) if chunk.id in chunk_index and chunk_index[chunk.id].source_path else "",
                "page": int(linked_figure.page) if linked_figure is not None and linked_figure.page is not None else None,
                "page_end": None,
                "image_path": str(linked_figure.image_path) if linked_figure is not None and linked_figure.image_path else "",
            }
        )
    for fid in sorted(figure_ids):
        ref = figure_index.get(fid)
        if ref is None:
            continue
        key = ("figure", fid)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            {
                "kind": "diagram",
                "id": ref.id,
                "title": f"{ref.document_title} · p.{ref.page or '?'}",
                "snippet": ref.caption,
                "source_path": str(ref.source_path) if ref.source_path else "",
                "page": ref.page,
                "image_path": str(ref.image_path) if ref.image_path else "",
            }
        )
    for tid in sorted(table_ids):
        ref = table_index.get(tid)
        if ref is None:
            continue
        key = ("table", tid)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            {
                "kind": "table",
                "id": ref.id,
                "title": f"{ref.document_title} · p.{ref.page or '?'}",
                "snippet": ref.caption,
                "source_path": str(ref.source_path) if ref.source_path else "",
                "page": ref.page,
            }
        )
    # Final defensive pass: collapse items with the same snippet text (catches
    # cross-kind overlap, e.g. a chunk whose body also surfaced as a claim).
    return dedupe_by_text(evidence, key=lambda e: str(e.get("snippet", "")))


def _build_mini_evidence(
    *,
    hits: list[RetrievalHit],
    figure_ids: set[str],
    table_ids: set[str],
    figure_index: dict[str, FigureRef],
    table_index: dict[str, TableRef],
    chunk_index: dict[str, ChunkRef],
) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits[:10]:
        key = (hit.kind, hit.id)
        if key in seen:
            continue
        seen.add(key)
        source_path = ""
        page = None
        page_end = None
        image_path = ""
        if hit.kind == "chunk":
            chunk = chunk_index.get(hit.id)
            if chunk is not None:
                source_path = str(chunk.source_path) if chunk.source_path else ""
                linked_figure = _first_chunk_figure_ref(chunk, figure_index)
                if linked_figure is not None:
                    page = linked_figure.page
                    image_path = str(linked_figure.image_path) if linked_figure.image_path else ""
        evidence.append(
            {
                "kind": hit.kind,
                "id": hit.id,
                "title": hit.id,
                "snippet": _short_text(hit.text, 420),
                "score": float(hit.score),
                "source_path": source_path,
                "page": page if page is not None else hit.meta.get("page"),
                "page_end": page_end if page_end is not None else hit.meta.get("page_end"),
                "image_path": image_path,
            }
        )
    for fid in sorted(figure_ids):
        ref = figure_index.get(fid)
        if ref is None:
            continue
        key = ("diagram", fid)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            {
                "kind": "diagram",
                "id": ref.id,
                "title": f"{ref.document_title} · p.{ref.page or '?'}",
                "snippet": ref.caption,
                "source_path": str(ref.source_path) if ref.source_path else "",
                "page": ref.page,
                "image_path": str(ref.image_path) if ref.image_path else "",
            }
        )
    for tid in sorted(table_ids):
        ref = table_index.get(tid)
        if ref is None:
            continue
        key = ("table", tid)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            {
                "kind": "table",
                "id": ref.id,
                "title": f"{ref.document_title} · p.{ref.page or '?'}",
                "snippet": ref.caption,
                "source_path": str(ref.source_path) if ref.source_path else "",
                "page": ref.page,
            }
        )
    # Final defensive pass: collapse items with the same snippet text (catches
    # cross-kind overlap, e.g. a chunk whose body also surfaced as a claim).
    return dedupe_by_text(evidence, key=lambda e: str(e.get("snippet", "")))


def _collect_mini_refs(hits: list[RetrievalHit]) -> tuple[set[str], set[str]]:
    figure_ids: set[str] = set()
    table_ids: set[str] = set()
    for hit in hits:
        if hit.kind == "figure":
            figure_ids.add(hit.id)
        if hit.kind == "table":
            table_ids.add(hit.id)
        if hit.kind == "claim":
            f_id = hit.meta.get("supporting_figure_id")
            t_id = hit.meta.get("supporting_table_id")
            if isinstance(f_id, str) and f_id:
                figure_ids.add(f_id)
            if isinstance(t_id, str) and t_id:
                table_ids.add(t_id)
        if hit.kind == "chunk":
            figure_ids.update(_json_list(hit.meta.get("figure_refs_json")))
            table_ids.update(_json_list(hit.meta.get("table_refs_json")))
    return figure_ids, table_ids


def _collect_lazy_refs(answer: LazyGraphRagAnswer) -> tuple[set[str], set[str]]:
    figure_ids: set[str] = set()
    table_ids: set[str] = set()
    for chunk in answer.chunks:
        figure_ids.update(_json_list(chunk.figure_refs_json))
        table_ids.update(_json_list(chunk.table_refs_json))
    for claim in answer.subgraph.claims:
        f_id = claim.get("supporting_figure_id")
        t_id = claim.get("supporting_table_id")
        if isinstance(f_id, str) and f_id:
            figure_ids.add(f_id)
        if isinstance(t_id, str) and t_id:
            table_ids.add(t_id)
    return figure_ids, table_ids


def _render_figure_refs(figure_ids: set[str], figure_index: dict[str, FigureRef]) -> None:
    if not figure_ids:
        return
    st.markdown("**Diagram references**")
    for fid in sorted(figure_ids):
        ref = figure_index.get(fid)
        if ref is None:
            st.write(f"- `{fid}`")
            continue
        page = f"p.{ref.page}" if ref.page else "page ?"
        label = f"{ref.document_title} — {page} — {ref.caption or fid}".strip(" —")
        st.write(f"- `{fid}` · {label}")
        if ref.image_path is not None:
            st.markdown(f"  [Open diagram file]({ref.image_path.resolve().as_uri()})")
            st.image(str(ref.image_path), caption=ref.caption or fid, width=520)


def _render_table_refs(table_ids: set[str], table_index: dict[str, TableRef]) -> None:
    if not table_ids:
        return
    st.markdown("**Table references**")
    for tid in sorted(table_ids):
        ref = table_index.get(tid)
        if ref is None:
            st.write(f"- `{tid}`")
            continue
        page = f"p.{ref.page}" if ref.page else "page ?"
        label = f"{ref.document_title} — {page} — {ref.caption or tid}".strip(" —")
        st.write(f"- `{tid}` · {label}")


def _mini_answer(result: RetrievalResult) -> str:
    if not result.hits:
        return "No relevant evidence found."
    lines = ["Top evidence snippets:"]
    for idx, hit in enumerate(result.hits[:5], start=1):
        lines.append(f"{idx}. [{hit.kind}] {hit.text}")
    return "\n".join(lines)


def _render_hit_table(hits: list[RetrievalHit]) -> None:
    if not hits:
        return
    rows = [
        {
            "rank": idx,
            "kind": h.kind,
            "id": h.id,
            "score": round(h.score, 3),
            "text": h.text,
        }
        for idx, h in enumerate(hits, start=1)
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _is_key_auth_disabled_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "authenticationtypedisabled" in message
        or "key based authentication is disabled" in message
    )


def _auth_remediation_message() -> str:
    return (
        "Azure key auth is disabled for this resource. Use credential auth instead.\n\n"
        "```powershell\n"
        "$env:AZURE_AUTH_MODE='credential'\n"
        "uv run ke preflight\n"
        "uv run ke webui --backend lazy\n"
        "```"
    )


def _render_telemetry_page(settings: Settings) -> None:
    st.subheader("Telemetry")
    st.caption("Inspect run logs from `work/logs` without re-running extraction.")
    log_dir_input = st.text_input("Log directory", value=str(settings.log_dir))
    runs = _list_runs(log_dir_input)
    if not runs:
        st.warning(f"No runs found in {log_dir_input}")
        return

    selected = st.selectbox(
        "Run file",
        options=list(range(len(runs))),
        format_func=lambda idx: runs[idx].name,
    )
    bundle = _load_run(str(runs[selected]))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Run ID", bundle["run_id"] or "—")
    c2.metric("Command", bundle["command"] or "—")
    c3.metric("Mode", bundle["mode"] or "—")
    c4.metric("Duration", _fmt_ms(bundle["duration_ms"]))

    llm_calls = bundle["by_event"].get("llm.complete_json", [])
    input_tokens = sum(int(r.get("input_tokens", 0)) for r in llm_calls)
    output_tokens = sum(int(r.get("output_tokens", 0)) for r in llm_calls)
    estimated_cost = sum(
        _cost_usd(
            str(r.get("model", "")),
            int(r.get("input_tokens", 0)),
            int(r.get("output_tokens", 0)),
            DEFAULT_PRICES,
        )
        for r in llm_calls
    )
    t1, t2, t3 = st.columns(3)
    t1.metric("LLM calls", len(llm_calls))
    t2.metric("Input / Output tokens", f"{input_tokens:,} / {output_tokens:,}")
    t3.metric("Estimated LLM cost", f"${estimated_cost:.4f}")

    stages = bundle["by_event"].get("pipeline.stage", [])
    st.markdown("**Pipeline stages**")
    if not stages:
        st.info("No pipeline stage records for this run.")
    else:
        rows = [
            {
                "stage": str(s.get("stage", "?")),
                "status": str(s.get("status", "?")),
                "duration_ms": int(s.get("duration_ms", 0)),
            }
            for s in stages
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        chart_data = {row["stage"]: row["duration_ms"] for row in rows}
        st.bar_chart(chart_data)

    with st.expander("Raw run metadata"):
        st.json(
            {
                "run.start": bundle["by_event"].get("run.start", [{}])[0],
                "run.finish": bundle["by_event"].get("run.finish", [{}])[0],
            }
        )


def _render_query_footer(stats: dict[str, Any]) -> None:
    """Render a persistent post-query footer with total wall-clock time and tokens spent."""
    elapsed = float(stats.get("elapsed_s", 0.0) or 0.0)
    tokens = stats.get("tokens")
    extra = str(stats.get("extra", "") or "")
    tok = f"{int(tokens):,}" if isinstance(tokens, (int, float)) else "n/a"
    line = f"⏱ **Total time:** {elapsed:.1f}s  ·  🧮 **Tokens spent:** {tok}"
    if extra:
        line += f"  ·  {extra}"
    st.caption(line)


def _render_demo_panel(settings: Settings) -> None:
    """Render the curated demo-query ladder as always-visible one-click presets (right column)."""
    st.markdown("### 🎬 Demo queries")
    tiers = _load_demo_queries(settings)
    if not tiers:
        st.caption("No demo_queries.json found in config/evals.")
        return
    backend = str(st.session_state.get("chat_backend", "") or "")
    st.caption(
        "Click a query to run it in the current mode. A backend-comparison ladder: "
        "Tier 1 plain RAG suffices; Tier 2 needs GraphRAG; Tier 3 favours Agentic/Nav; "
        "Tier 4 should be refused."
        + (
            f" Active mode: **{BACKEND_LABELS.get(backend, backend)}**."
            if backend
            else ""
        )
    )
    for t_idx, tier in enumerate(tiers):
        label = str(tier.get("label", tier.get("id", "Tier")))
        rec = str(tier.get("recommended_backend", "")).strip()
        header = f"**{label}**"
        if rec:
            header += f"  ·  best mode: `{rec}`"
        st.markdown(header)
        summary = str(tier.get("summary", "")).strip()
        if summary:
            st.caption(summary)
        queries = tier.get("queries", [])
        if not isinstance(queries, list):
            continue
        for q_idx, query in enumerate(queries):
            text = str(query.get("text", "")).strip()
            if not text:
                continue
            if st.button(text, key=f"demo-{t_idx}-{q_idx}", use_container_width=True):
                st.session_state["_demo_prompt"] = text
                st.rerun()
            watch_for = str(query.get("watch_for", "")).strip()
            if watch_for:
                st.caption(f"👀 {watch_for}")
        st.divider()



def _render_chat_page(settings: Settings, default_backend: str) -> None:
    chat_col, demo_col = st.columns([3, 1], gap="large")
    with chat_col:
        _render_chat_column(settings, default_backend)
    with demo_col:
        _render_demo_panel(settings)


def _render_chat_column(settings: Settings, default_backend: str) -> None:
    st.subheader("Chat")
    st.caption("Ask questions and inspect evidence references (including diagram links).")

    c1, c2 = st.columns([1, 1])
    # Ordered by increasing complexity: evidence → graphrag → lazygraphrag → agentic → navigator.
    backend_options = ["mini", "ms", "lazy", "agentic", "nav"]
    default_index = backend_options.index(default_backend) if default_backend in backend_options else 0
    backend = c1.radio(
        "Retrieval mode",
        backend_options,
        index=default_index,
        format_func=lambda b: BACKEND_LABELS.get(b, b),
        key="chat_backend",
    )
    top_k = c2.slider("Top K", min_value=3, max_value=30, value=10, step=1)
    st.caption(f"Active mode: **{BACKEND_LABELS.get(backend, backend)}**")
    ms_method = "auto"
    ms_community = 2
    ms_response_type = "Multiple Paragraphs"
    if backend == "ms":
        m1, m2 = st.columns([1, 1])
        ms_method = m1.selectbox("MS method", ["auto", "local", "global", "drift", "basic"], index=0)
        ms_community = m2.slider("Community level", min_value=1, max_value=6, value=2, step=1)
        ms_response_type = st.selectbox(
            "MS response type",
            ["Multiple Paragraphs", "Single Paragraph", "Bullet List"],
            index=0,
        )
    if st.button("Clear chat"):
        st.session_state.pop("messages", None)
        st.rerun()

    st.caption(f"SQLite: {settings.sqlite_path}")
    if settings.azure_auth_mode is AzureAuthMode.KEY:
        st.warning("AZURE_AUTH_MODE=key. If your Azure resource disables key auth, chat queries will fail.")

    if "messages" not in st.session_state:
        st.session_state.messages = []

    figure_index = _load_figure_refs(settings)
    table_index = _load_table_refs(settings)
    chunk_index = _load_chunk_refs(settings)

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["text"])
            if message.get("evidence"):
                _render_evidence_panel(settings=settings, evidence=message["evidence"])
            if message.get("ms_debug"):
                _render_ms_debug_panel(
                    question=str(message.get("question", "")),
                    method=str(message["ms_debug"].get("method", "local")),
                    community_level=int(message["ms_debug"].get("community_level", 2)),
                    response_type=str(message["ms_debug"].get("response_type", "Multiple Paragraphs")),
                    answer_payload=message["ms_debug"],
                )
            if message.get("stats"):
                _render_query_footer(message["stats"])

    prompt = st.chat_input("Ask a question about your ingested knowledge base")
    if not prompt:
        prompt = st.session_state.pop("_demo_prompt", None)
    if not prompt:
        return

    st.session_state.messages.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        t0 = time.perf_counter()
        try:
            if backend == "lazy":
                agent = _build_lazy_agent(settings)
                with st.spinner("Running LazyGraphRAG (retrieval + LLM synthesis)..."):
                    answer = agent.ask(prompt, top_k_chunks=top_k)
                figure_ids, table_ids = _collect_lazy_refs(answer)
                evidence = _build_lazy_evidence(
                    answer=answer,
                    figure_ids=figure_ids,
                    table_ids=table_ids,
                    figure_index=figure_index,
                    table_index=table_index,
                    chunk_index=chunk_index,
                )
                st.markdown(answer.answer)
                _render_evidence_panel(settings=settings, evidence=evidence)
                stats = {"elapsed_s": time.perf_counter() - t0, "tokens": answer.tokens.total}
                _render_query_footer(stats)
                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "text": answer.answer,
                        "evidence": evidence,
                        "stats": stats,
                    }
                )
                return

            if backend == "mini":
                agent = MiniGraphRagAgent(settings.sqlite_path, settings.graph_storage_path)
                with st.spinner("Running MiniGraphRAG (BM25 + graph hop)..."):
                    result = agent.ask(prompt, top_k=top_k, include_graph=False)
                figure_ids, table_ids = _collect_mini_refs(result.hits)
                answer_text = _mini_answer(result)
                evidence = _build_mini_evidence(
                    hits=result.hits,
                    figure_ids=figure_ids,
                    table_ids=table_ids,
                    figure_index=figure_index,
                    table_index=table_index,
                    chunk_index=chunk_index,
                )
                st.markdown(answer_text)
                _render_evidence_panel(settings=settings, evidence=evidence)
                stats = {
                    "elapsed_s": time.perf_counter() - t0,
                    "tokens": 0,
                    "extra": "lexical retrieval (no LLM)",
                }
                _render_query_footer(stats)
                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "text": answer_text,
                        "evidence": evidence,
                        "stats": stats,
                    }
                )
                return

            if backend == "agentic":
                agent = _build_agentic_agent(settings)
                opts = AgenticSearchOptions(
                    max_rounds=settings.agentic_max_rounds,
                    max_subquestions=settings.agentic_max_subquestions,
                    top_k_per_query=top_k,
                )
                with st.spinner(
                    "Agentic RAG: planning subquestions → retrieving evidence → "
                    "critiquing → synthesizing..."
                ):
                    answer = agent.ask(prompt, options=opts)
                with st.expander("🗺 Plan", expanded=True):
                    for i, sq in enumerate(answer.plan.subquestions, start=1):
                        st.write(f"{i}. {sq}")
                with st.expander(
                    f"🔍 Evidence critique (round {answer.rounds}, "
                    f"confidence={answer.critique.confidence:.0%})"
                ):
                    st.write(f"**Sufficient:** {answer.critique.sufficient}")
                    if answer.critique.missing_information:
                        st.write("**Missing information:**")
                        for m in answer.critique.missing_information:
                            st.write(f"- {m}")
                    if answer.critique.follow_up_queries:
                        st.write("**Follow-up queries used:**")
                        for q in answer.critique.follow_up_queries:
                            st.write(f"- {q}")
                st.markdown(answer.answer)
                with st.expander(f"📄 Evidence ({len(answer.evidence)} items)"):
                    for e in answer.evidence:
                        st.write(
                            f"**{e.citation_label}** [{e.kind}] "
                            f"score={e.score:.3f}"
                            + (f" · p{e.page_start}" if e.page_start else "")
                        )
                        st.write(e.text[:300])
                        st.divider()
                st.caption(
                    f"Agentic RAG · {answer.duration_ms} ms · "
                    f"tokens={answer.tokens.total} · rounds={answer.rounds}"
                )
                stats = {
                    "elapsed_s": time.perf_counter() - t0,
                    "tokens": answer.tokens.total,
                    "extra": f"rounds={answer.rounds}",
                }
                _render_query_footer(stats)
                st.session_state.messages.append(
                    {"role": "assistant", "text": answer.answer, "stats": stats}
                )
                return

            if backend == "nav":
                agent = _build_agentic_nav_agent(settings)
                opts = AgenticNavOptions(
                    max_docs=settings.agentic_nav_max_docs,
                    max_steps=settings.agentic_nav_max_steps,
                )
                with st.spinner(
                    "Agentic Navigator: routing to documents → navigating "
                    "sections/tables/figures → synthesizing..."
                ):
                    answer = agent.ask(prompt, options=opts)
                with st.expander("🗂 Documents selected", expanded=True):
                    if answer.route_reasoning:
                        st.caption(answer.route_reasoning)
                    for d in answer.selected_documents:
                        st.write(f"- {d}")
                st.markdown(answer.answer)
                with st.expander(f"🧭 Navigation trace ({answer.steps} step(s))"):
                    for i, s in enumerate(answer.transcript, start=1):
                        st.write(f"**{i}. {s.tool}** `{s.args}`")
                        st.write(s.observation[:400])
                        st.divider()
                st.caption(
                    f"Agentic Navigator · {answer.duration_ms} ms · "
                    f"tokens={answer.tokens.total} · docs={len(answer.selected_documents)} · "
                    f"steps={answer.steps}"
                )
                stats = {
                    "elapsed_s": time.perf_counter() - t0,
                    "tokens": answer.tokens.total,
                    "extra": f"docs={len(answer.selected_documents)} · steps={answer.steps}",
                }
                _render_query_footer(stats)
                st.session_state.messages.append(
                    {"role": "assistant", "text": answer.answer, "stats": stats}
                )
                return

            ms_agent = MsGraphRagAgent(settings)
            selected_method = None if ms_method == "auto" else ms_method
            with st.spinner(
                "Running Microsoft GraphRAG query (usually 20-60s — "
                "subprocess loads parquets, runs embedding + LLM)..."
            ):
                ms_answer = ms_agent.ask(
                    prompt,
                    method=selected_method,  # type: ignore[arg-type]
                    community_level=ms_community,
                    response_type=ms_response_type,
                )
            st.markdown(ms_answer.answer)
            st.caption(
                "MS GraphRAG does not expose structured figure/table references in this app; "
                "use mini/lazy for explicit diagram links."
            )
            payload = ms_answer.to_dict()
            payload["raw_output"] = ms_answer.raw_output
            payload["community_level"] = ms_community
            payload["response_type"] = ms_response_type
            _render_ms_debug_panel(
                question=prompt,
                method=payload.get("method", "local"),  # type: ignore[arg-type]
                community_level=ms_community,
                response_type=ms_response_type,
                answer_payload=payload,
            )
            stats = {
                "elapsed_s": time.perf_counter() - t0,
                "tokens": None,
                "extra": "tokens not captured (graphrag subprocess)",
            }
            _render_query_footer(stats)
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "text": ms_answer.answer,
                    "question": prompt,
                    "ms_debug": payload,
                    "stats": stats,
                }
            )
        except IndexNotFoundError as exc:
            message = str(exc)
            st.error(message)
            st.session_state.messages.append({"role": "assistant", "text": message})
        except Exception as exc:
            if _is_key_auth_disabled_error(exc):
                message = _auth_remediation_message()
            else:
                message = f"Query failed: {exc}"
            st.error(message)
            st.session_state.messages.append({"role": "assistant", "text": message})


def main() -> None:
    args = _parse_cli()
    default_backend = str(args.backend or "lazy").lower()
    if default_backend not in {"lazy", "mini", "ms", "agentic", "nav"}:
        default_backend = "lazy"

    st.set_page_config(page_title="KE WebUI", page_icon="🧩", layout="wide")
    st.title("KE WebUI")

    settings = get_settings()
    settings.ensure_dirs()

    page = st.sidebar.radio("Page", ["Telemetry", "Chat"], index=0)
    if page == "Telemetry":
        _render_telemetry_page(settings)
    else:
        _render_chat_page(settings, default_backend)


if __name__ == "__main__":
    main()
