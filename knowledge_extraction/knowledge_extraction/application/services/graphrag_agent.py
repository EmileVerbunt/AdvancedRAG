"""Small reusable GraphRAG-style retrieval agent.

This module is intentionally lightweight so it can later be wrapped by MCP or
an HTTP API without changing the retrieval core.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from knowledge_extraction.application.services.query_rewriter import reciprocal_rank_fusion

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_DATE_PATTERN = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+20\d{2}\b", re.I)
_MONTH_YEAR_PATTERN = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+20\d{2}\b", re.I)
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "into", "about", "what", "when", "where", "which", "have", "has", "had",
    "not", "you", "your", "our", "their", "can", "could", "should", "would",
    "how", "why", "who", "whom", "whose", "then", "than", "also", "using",
    "did", "come", "out",
}


@dataclass(slots=True)
class RetrievalHit:
    kind: str
    id: str
    score: float
    text: str
    meta: dict[str, object]


@dataclass(slots=True)
class GraphContext:
    node_id: str
    label: str
    node_type: str
    neighbors: list[dict[str, object]]


@dataclass(slots=True)
class RetrievalResult:
    question: str
    query_terms: list[str]
    hits: list[RetrievalHit]
    graph_context: list[GraphContext]

    def to_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "query_terms": self.query_terms,
            "hits": [asdict(h) for h in self.hits],
            "graph_context": [asdict(c) for c in self.graph_context],
        }


class MiniGraphRagAgent:
    """Keyword-overlap retrieval over claims, entities, tables, and figures."""

    def __init__(
        self,
        sqlite_path: Path,
        graph_dir: Path,
        source_pdf: Path | None = None,
        source_markdown: Path | None = None,
    ) -> None:
        self._sqlite_path = sqlite_path
        self._graph_dir = graph_dir
        self._source_pdf = source_pdf
        self._source_markdown = source_markdown

    def ask(
        self,
        question: str,
        *,
        top_k: int = 8,
        include_graph: bool = True,
        max_neighbors: int = 5,
    ) -> RetrievalResult:
        terms = _terms(question)
        if not terms:
            return RetrievalResult(question=question, query_terms=[], hits=[], graph_context=[])

        temporal_query = _is_temporal_question(question)
        hits = self._retrieve_hits(terms, top_k=top_k)
        if temporal_query:
            _apply_temporal_bonus(hits)
            hits.sort(key=lambda h: h.score, reverse=True)
        if self._needs_fallback(terms, hits) or (temporal_query and not _has_date_evidence(hits)):
            hits.extend(self._markdown_candidates(terms, top_k=top_k))
            hits.extend(self._pdf_page_candidates(terms, top_k=top_k))
            if temporal_query:
                _apply_temporal_bonus(hits)
            hits.sort(key=lambda h: h.score, reverse=True)
            hits = hits[:max(1, top_k)]
        graph_context: list[GraphContext] = []
        if include_graph and hits:
            graph_context = self._graph_context(hits, max_neighbors=max_neighbors)
        return RetrievalResult(
            question=question,
            query_terms=sorted(terms),
            hits=hits,
            graph_context=graph_context,
        )

    def ask_multi(
        self,
        queries: list[str],
        *,
        top_k: int = 8,
        include_graph: bool = True,
        max_neighbors: int = 5,
    ) -> RetrievalResult:
        """Run retrieval for each query, fuse with Reciprocal Rank Fusion.

        Use this with a :class:`QueryRewriter` to combine the original question
        with paraphrases — variants surface different chunks; RRF picks the items
        ranked highly by *any* variant. Graph context is computed once on the
        final fused hits.
        """
        if not queries:
            return RetrievalResult(question="", query_terms=[], hits=[], graph_context=[])
        if len(queries) == 1:
            return self.ask(
                queries[0],
                top_k=top_k,
                include_graph=include_graph,
                max_neighbors=max_neighbors,
            )

        # Over-fetch per variant so RRF has more raw signal to fuse.
        per_variant_top_k = max(top_k * 2, top_k + 4)
        per_query_hits: list[list[RetrievalHit]] = []
        union_terms: set[str] = set()
        for q in queries:
            result = self.ask(q, top_k=per_variant_top_k, include_graph=False)
            per_query_hits.append(result.hits)
            union_terms.update(result.query_terms)

        fused = reciprocal_rank_fusion(
            per_query_hits,
            key=lambda h: f"{h.kind}:{h.id}",
        )[: max(1, top_k)]

        graph_context: list[GraphContext] = []
        if include_graph and fused:
            graph_context = self._graph_context(fused, max_neighbors=max_neighbors)

        return RetrievalResult(
            question=queries[0],
            query_terms=sorted(union_terms),
            hits=fused,
            graph_context=graph_context,
        )

    def _retrieve_hits(self, query_terms: set[str], *, top_k: int) -> list[RetrievalHit]:
        if not self._sqlite_path.exists():
            return []

        con = sqlite3.connect(str(self._sqlite_path))
        con.row_factory = sqlite3.Row
        try:
            candidates: list[RetrievalHit] = []
            candidates.extend(self._claim_candidates(con, query_terms))
            if _table_exists(con, "relationships"):
                candidates.extend(self._relationship_candidates(con, query_terms))
            candidates.extend(self._table_candidates(con, query_terms))
            candidates.extend(self._figure_candidates(con, query_terms))
            candidates.extend(self._entity_candidates(con, query_terms))
            if _table_exists(con, "chunks"):
                candidates.extend(self._chunk_candidates(con, query_terms))
            candidates.sort(key=lambda h: h.score, reverse=True)
            return candidates[:max(1, top_k)]
        finally:
            con.close()

    def _claim_candidates(self, con: sqlite3.Connection, query_terms: set[str]) -> list[RetrievalHit]:
        rows = con.execute(
            "SELECT id, text, confidence, supporting_table_id, supporting_figure_id FROM claims"
        ).fetchall()
        out: list[RetrievalHit] = []
        for r in rows:
            txt = str(r["text"] or "")
            score = _score(query_terms, txt, kind="claim")
            if score <= 0:
                continue
            out.append(
                RetrievalHit(
                    kind="claim",
                    id=str(r["id"]),
                    score=score,
                    text=_trim(txt, 240),
                    meta={
                        "confidence": float(r["confidence"] or 0.0),
                        "supporting_table_id": r["supporting_table_id"],
                        "supporting_figure_id": r["supporting_figure_id"],
                    },
                )
            )
        return out

    def _markdown_candidates(self, query_terms: set[str], *, top_k: int) -> list[RetrievalHit]:
        if self._source_markdown is None or not self._source_markdown.exists():
            return []
        lines = self._source_markdown.read_text(encoding="utf-8", errors="ignore").splitlines()
        out: list[RetrievalHit] = []
        seen: set[tuple[int, int]] = set()
        for idx in range(len(lines)):
            start = max(0, idx - 2)
            end = min(len(lines), idx + 3)
            key = (start, end)
            if key in seen:
                continue
            block = "\n".join(lines[start:end]).strip()
            if not block:
                continue
            score = _score(query_terms, block, kind="markdown")
            if score <= 0:
                continue
            seen.add(key)
            out.append(
                RetrievalHit(
                    kind="markdown",
                    id=f"md:{idx + 1}",
                    score=score,
                    text=_trim_around_terms(block, query_terms, 520),
                    meta={"line_start": start + 1, "line_end": end, "source_markdown": str(self._source_markdown)},
                )
            )
        out.sort(key=lambda h: h.score, reverse=True)
        return out[:max(1, top_k)]

    def _table_candidates(self, con: sqlite3.Connection, query_terms: set[str]) -> list[RetrievalHit]:
        rows = con.execute(
            """
            SELECT
                t.id,
                t.caption,
                t.page,
                t.page_end,
                (
                    SELECT GROUP_CONCAT(text, ' | ')
                    FROM (
                        SELECT text
                        FROM table_cells tc
                        WHERE tc.table_id = t.id
                        ORDER BY tc.row_index, tc.column_index
                        LIMIT 30
                    )
                ) AS cell_text
            FROM tables t
            """
        ).fetchall()
        out: list[RetrievalHit] = []
        for r in rows:
            combined = f"{r['caption'] or ''} {r['cell_text'] or ''}".strip()
            score = _score(query_terms, combined, kind="table")
            if score <= 0:
                continue
            out.append(
                RetrievalHit(
                    kind="table",
                    id=str(r["id"]),
                    score=score,
                    text=_trim(combined, 240),
                    meta={"page": r["page"], "page_end": r["page_end"]},
                )
            )
        return out

    def _relationship_candidates(self, con: sqlite3.Connection, query_terms: set[str]) -> list[RetrievalHit]:
        rows = con.execute(
            """
            SELECT
                r.id,
                r.source_id,
                r.target_id,
                r.type,
                r.confidence,
                r.chunk_id,
                se.name AS source_name,
                se.type AS source_type,
                te.name AS target_name,
                te.type AS target_type,
                sc.text AS source_claim_text,
                tc.text AS target_claim_text,
                sf.interpretation_title AS source_figure_title,
                tf.interpretation_title AS target_figure_title,
                st.caption AS source_table_caption,
                tt.caption AS target_table_caption
            FROM relationships r
            LEFT JOIN entities se ON se.id = r.source_id
            LEFT JOIN entities te ON te.id = r.target_id
            LEFT JOIN claims sc ON sc.id = r.source_id
            LEFT JOIN claims tc ON tc.id = r.target_id
            LEFT JOIN figures sf ON sf.id = r.source_id
            LEFT JOIN figures tf ON tf.id = r.target_id
            LEFT JOIN tables st ON st.id = r.source_id
            LEFT JOIN tables tt ON tt.id = r.target_id
            """
        ).fetchall()
        out: list[RetrievalHit] = []
        for r in rows:
            source = str(
                r["source_name"]
                or r["source_claim_text"]
                or r["source_figure_title"]
                or r["source_table_caption"]
                or r["source_id"]
                or ""
            )
            target = str(
                r["target_name"]
                or r["target_claim_text"]
                or r["target_figure_title"]
                or r["target_table_caption"]
                or r["target_id"]
                or ""
            )
            rel_type = str(r["type"] or "")
            combined = f"{source} ({r['source_type'] or ''}) --{rel_type}--> {target} ({r['target_type'] or ''})".strip()
            score = _score(query_terms, combined, kind="relationship")
            rel_type_lower = rel_type.lower()
            if "supports" in query_terms and "claim" in query_terms and "supports_claim" in rel_type_lower:
                score += 0.45
            if "released" in query_terms and "released_by" in rel_type_lower:
                score += 0.35
            if score <= 0:
                continue
            out.append(
                RetrievalHit(
                    kind="relationship",
                    id=str(r["id"]),
                    score=score,
                    text=_trim(combined, 280),
                    meta={
                        "relationship_type": rel_type,
                        "source_id": r["source_id"],
                        "target_id": r["target_id"],
                        "chunk_id": r["chunk_id"],
                        "confidence": float(r["confidence"] or 0.0),
                    },
                )
            )
        return out

    def _figure_candidates(self, con: sqlite3.Connection, query_terms: set[str]) -> list[RetrievalHit]:
        rows = con.execute(
            """
            SELECT
                id,
                page,
                caption,
                interpretation_title,
                interpretation_chart_type,
                interpretation_confidence
            FROM figures
            """
        ).fetchall()
        out: list[RetrievalHit] = []
        for r in rows:
            combined = " ".join(
                part
                for part in (
                    r["caption"],
                    r["interpretation_title"],
                    r["interpretation_chart_type"],
                )
                if part
            )
            score = _score(query_terms, combined, kind="figure")
            if score <= 0:
                continue
            out.append(
                RetrievalHit(
                    kind="figure",
                    id=str(r["id"]),
                    score=score,
                    text=_trim(combined, 240),
                    meta={
                        "page": r["page"],
                        "chart_type": r["interpretation_chart_type"],
                        "confidence": r["interpretation_confidence"],
                    },
                )
            )
        return out

    def _entity_candidates(self, con: sqlite3.Connection, query_terms: set[str]) -> list[RetrievalHit]:
        rows = con.execute("SELECT id, name, type, confidence FROM entities").fetchall()
        out: list[RetrievalHit] = []
        for r in rows:
            combined = f"{r['name'] or ''} {r['type'] or ''}".strip()
            score = _score(query_terms, combined, kind="entity")
            if score <= 0:
                continue
            out.append(
                RetrievalHit(
                    kind="entity",
                    id=str(r["id"]),
                    score=score,
                    text=_trim(combined, 160),
                    meta={"entity_type": r["type"], "confidence": float(r["confidence"] or 0.0)},
                )
            )
        return out

    def _chunk_candidates(self, con: sqlite3.Connection, query_terms: set[str]) -> list[RetrievalHit]:
        rows = con.execute(
            """
            SELECT id, text, page_start, page_end, figure_refs_json, table_refs_json
            FROM chunks
            """
        ).fetchall()
        out: list[RetrievalHit] = []
        for r in rows:
            txt = str(r["text"] or "")
            score = _score(query_terms, txt, kind="chunk")
            if score <= 0:
                continue
            out.append(
                RetrievalHit(
                    kind="chunk",
                    id=str(r["id"]),
                    score=score,
                    text=_trim_around_terms(txt, query_terms, 520),
                    meta={
                        "page_start": r["page_start"],
                        "page_end": r["page_end"],
                        "figure_refs_json": r["figure_refs_json"],
                        "table_refs_json": r["table_refs_json"],
                    },
                )
            )
        return out

    def _needs_fallback(self, query_terms: set[str], hits: list[RetrievalHit]) -> bool:
        has_markdown = self._source_markdown is not None and self._source_markdown.exists()
        has_pdf = self._source_pdf is not None and self._source_pdf.exists()
        if not has_markdown and not has_pdf:
            return False
        if not hits:
            return True
        corpus = " ".join(h.text.lower() for h in hits)
        focus_terms = [t for t in query_terms if (len(t) >= 6 or any(ch.isdigit() for ch in t))]
        if not focus_terms:
            return False
        return not any(t in corpus for t in focus_terms)

    def _pdf_page_candidates(self, query_terms: set[str], *, top_k: int) -> list[RetrievalHit]:
        if self._source_pdf is None or not self._source_pdf.exists():
            return []
        try:
            import pypdfium2 as pdfium
        except Exception:
            return []
        pdf = pdfium.PdfDocument(str(self._source_pdf))
        out: list[RetrievalHit] = []
        try:
            for page_idx in range(len(pdf)):
                page = pdf[page_idx]
                text_page = page.get_textpage()
                text = text_page.get_text_range() or ""
                score = _score(query_terms, text, kind="pdf_page")
                if score <= 0:
                    continue
                out.append(
                    RetrievalHit(
                        kind="pdf_page",
                        id=f"pdf:{page_idx + 1}",
                        score=score,
                        text=_trim_around_terms(text, query_terms, 520),
                        meta={"page": page_idx + 1, "source_pdf": str(self._source_pdf)},
                    )
                )
        finally:
            pdf.close()
        out.sort(key=lambda h: h.score, reverse=True)
        return out[:max(1, top_k)]

    def _graph_context(self, hits: list[RetrievalHit], *, max_neighbors: int) -> list[GraphContext]:
        latest = _latest_graphml(self._graph_dir)
        if latest is None:
            return []
        try:
            import networkx as nx
        except Exception:
            return []
        graph = nx.read_graphml(latest)

        out: list[GraphContext] = []
        seen: set[str] = set()
        for hit in hits:
            node_id = hit.id
            if node_id in seen or node_id not in graph:
                continue
            seen.add(node_id)
            node_data = graph.nodes[node_id]
            neighbors: list[dict[str, object]] = []
            for nb in list(graph.neighbors(node_id))[:max_neighbors]:
                edge_types = _edge_types(graph, node_id, nb)
                neighbors.append(
                    {
                        "id": str(nb),
                        "label": str(graph.nodes[nb].get("label", nb)),
                        "type": str(graph.nodes[nb].get("type", "")),
                        "edge_types": edge_types,
                    }
                )
            out.append(
                GraphContext(
                    node_id=node_id,
                    label=str(node_data.get("label", node_id)),
                    node_type=str(node_data.get("type", hit.kind)),
                    neighbors=neighbors,
                )
            )
        return out


def _latest_graphml(graph_dir: Path) -> Path | None:
    if not graph_dir.exists():
        return None
    files = sorted(graph_dir.glob("*.graphml"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _edge_types(graph, source: str, target: str) -> list[str]:
    data: Any = graph.get_edge_data(source, target, default={})
    edge_types: list[str] = []
    if isinstance(data, dict):
        # MultiGraph returns {key: attrs}, Graph returns attrs directly.
        if "type" in data:
            edge_types.append(str(data.get("type")))
        else:
            for attrs in data.values():
                if isinstance(attrs, dict) and "type" in attrs:
                    edge_types.append(str(attrs.get("type")))
    return sorted(set(edge_types))


def _terms(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _score(query_terms: set[str], text: str, *, kind: str) -> float:
    if not text:
        return 0.0
    text_terms = _terms(text)
    if not text_terms:
        return 0.0
    overlap = _overlap_weight(query_terms, text_terms)
    if overlap <= 0:
        return 0.0
    kind_bonus = {
        "chunk": 0.45,
        "markdown": 0.5,
        "relationship": 0.58,
        "pdf_page": 0.4,
        "table": 0.35,
        "claim": 0.3,
        "figure": 0.22,
        "entity": 0.15,
    }.get(kind, 0.1)
    return (overlap / max(1, len(query_terms))) + min(overlap, 3) * 0.08 + kind_bonus


def _trim(text: str, max_chars: int) -> str:
    t = " ".join(text.split())
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1] + "…"


def _trim_around_terms(text: str, terms: set[str], max_chars: int) -> str:
    t = " ".join(text.split())
    if len(t) <= max_chars:
        return t
    lower = t.lower()
    anchor = -1
    found: list[tuple[int, int]] = []
    for term in terms:
        term_l = term.lower()
        pos = lower.find(term_l)
        if pos < 0:
            continue
        freq = lower.count(term_l)
        found.append((freq, pos))
    if found:
        found.sort(key=lambda x: (x[0], x[1]))
        anchor = found[0][1]
    if anchor < 0:
        return _trim(t, max_chars)
    half = max_chars // 2
    start = max(0, anchor - half)
    end = min(len(t), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    snippet = t[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(t):
        snippet = snippet + "…"
    return snippet


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _overlap_weight(query_terms: set[str], text_terms: set[str]) -> float:
    exact = query_terms & text_terms
    score = float(len(exact))
    unresolved = query_terms - exact
    if not unresolved or not text_terms:
        return score
    for q in unresolved:
        fuzzy = _best_fuzzy_match(q, text_terms)
        if fuzzy >= 86:
            score += 0.9
        elif fuzzy >= 80:
            score += 0.7
    return score


def _best_fuzzy_match(query_term: str, text_terms: set[str]) -> int:
    if len(query_term) < 5:
        return 0
    try:
        from rapidfuzz import fuzz
    except Exception:
        return 0
    candidates = [
        t for t in text_terms
        if abs(len(t) - len(query_term)) <= 2 and t[:1] == query_term[:1]
    ]
    if not candidates:
        return 0
    return max(int(fuzz.ratio(query_term, c)) for c in candidates)


def _is_temporal_question(question: str) -> bool:
    q = question.lower()
    return any(tok in q for tok in ("when", "release date", "released", "come out", "launched"))


def _contains_date(text: str) -> bool:
    return bool(_DATE_PATTERN.search(text) or _MONTH_YEAR_PATTERN.search(text))


def _has_date_evidence(hits: list[RetrievalHit]) -> bool:
    return any(_contains_date(h.text) for h in hits)


def _apply_temporal_bonus(hits: list[RetrievalHit]) -> None:
    for hit in hits:
        if _DATE_PATTERN.search(hit.text):
            hit.score += 0.45
        elif _MONTH_YEAR_PATTERN.search(hit.text):
            hit.score += 0.25

