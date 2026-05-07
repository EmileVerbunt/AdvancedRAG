"""Streamlit pipeline tour — launched via `ke tour`.

Reads per-run JSONL logs from work/logs/, plus the relational + graph artifacts,
and walks through every pipeline step with timings, tokens, and samples.

Run via: `uv run ke tour` (which exec's `streamlit run` on this file).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import networkx as nx
import pandas as pd
import streamlit as st
import yaml

# --------------------------------------------------------------------------------------- #
# Defaults / pricing                                                                       #
# --------------------------------------------------------------------------------------- #

DEFAULT_LOG_DIR = Path("./work/logs")
DEFAULT_GRAPH_DIR = Path("./work/graph")
DEFAULT_ARTIFACT_DIR = Path("./work/artifacts")
DEFAULT_ONTOLOGY_DIR = Path("./ontology")

# Illustrative; override via sidebar.
DEFAULT_PRICES = {
    "gpt-5.4":              {"in": 1.25, "out": 10.00},   # per 1M tokens
    "gpt-5.4-mini":         {"in": 0.25, "out":  2.00},
    "text-embedding-ada-002": {"in": 0.10, "out": 0.0},
    "text-embedding-3-large": {"in": 0.13, "out": 0.0},
}


# --------------------------------------------------------------------------------------- #
# Loading                                                                                  #
# --------------------------------------------------------------------------------------- #

@st.cache_data(show_spinner=False)
def list_runs(log_dir: Path) -> list[Path]:
    if not log_dir.exists():
        return []
    return sorted(log_dir.glob("run-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


@st.cache_data(show_spinner=False)
def load_run(path: Path) -> dict[str, Any]:
    """Parse a per-run JSONL into a structured bundle."""
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    by_event: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        ev = r.get("event")
        if ev:
            by_event.setdefault(ev, []).append(r)

    start = next((r for r in records if r.get("event") == "run.start"), {})
    finish = next((r for r in records if r.get("event") == "run.finish"), {})

    return {
        "path": path,
        "records": records,
        "by_event": by_event,
        "run_id": start.get("run_id") or (records[0].get("run_id") if records else None),
        "command": start.get("command", "—"),
        "argv": start.get("argv", ""),
        "started_at": start.get("started_at") or (records[0].get("ts") if records else None),
        "duration_ms": finish.get("duration_ms"),
        "mode": next((r.get("mode") for r in records if r.get("mode")), None),
        "document_id": next((r.get("document_id") for r in records if r.get("document_id")), None),
        "pdf": next((r.get("pdf") for r in records if r.get("pdf")), None),
        "ontology_version": next(
            (r.get("ontology_version") for r in records if r.get("ontology_version")), None,
        ),
    }


def fmt_ms(ms: int | float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{int(ms)} ms"
    return f"{ms / 1000:.2f} s"


def cost_usd(model: str, in_tok: int, out_tok: int, prices: dict[str, dict[str, float]]) -> float:
    p = prices.get(model)
    if not p:
        return 0.0
    return (in_tok / 1_000_000) * p["in"] + (out_tok / 1_000_000) * p["out"]


# --------------------------------------------------------------------------------------- #
# Pages                                                                                    #
# --------------------------------------------------------------------------------------- #

def page_overview(bundle: dict[str, Any], prices: dict[str, dict[str, float]]) -> None:
    st.header("Overview")
    st.caption("Each `ke` invocation produces one JSONL log. Pick one in the sidebar to tour.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Run ID", bundle["run_id"] or "—")
    c2.metric("Command", bundle["command"])
    c3.metric("Mode", bundle["mode"] or "—")
    c4.metric("Total wallclock", fmt_ms(bundle["duration_ms"]))

    c5, c6, c7, c8 = st.columns(4)
    llm_calls = bundle["by_event"].get("llm.complete_json", [])
    in_tok = sum(r.get("input_tokens", 0) for r in llm_calls)
    out_tok = sum(r.get("output_tokens", 0) for r in llm_calls)
    total_cost = sum(
        cost_usd(r.get("model", ""), r.get("input_tokens", 0), r.get("output_tokens", 0), prices)
        for r in llm_calls
    )
    c5.metric("LLM calls", len(llm_calls))
    c6.metric("Σ input tokens", f"{in_tok:,}")
    c7.metric("Σ output tokens", f"{out_tok:,}")
    c8.metric("Estimated cost", f"${total_cost:.4f}")

    st.subheader("Why this matters")
    st.markdown(
        """
        **Wide events** capture every logical step with full context. One JSON line per operation,
        bound to `run_id` / `command` / `mode` / `document_id` / `ontology_version`.
        Scrub the sidebar steps to see *what* happened, *how long* it took, *what tokens cost*,
        and *what was extracted* — without needing to re-run anything.
        """
    )

    st.subheader("Stage timeline")
    stages = bundle["by_event"].get("pipeline.stage", [])
    if stages:
        df = pd.DataFrame([
            {"stage": r.get("stage", "?"), "duration_ms": r.get("duration_ms", 0),
             "status": r.get("status", "?")}
            for r in stages
        ])
        st.bar_chart(df.set_index("stage")["duration_ms"])
        st.dataframe(df, use_container_width=True)
    else:
        st.info("No `pipeline.stage` events in this run (e.g. `stats` command).")

    with st.expander("Raw run.start / run.finish"):
        st.json({
            "run.start": bundle["by_event"].get("run.start", [{}])[0],
            "run.finish": bundle["by_event"].get("run.finish", [{}])[0],
        })


def page_ingest(bundle: dict[str, Any]) -> None:
    st.header("📄 Ingest")
    st.caption(
        "Docling parses the PDF into structured Markdown, preserving sections, tables, and "
        "page anchors. The slice step keeps long PDFs cheap during smoke runs."
    )
    slices = bundle["by_event"].get("ingest.slice_pdf", [])
    docling = bundle["by_event"].get("ingest.docling", [])

    cols = st.columns(2)
    if slices:
        s = slices[0]
        with cols[0]:
            st.subheader("Slice")
            st.metric("Pages kept", s.get("pages", "—"))
            st.metric("Slice duration", fmt_ms(s.get("duration_ms")))
            st.metric("Slice bytes", f"{s.get('bytes', 0):,}")
            st.code(s.get("destination", ""), language=None)
    if docling:
        d = docling[0]
        with cols[1]:
            st.subheader("Docling")
            st.metric("Pages parsed", d.get("pages", "—"))
            st.metric("Markdown chars", f"{d.get('markdown_chars', 0):,}")
            st.metric("Duration", fmt_ms(d.get("duration_ms")))
            st.metric("Document ID", d.get("document_id", "—"))

    # Try to surface the markdown produced
    pdf = bundle.get("pdf")
    if pdf:
        stem = Path(pdf).stem
        md_path = DEFAULT_ARTIFACT_DIR / stem / "doc.md"
        if md_path.exists():
            st.subheader("Markdown excerpt")
            md = md_path.read_text(encoding="utf-8")
            preview_chars = st.slider("Preview characters", 200, min(20_000, len(md)), 2000, 200)
            st.markdown(md[:preview_chars])
        pages_dir = DEFAULT_ARTIFACT_DIR / stem / "pages"
        if pages_dir.exists():
            st.subheader("Page renders")
            pngs = sorted(pages_dir.glob("*.png"))[:6]
            if pngs:
                st.image([str(p) for p in pngs], width=200)


def page_chunk(bundle: dict[str, Any]) -> None:
    st.header("✂️ Chunking")
    st.caption(
        "Semantic chunker respects section boundaries from Docling so each chunk is a coherent "
        "unit. Chunk count drives LLM cost — too many small chunks = redundant calls; too few "
        "large chunks = lossy extraction."
    )
    ev = bundle["by_event"].get("chunk.semantic", [])
    if not ev:
        st.info("No chunking event in this run.")
        return
    e = ev[0]
    c1, c2, c3 = st.columns(3)
    c1.metric("Chunks produced", e.get("chunks", 0))
    c2.metric("Sections detected", e.get("sections", 0))
    c3.metric("Duration", fmt_ms(e.get("duration_ms")))


def page_extract(bundle: dict[str, Any], prices: dict[str, dict[str, float]]) -> None:
    st.header("🧠 Extraction")
    st.caption(
        "Per chunk, the LLM is prompted to emit structured JSON: entities (canonicalised), "
        "relationships (typed edges with evidence spans), and claims (assertions with confidence). "
        "Governed mode constrains to the active ontology; discovery mode is free-form."
    )
    llm = bundle["by_event"].get("llm.complete_json", [])
    chunks = bundle["by_event"].get("extract.chunk", [])
    if not chunks:
        st.info("No `extract.chunk` events in this run.")
        return

    # Aggregate
    df = pd.DataFrame([
        {
            "chunk_id": r.get("chunk_id", "?")[:8],
            "page_start": r.get("page_start"),
            "page_end": r.get("page_end"),
            "char_count": r.get("char_count", 0),
            "entities": r.get("entities", 0),
            "relationships": r.get("relationships", 0),
            "claims": r.get("claims", 0),
            "input_tokens": r.get("input_tokens", 0),
            "output_tokens": r.get("output_tokens", 0),
            "duration_ms": r.get("duration_ms", 0),
            "cached": r.get("cached", False),
        }
        for r in chunks
    ])
    st.subheader("Per-chunk summary")
    st.dataframe(df, use_container_width=True)

    st.subheader("Drill into a chunk")
    options = [f"{i}: {row.chunk_id}" for i, row in df.iterrows()]
    sel = st.selectbox("Pick a chunk", options, index=0)
    idx = int(sel.split(":")[0])
    chunk_event = chunks[idx]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Entities", chunk_event.get("entities", 0))
    c2.metric("Relationships", chunk_event.get("relationships", 0))
    c3.metric("Claims", chunk_event.get("claims", 0))
    c4.metric("Latency", fmt_ms(chunk_event.get("duration_ms")))

    # Find matching llm event by chunk position (1:1 ordering)
    if idx < len(llm):
        llm_ev = llm[idx]
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Model", llm_ev.get("model", "—"))
        c6.metric("Input tokens", f"{llm_ev.get('input_tokens', 0):,}")
        c7.metric("Output tokens", f"{llm_ev.get('output_tokens', 0):,}")
        c8.metric(
            "Cost",
            f"${cost_usd(llm_ev.get('model', ''), llm_ev.get('input_tokens', 0), llm_ev.get('output_tokens', 0), prices):.5f}",
        )
        with st.expander("Wide event for this LLM call"):
            st.json(llm_ev)

    with st.expander("Wide event for this chunk extraction"):
        st.json(chunk_event)

    st.subheader("Cost & latency over chunks")
    df["cost_usd"] = [
        cost_usd(llm[i].get("model", "") if i < len(llm) else "",
                 r.get("input_tokens", 0), r.get("output_tokens", 0), prices)
        for i, r in enumerate(chunks)
    ]
    chart_cols = st.columns(2)
    chart_cols[0].bar_chart(df.set_index("chunk_id")["duration_ms"])
    chart_cols[1].bar_chart(df.set_index("chunk_id")["cost_usd"])


def page_graph(bundle: dict[str, Any]) -> None:
    st.header("🕸️ Knowledge Graph")
    st.caption(
        "All extracted entities and relationships are merged into a single NetworkX graph, "
        "exported as GraphML, JSON-LD and Cypher for downstream tooling."
    )
    exports = bundle["by_event"].get("graph.export", [])
    if exports:
        df = pd.DataFrame([
            {"format": r.get("format", "?"), "nodes": r.get("nodes", 0),
             "edges": r.get("edges", 0), "bytes": r.get("bytes", 0),
             "duration_ms": r.get("duration_ms", 0)}
            for r in exports
        ])
        st.subheader("Graph exports")
        st.dataframe(df, use_container_width=True)

    # Try to load the GraphML for an actual visual
    graphml = DEFAULT_GRAPH_DIR / "knowledge_graph.graphml"
    if graphml.exists():
        try:
            graph = nx.read_graphml(graphml)
        except Exception as exc:
            st.warning(f"Could not parse GraphML: {exc}")
            return
        c1, c2, c3 = st.columns(3)
        c1.metric("Nodes", graph.number_of_nodes())
        c2.metric("Edges", graph.number_of_edges())
        c3.metric("Components", nx.number_connected_components(graph.to_undirected()))

        st.subheader("Top entities by degree")
        deg = sorted(graph.degree, key=lambda x: x[1], reverse=True)[:20]
        rows = []
        for node_id, d in deg:
            attrs = dict(graph.nodes[node_id])
            rows.append({"id": node_id, "name": attrs.get("name", node_id),
                         "type": attrs.get("type", "?"), "degree": d})
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

        st.subheader("Entity type distribution")
        types = Counter((graph.nodes[n].get("type") or "unknown") for n in graph.nodes)
        st.bar_chart(pd.DataFrame(types.most_common(), columns=["type", "count"]).set_index("type"))

        st.subheader("Edges (sample)")
        edge_rows = []
        for u, v, attrs in list(graph.edges(data=True))[:50]:
            edge_rows.append({"source": u, "target": v,
                              "type": attrs.get("type", "?"),
                              "confidence": attrs.get("confidence")})
        st.dataframe(pd.DataFrame(edge_rows), use_container_width=True)
    else:
        st.info(f"GraphML not found at {graphml}. Run an extract first.")


def page_ontology(bundle: dict[str, Any]) -> None:
    st.header("📚 Ontology")
    st.caption(
        "The ontology is the schema the governed pipeline extracts against. "
        "Discovery mode produces *candidate* ontologies that need human approval."
    )
    onto = bundle["by_event"].get("ontology.bootstrap", [])
    if onto:
        st.metric("Bootstrap duration", fmt_ms(onto[0].get("duration_ms")))
    if bundle.get("ontology_version"):
        st.metric("Active version", bundle["ontology_version"])

    if DEFAULT_ONTOLOGY_DIR.exists():
        files = sorted(DEFAULT_ONTOLOGY_DIR.glob("*.yaml"))
        if files:
            sel = st.selectbox("Inspect ontology file", [f.name for f in files])
            f = next(p for p in files if p.name == sel)
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                st.json(data)
            except yaml.YAMLError as exc:
                st.error(str(exc))
                st.code(f.read_text(encoding="utf-8"), language="yaml")


def page_cost(bundle: dict[str, Any], prices: dict[str, dict[str, float]]) -> None:
    st.header("💰 Cost & Timings")
    st.caption("All numbers below are derived from the JSONL log — no re-runs needed.")

    llm = bundle["by_event"].get("llm.complete_json", [])
    if llm:
        df = pd.DataFrame([
            {
                "model": r.get("model", "?"),
                "input_tokens": r.get("input_tokens", 0),
                "output_tokens": r.get("output_tokens", 0),
                "latency_ms": r.get("latency_ms", 0),
                "cost_usd": cost_usd(r.get("model", ""), r.get("input_tokens", 0), r.get("output_tokens", 0), prices),
            }
            for r in llm
        ])
        st.subheader("LLM calls")
        st.dataframe(df, use_container_width=True)
        agg = df.groupby("model")[["input_tokens", "output_tokens", "latency_ms", "cost_usd"]].sum()
        st.subheader("By model")
        st.dataframe(agg, use_container_width=True)

    embed = bundle["by_event"].get("embedding.embed", [])
    if embed:
        st.subheader("Embedding calls")
        edf = pd.DataFrame([
            {
                "batch_size": r.get("batch_size", 0),
                "dims": r.get("dims", 0),
                "input_tokens": r.get("input_tokens", 0),
                "duration_ms": r.get("duration_ms", 0),
            }
            for r in embed
        ])
        st.dataframe(edf, use_container_width=True)

    st.subheader("All wide events")
    df_all = pd.DataFrame([
        {"event": r.get("event", "?"), "stage": r.get("stage"),
         "duration_ms": r.get("duration_ms"), "status": r.get("status")}
        for r in bundle["records"] if r.get("event")
    ])
    st.dataframe(df_all, use_container_width=True)


# --------------------------------------------------------------------------------------- #
# App entry                                                                                #
# --------------------------------------------------------------------------------------- #

def main() -> None:
    st.set_page_config(page_title="Knowledge Extraction — Pipeline Tour",
                       layout="wide", page_icon="🧭")
    st.title("🧭 Knowledge Extraction — Pipeline Tour")

    with st.sidebar:
        st.header("Run picker")
        log_dir = Path(st.text_input("Log directory", str(DEFAULT_LOG_DIR)))
        runs = list_runs(log_dir)
        if not runs:
            st.warning(f"No runs found in {log_dir}.")
            st.stop()
        sel = st.selectbox("Pick a run (newest first)",
                           options=list(range(len(runs))),
                           format_func=lambda i: runs[i].name)
        bundle = load_run(runs[sel])
        st.caption(f"records: {len(bundle['records'])}")

        st.divider()
        st.header("Pricing ($/1M tokens)")
        prices: dict[str, dict[str, float]] = {}
        for model, p in DEFAULT_PRICES.items():
            with st.expander(model, expanded=False):
                pin = st.number_input(f"{model} input", value=p["in"], step=0.05, key=f"in_{model}")
                pout = st.number_input(f"{model} output", value=p["out"], step=0.05, key=f"out_{model}")
                prices[model] = {"in": pin, "out": pout}

        st.divider()
        page = st.radio(
            "Steps",
            [
                "Overview",
                "📄 Ingest",
                "✂️ Chunking",
                "🧠 Extraction",
                "🕸️ Graph",
                "📚 Ontology",
                "💰 Cost & Timings",
            ],
        )

    if page == "Overview":
        page_overview(bundle, prices)
    elif page == "📄 Ingest":
        page_ingest(bundle)
    elif page == "✂️ Chunking":
        page_chunk(bundle)
    elif page == "🧠 Extraction":
        page_extract(bundle, prices)
    elif page == "🕸️ Graph":
        page_graph(bundle)
    elif page == "📚 Ontology":
        page_ontology(bundle)
    elif page == "💰 Cost & Timings":
        page_cost(bundle, prices)


if __name__ == "__main__":
    main()
