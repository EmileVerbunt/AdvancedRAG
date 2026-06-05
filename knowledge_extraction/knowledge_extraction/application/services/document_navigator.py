"""Document navigator — metadata catalog + on-demand document-reading tools.

This is the substrate for the ``nav`` ("Agentic Navigator") retrieval backend. It
deliberately works the way a human researcher would: first inspect lightweight
**metadata** about which documents exist, then **open** the actual document
artifact (``doc.md``) and drill into the relevant sections / tables / figures on
demand. There is no ahead-of-time vector index or query-time chunk fan-out — the
agent navigates the real document.

The navigator reads directly from the SQLite store (raw, read-only ``sqlite3``,
the same precedent as :class:`MiniGraphRagAgent` and ``agentic_index_available``)
plus the ``doc.md`` artifact on the filesystem. Every method is **schema and
filesystem tolerant**: missing tables, missing columns, or a missing ``doc.md``
all degrade gracefully (e.g. chunk-text fallback) instead of raising, so the
tools work against minimal / in-memory test databases too.

All tool methods return short, already-truncated **strings** so they can be fed
straight back into the LLM transcript as observations without blowing the
context window.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "into", "about", "what", "when", "where", "which", "have", "has", "had",
    "not", "you", "your", "our", "their", "can", "could", "should", "would",
    "how", "why", "who", "whom", "whose", "then", "than", "also", "using",
    "did", "does", "list", "show", "tell", "give",
}

_DEFAULT_MAX_CHARS = 4000
_DEFAULT_PREVIEW_CHARS = 600
_DEFAULT_MAX_CAPTIONS = 8


@dataclass(slots=True)
class DocMeta:
    """Lightweight metadata for one document — the routing view of the corpus.

    Intentionally cheap to build: counts + a few captions + a short preview, with
    no full-text payload, so the router LLM can pick candidate documents from
    metadata alone.
    """

    document_id: str
    title: str
    page_count: int
    n_chunks: int
    n_tables: int
    n_figures: int
    table_captions: list[str] = field(default_factory=list)
    figure_captions: list[str] = field(default_factory=list)
    preview: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "page_count": self.page_count,
            "n_chunks": self.n_chunks,
            "n_tables": self.n_tables,
            "n_figures": self.n_figures,
            "table_captions": list(self.table_captions),
            "figure_captions": list(self.figure_captions),
            "preview": self.preview,
        }


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _overlap_score(query_terms: set[str], text: str) -> float:
    if not query_terms:
        return 0.0
    text_terms = _tokens(text)
    if not text_terms:
        return 0.0
    return float(len(query_terms & text_terms))


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …[truncated]"


class DocumentNavigator:
    """Read-only catalog + navigation tools over the persisted knowledge store."""

    def __init__(
        self,
        sqlite_path: Path,
        artifact_path: Path,
        *,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self._sqlite_path = sqlite_path
        self._artifact_path = artifact_path
        self._max_chars = max_chars

    # ------------------------------------------------------------------ sqlite

    def _connect(self) -> sqlite3.Connection | None:
        if not self._sqlite_path.exists():
            return None
        try:
            con = sqlite3.connect(str(self._sqlite_path))
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.DatabaseError:
            return None

    @staticmethod
    def _table_exists(con: sqlite3.Connection, table: str) -> bool:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _columns(con: sqlite3.Connection, table: str) -> set[str]:
        try:
            return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.DatabaseError:
            return set()

    @staticmethod
    def _scalar_int(con: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> int:
        try:
            row = con.execute(sql, params).fetchone()
        except sqlite3.DatabaseError:
            return 0
        return int(row[0]) if row and row[0] is not None else 0

    # --------------------------------------------------------------- artifacts

    def _doc_md_path(self, source_path: str | None) -> Path | None:
        if not source_path:
            return None
        stem = Path(source_path).stem
        if not stem:
            return None
        candidate = self._artifact_path / stem / "doc.md"
        return candidate if candidate.exists() else None

    def _read_doc_md(self, source_path: str | None) -> str | None:
        path = self._doc_md_path(source_path)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    # ----------------------------------------------------------------- catalog

    def document_ids(self) -> list[str]:
        con = self._connect()
        if con is None:
            return []
        try:
            if not self._table_exists(con, "documents"):
                return []
            rows = con.execute("SELECT id FROM documents ORDER BY id").fetchall()
            return [str(r[0]) for r in rows]
        finally:
            con.close()

    def catalog(
        self,
        *,
        max_captions: int = _DEFAULT_MAX_CAPTIONS,
        preview_chars: int = _DEFAULT_PREVIEW_CHARS,
    ) -> list[DocMeta]:
        con = self._connect()
        if con is None or not self._table_exists(con, "documents"):
            if con is not None:
                con.close()
            return []
        try:
            doc_cols = self._columns(con, "documents")
            has_pc = "page_count" in doc_cols
            has_src = "source_path" in doc_cols
            rows = con.execute("SELECT * FROM documents ORDER BY id").fetchall()
            metas: list[DocMeta] = []
            for row in rows:
                doc_id = str(row["id"])
                source_path = str(row["source_path"]) if has_src and row["source_path"] else None
                title = str(row["title"]) if "title" in doc_cols and row["title"] else doc_id
                page_count = int(row["page_count"]) if has_pc and row["page_count"] is not None else 0
                n_chunks = self._scalar_int(
                    con, "SELECT COUNT(*) FROM chunks WHERE document_id=?", (doc_id,)
                ) if self._table_exists(con, "chunks") else 0
                table_caps = self._captions(con, "tables", doc_id, max_captions)
                figure_caps = self._captions(con, "figures", doc_id, max_captions)
                n_tables = self._scalar_int(
                    con, "SELECT COUNT(*) FROM tables WHERE document_id=?", (doc_id,)
                ) if self._has_doc_column(con, "tables") else len(table_caps)
                n_figures = self._scalar_int(
                    con, "SELECT COUNT(*) FROM figures WHERE document_id=?", (doc_id,)
                ) if self._has_doc_column(con, "figures") else len(figure_caps)
                preview = self._preview(con, doc_id, source_path, preview_chars)
                metas.append(
                    DocMeta(
                        document_id=doc_id,
                        title=title,
                        page_count=page_count,
                        n_chunks=n_chunks,
                        n_tables=n_tables,
                        n_figures=n_figures,
                        table_captions=table_caps,
                        figure_captions=figure_caps,
                        preview=preview,
                    )
                )
            return metas
        finally:
            con.close()

    @staticmethod
    def _has_doc_column(con: sqlite3.Connection, table: str) -> bool:
        if not DocumentNavigator._table_exists(con, table):
            return False
        return "document_id" in DocumentNavigator._columns(con, table)

    def _captions(
        self, con: sqlite3.Connection, table: str, doc_id: str, limit: int
    ) -> list[str]:
        if not self._table_exists(con, table):
            return []
        cols = self._columns(con, table)
        if "caption" not in cols:
            return []
        try:
            if "document_id" in cols:
                rows = con.execute(
                    f"SELECT caption FROM {table} WHERE document_id=? ORDER BY "
                    f"{'page' if 'page' in cols else 'id'} LIMIT ?",
                    (doc_id, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    f"SELECT caption FROM {table} LIMIT ?", (limit,)
                ).fetchall()
        except sqlite3.DatabaseError:
            return []
        return [str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()]

    def _preview(
        self, con: sqlite3.Connection, doc_id: str, source_path: str | None, limit: int
    ) -> str:
        doc_md = self._read_doc_md(source_path)
        if doc_md:
            # Skip leading blank lines / heading-only lines for a meatier preview.
            return _truncate(doc_md, limit)
        if self._table_exists(con, "chunks"):
            try:
                row = con.execute(
                    "SELECT text FROM chunks WHERE document_id=? ORDER BY page_start, id LIMIT 1",
                    (doc_id,),
                ).fetchone()
            except sqlite3.DatabaseError:
                row = None
            if row and row[0]:
                return _truncate(str(row[0]), limit)
        return ""

    # ------------------------------------------------------------- doc helpers

    def _doc_source(self, con: sqlite3.Connection, document_id: str) -> str | None:
        if not self._table_exists(con, "documents"):
            return None
        if "source_path" not in self._columns(con, "documents"):
            return None
        try:
            row = con.execute(
                "SELECT source_path FROM documents WHERE id=? LIMIT 1", (document_id,)
            ).fetchone()
        except sqlite3.DatabaseError:
            return None
        return str(row[0]) if row and row[0] else None

    def _doc_exists(self, con: sqlite3.Connection, document_id: str) -> bool:
        if not self._table_exists(con, "documents"):
            return False
        row = con.execute(
            "SELECT 1 FROM documents WHERE id=? LIMIT 1", (document_id,)
        ).fetchone()
        return row is not None

    # ----------------------------------------------------------------- tools

    def open_document(self, document_id: str) -> str:
        """Return the document outline (headings) — the table of contents."""
        con = self._connect()
        if con is None:
            return "error: no knowledge store available"
        try:
            if not self._doc_exists(con, document_id):
                return f"error: unknown document_id {document_id!r}"
            source = self._doc_source(con, document_id)
            doc_md = self._read_doc_md(source)
            if doc_md:
                headings = self._headings(doc_md)
                if headings:
                    lines = [f"{'  ' * (lvl - 1)}- {title}" for lvl, title, _ in headings]
                    return _truncate("Outline (headings):\n" + "\n".join(lines), self._max_chars)
            # Fallback: section ids + page ranges from chunks.
            return self._chunk_outline(con, document_id)
        finally:
            con.close()

    def _chunk_outline(self, con: sqlite3.Connection, document_id: str) -> str:
        if not self._table_exists(con, "chunks"):
            return "(no outline available — document has no doc.md and no chunks)"
        try:
            rows = con.execute(
                "SELECT section_id, MIN(page_start), MAX(page_end) FROM chunks "
                "WHERE document_id=? GROUP BY section_id ORDER BY MIN(page_start)",
                (document_id,),
            ).fetchall()
        except sqlite3.DatabaseError:
            return "(no outline available)"
        if not rows:
            return "(no outline available — document has no chunks)"
        lines = []
        for section_id, p0, p1 in rows:
            label = str(section_id) if section_id else "(unlabeled section)"
            lines.append(f"- {label} (pages {p0}-{p1})")
        return _truncate("Outline (sections from chunks):\n" + "\n".join(lines), self._max_chars)

    def read_section(self, document_id: str, query: str) -> str:
        """Return the body of the section whose heading best matches ``query``."""
        con = self._connect()
        if con is None:
            return "error: no knowledge store available"
        try:
            if not self._doc_exists(con, document_id):
                return f"error: unknown document_id {document_id!r}"
            source = self._doc_source(con, document_id)
            doc_md = self._read_doc_md(source)
            if doc_md:
                section = self._best_section(doc_md, query)
                if section:
                    return _truncate(section, self._max_chars)
            # Fallback: best matching chunks concatenated.
            return self._chunk_read(con, document_id, query)
        finally:
            con.close()

    def search_document(self, document_id: str, query: str, top_k: int = 3) -> str:
        """Lexically search within a single document and return top passages."""
        con = self._connect()
        if con is None:
            return "error: no knowledge store available"
        try:
            if not self._doc_exists(con, document_id):
                return f"error: unknown document_id {document_id!r}"
            top_k = max(1, min(int(top_k), 10))
            source = self._doc_source(con, document_id)
            doc_md = self._read_doc_md(source)
            terms = _tokens(query)
            if doc_md:
                passages = self._search_doc_md(doc_md, terms, top_k)
                if passages:
                    return _truncate("\n\n".join(passages), self._max_chars)
            return self._chunk_search(con, document_id, terms, top_k)
        finally:
            con.close()

    def get_table(self, document_id: str, table: str) -> str:
        con = self._connect()
        if con is None:
            return "error: no knowledge store available"
        try:
            if not self._doc_exists(con, document_id):
                return f"error: unknown document_id {document_id!r}"
            if not self._table_exists(con, "tables"):
                return "error: no tables stored for this corpus"
            cols = self._columns(con, "tables")
            row = self._pick_by_id_or_index(con, "tables", document_id, table, cols)
            if row is None:
                return f"error: table {table!r} not found in document {document_id!r}"
            caption = str(row["caption"]) if "caption" in cols and row["caption"] else ""
            page = row["page"] if "page" in cols else None
            markdown = str(row["markdown"]) if "markdown" in cols and row["markdown"] else ""
            header = f"Table {row['id']}" + (f" (page {page})" if page is not None else "")
            body = "\n".join(p for p in (caption, markdown) if p)
            return _truncate(f"{header}\n{body}".strip(), self._max_chars)
        finally:
            con.close()

    def get_figure(self, document_id: str, figure: str) -> str:
        con = self._connect()
        if con is None:
            return "error: no knowledge store available"
        try:
            if not self._doc_exists(con, document_id):
                return f"error: unknown document_id {document_id!r}"
            if not self._table_exists(con, "figures"):
                return "error: no figures stored for this corpus"
            cols = self._columns(con, "figures")
            row = self._pick_by_id_or_index(con, "figures", document_id, figure, cols)
            if row is None:
                return f"error: figure {figure!r} not found in document {document_id!r}"
            parts = [f"Figure {row['id']}"]
            if "page" in cols and row["page"] is not None:
                parts[0] += f" (page {row['page']})"
            if "caption" in cols and row["caption"]:
                parts.append(f"Caption: {row['caption']}")
            if "interpretation_title" in cols and row["interpretation_title"]:
                parts.append(f"Interpretation: {row['interpretation_title']}")
            if "interpretation_chart_type" in cols and row["interpretation_chart_type"]:
                parts.append(f"Chart type: {row['interpretation_chart_type']}")
            if "image_path" in cols and row["image_path"]:
                parts.append(f"Image: {row['image_path']}")
            return _truncate("\n".join(parts), self._max_chars)
        finally:
            con.close()

    def _pick_by_id_or_index(
        self,
        con: sqlite3.Connection,
        table: str,
        document_id: str,
        ident: str,
        cols: set[str],
    ) -> sqlite3.Row | None:
        has_doc = "document_id" in cols
        ident = (ident or "").strip()
        # Try exact id match first.
        row: sqlite3.Row | None = None
        try:
            if has_doc:
                row = con.execute(
                    f"SELECT * FROM {table} WHERE document_id=? AND id=? LIMIT 1",
                    (document_id, ident),
                ).fetchone()
            else:
                row = con.execute(
                    f"SELECT * FROM {table} WHERE id=? LIMIT 1", (ident,)
                ).fetchone()
        except sqlite3.DatabaseError:
            row = None
        if row is not None:
            return row
        # Fall back to 1-based index within the document.
        order = "page" if "page" in cols else "id"
        try:
            if has_doc:
                rows = con.execute(
                    f"SELECT * FROM {table} WHERE document_id=? ORDER BY {order}",
                    (document_id,),
                ).fetchall()
            else:
                rows = con.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        except sqlite3.DatabaseError:
            return None
        if ident.isdigit():
            idx = int(ident) - 1
            if 0 <= idx < len(rows):
                result: sqlite3.Row = rows[idx]
                return result
        return None

    # --------------------------------------------------------- doc.md parsing

    @staticmethod
    def _headings(doc_md: str) -> list[tuple[int, str, int]]:
        headings: list[tuple[int, str, int]] = []
        for idx, line in enumerate(doc_md.splitlines()):
            m = _HEADING_RE.match(line.strip())
            if m:
                headings.append((len(m.group(1)), m.group(2).strip(), idx))
        return headings

    def _best_section(self, doc_md: str, query: str) -> str | None:
        lines = doc_md.splitlines()
        headings = self._headings(doc_md)
        if not headings:
            return None
        terms = _tokens(query)
        best_i = -1
        best_score = -1.0
        for i, (_, title, _) in enumerate(headings):
            score = _overlap_score(terms, title)
            if score > best_score:
                best_score = score
                best_i = i
        if best_i < 0:
            best_i = 0
        level, title, start_line = headings[best_i]
        # End at the next heading whose level is <= this heading's level so we keep
        # child subsections (deeper headings) as part of the section body.
        end_line = len(lines)
        for _, _, h_line in headings[best_i + 1:]:
            # Recover that heading's level.
            m = _HEADING_RE.match(lines[h_line].strip())
            if m and len(m.group(1)) <= level:
                end_line = h_line
                break
        body = "\n".join(lines[start_line:end_line]).strip()
        return body or None

    def _search_doc_md(self, doc_md: str, terms: set[str], top_k: int) -> list[str]:
        lines = doc_md.splitlines()
        headings = self._headings(doc_md)
        heading_at = {h_line: title for _, title, h_line in headings}
        # Build (paragraph_text, nearest_heading) blocks split on blank lines.
        blocks: list[tuple[str, str]] = []
        buf: list[str] = []
        current_heading = ""
        for idx, line in enumerate(lines):
            if idx in heading_at:
                current_heading = heading_at[idx]
            if line.strip():
                buf.append(line)
            elif buf:
                blocks.append(("\n".join(buf).strip(), current_heading))
                buf = []
        if buf:
            blocks.append(("\n".join(buf).strip(), current_heading))
        scored = [
            (_overlap_score(terms, text), text, heading)
            for text, heading in blocks
            if text
        ]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: s[0], reverse=True)
        out: list[str] = []
        for _, text, heading in scored[:top_k]:
            loc = f"[under: {heading}] " if heading else ""
            out.append(_truncate(loc + text, self._max_chars // max(1, top_k)))
        return out

    # ----------------------------------------------------------- chunk fallbacks

    def _chunk_rows(self, con: sqlite3.Connection, document_id: str) -> list[sqlite3.Row]:
        if not self._table_exists(con, "chunks"):
            return []
        try:
            return con.execute(
                "SELECT * FROM chunks WHERE document_id=? ORDER BY page_start, id",
                (document_id,),
            ).fetchall()
        except sqlite3.DatabaseError:
            return []

    def _chunk_read(self, con: sqlite3.Connection, document_id: str, query: str) -> str:
        rows = self._chunk_rows(con, document_id)
        if not rows:
            return "(no readable content found for this document)"
        terms = _tokens(query)
        scored = sorted(
            rows, key=lambda r: _overlap_score(terms, str(r["text"] or "")), reverse=True
        )
        top = [str(r["text"]) for r in scored[:3] if r["text"]]
        return _truncate("\n\n".join(top), self._max_chars) if top else "(no matching content)"

    def _chunk_search(
        self, con: sqlite3.Connection, document_id: str, terms: set[str], top_k: int
    ) -> str:
        rows = self._chunk_rows(con, document_id)
        if not rows:
            return "(no readable content found for this document)"
        scored = [
            (_overlap_score(terms, str(r["text"] or "")), r) for r in rows
        ]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: s[0], reverse=True)
        if not scored:
            return "(no passages matched the query)"
        out = []
        budget = self._max_chars // max(1, top_k)
        for _, r in scored[:top_k]:
            row_keys = r.keys()
            loc = f"[pages {r['page_start']}-{r['page_end']}] " if "page_start" in row_keys else ""
            out.append(_truncate(loc + str(r["text"]), budget))
        return _truncate("\n\n".join(out), self._max_chars)


def nav_index_available(sqlite_path: Path) -> bool:
    """True iff the SQLite store has a non-empty ``documents`` table."""
    if not sqlite_path.exists():
        return False
    try:
        con = sqlite3.connect(str(sqlite_path))
    except sqlite3.DatabaseError:
        return False
    try:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents' LIMIT 1"
        ).fetchone()
        if row is None:
            return False
        count = con.execute("SELECT COUNT(*) FROM documents").fetchone()
        return bool(count and count[0])
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


__all__ = ["DocMeta", "DocumentNavigator", "nav_index_available"]
