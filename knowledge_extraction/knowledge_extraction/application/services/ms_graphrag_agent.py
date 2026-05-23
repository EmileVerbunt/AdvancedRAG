"""Microsoft GraphRAG retrieval agent.

Wraps the official ``graphrag query`` CLI so the rest of the codebase (CLI
``graphrag ask``, eval harness, future MCP/HTTP wrappers) can call it through a
small, stable adapter. We deliberately shell out instead of importing the
``graphrag`` Python API because the API surface changes between minor releases
and the CLI contract (``--method [local|global|drift|basic]``) is stable.

Two query methods, automatically routed (override with explicit ``method=``):

* ``local``  — entity-centric, best for single-fact / lookup questions.
* ``global`` — community-summary map-reduce, best for thematic / synthesis
  questions ("what does the report say overall about X?").

Routing heuristic: questions that look factoid (start with "when/where/who",
mention a specific named entity, or contain a year/percent literal) → local.
Everything else → global. Override with ``method=`` for benchmarking.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from knowledge_extraction.config.settings import Settings
from knowledge_extraction.infrastructure.graphrag.graphrag_runner import resolve_graphrag_executable

log = logging.getLogger(__name__)

QueryMethod = Literal["local", "global", "drift", "basic"]

_FACTOID_LEADERS = ("when", "where", "who", "which", "what is", "what was", "what are")
_NUMERIC_RE = re.compile(r"\b\d{2,4}(?:[.,]\d+)?%?\b")
_REQUIRED_QUERY_PARQUETS: tuple[str, ...] = (
    "communities.parquet",
    "community_reports.parquet",
    "entities.parquet",
    "relationships.parquet",
    "text_units.parquet",
)


@dataclass(slots=True)
class MsGraphRagAnswer:
    """The structured answer returned by ``graphrag query``.

    ``raw_output`` preserves the full CLI stdout (which contains a
    "SUCCESS: <method> Search Response:" preamble); ``answer`` is just the
    response body after that preamble.
    """
    question: str
    method: QueryMethod
    answer: str
    raw_output: str
    workdir: Path
    duration_ms: int
    exit_code: int

    def to_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "method": self.method,
            "answer": self.answer,
            "workdir": str(self.workdir),
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
        }


class IndexNotFoundError(RuntimeError):
    """Raised when no indexed graphrag workdir is available."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        super().__init__(
            f"No graphrag index found at {workdir}. "
            f"Run `uv run ke ingest` first (or `--build-knowledge-tree`)."
        )


class IndexIncompleteError(IndexNotFoundError):
    """Raised when an index exists but required query artifacts are missing."""

    def __init__(self, workdir: Path, missing_files: tuple[str, ...]) -> None:
        self.workdir = workdir
        self.missing_files = missing_files
        missing = ", ".join(missing_files)
        RuntimeError.__init__(
            self,
            f"GraphRAG index at {workdir} is incomplete (missing: {missing}). "
            f"Run `uv run ke graphrag-index` (or `uv run ke ingest`) to rebuild it.",
        )


class MsGraphRagAgent:
    """Adapter over the official ``graphrag query`` CLI."""

    def __init__(
        self,
        settings: Settings,
        workdir_root: Path | None = None,
        executable: str | None = None,
    ) -> None:
        self._settings = settings
        self._root = workdir_root or settings.graphrag_workdir
        self._exe = executable or resolve_graphrag_executable(settings)

    # ---------------------------------------------------------------- queries

    def ask(
        self,
        question: str,
        *,
        method: QueryMethod | None = None,
        community_level: int = 2,
        response_type: str = "Multiple Paragraphs",
        timeout_seconds: int = 300,
    ) -> MsGraphRagAnswer:
        chosen = method or _route_method(question)
        workdir = self._latest_workdir()
        return self._query_sync(
            question, chosen, workdir, community_level, response_type, timeout_seconds
        )

    async def ask_async(
        self,
        question: str,
        *,
        method: QueryMethod | None = None,
        community_level: int = 2,
        response_type: str = "Multiple Paragraphs",
        timeout_seconds: int = 300,
    ) -> MsGraphRagAnswer:
        chosen = method or _route_method(question)
        workdir = self._latest_workdir()
        return await self._query(
            question, chosen, workdir, community_level, response_type, timeout_seconds
        )

    # ---------------------------------------------------------------- helpers

    def _latest_workdir(self) -> Path:
        if not self._root.exists():
            raise IndexNotFoundError(self._root)
        workdirs = [d for d in self._root.iterdir() if d.is_dir() and (d / "output").exists()]
        if not workdirs:
            raise IndexNotFoundError(self._root)
        ready = [d for d in workdirs if _is_query_ready_workdir(d)]
        if ready:
            return max(ready, key=lambda p: p.stat().st_mtime)
        latest_partial = max(workdirs, key=lambda p: p.stat().st_mtime)
        raise IndexIncompleteError(latest_partial, _missing_required_parquets(latest_partial))

    async def _query(
        self,
        question: str,
        method: QueryMethod,
        workdir: Path,
        community_level: int,
        response_type: str,
        timeout_seconds: int,
    ) -> MsGraphRagAnswer:
        t0 = time.perf_counter()
        cmd = [
            self._exe, "query",
            "--root", str(workdir),
            "--method", method,
            "--community-level", str(community_level),
            "--response-type", response_type,
            "--query", question,
        ]
        log.info("graphrag.query.start method=%s workdir=%s", method, workdir)
        # Force UTF-8 in the child so smart quotes / non-ASCII tokens survive the round-trip.
        # Without this, Python on Windows defaults stdout to cp1252 and our utf-8 decode below
        # silently produces U+FFFD replacement characters.
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"graphrag query timed out after {timeout_seconds}s") from None

        duration_ms = int((time.perf_counter() - t0) * 1000)
        out = out_b.decode("utf-8", "replace")
        err = err_b.decode("utf-8", "replace")
        rc = proc.returncode or 0
        if rc != 0:
            log.error("graphrag.query.failed rc=%s err=%s", rc, err[-2000:])
            raise RuntimeError(f"graphrag query exited {rc}: {err[-2000:]}")
        log.info("graphrag.query.complete method=%s duration_ms=%d", method, duration_ms)
        return MsGraphRagAnswer(
            question=question,
            method=method,
            answer=_extract_answer(out),
            raw_output=out,
            workdir=workdir,
            duration_ms=duration_ms,
            exit_code=rc,
        )

    def _query_sync(
        self,
        question: str,
        method: QueryMethod,
        workdir: Path,
        community_level: int,
        response_type: str,
        timeout_seconds: int,
    ) -> MsGraphRagAnswer:
        """Synchronous subprocess invocation.

        Avoids asyncio.run on Windows where Streamlit's ScriptRunner thread does
        not always have a ProactorEventLoop (which is required for subprocess
        support). subprocess.run is simpler, more robust, and produces the same
        result for a one-shot graphrag query.
        """
        t0 = time.perf_counter()
        cmd = [
            self._exe, "query",
            "--root", str(workdir),
            "--method", method,
            "--community-level", str(community_level),
            "--response-type", response_type,
            "--query", question,
        ]
        log.info("graphrag.query.start method=%s workdir=%s", method, workdir)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                env=env,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"graphrag query timed out after {timeout_seconds}s"
            ) from exc

        duration_ms = int((time.perf_counter() - t0) * 1000)
        out = result.stdout.decode("utf-8", "replace")
        err = result.stderr.decode("utf-8", "replace")
        rc = result.returncode or 0
        if rc != 0:
            log.error("graphrag.query.failed rc=%s err=%s", rc, err[-2000:])
            raise RuntimeError(f"graphrag query exited {rc}: {err[-2000:]}")
        log.info("graphrag.query.complete method=%s duration_ms=%d", method, duration_ms)
        return MsGraphRagAnswer(
            question=question,
            method=method,
            answer=_extract_answer(out),
            raw_output=out,
            workdir=workdir,
            duration_ms=duration_ms,
            exit_code=rc,
        )


def _route_method(question: str) -> QueryMethod:
    """Pick local for factoids, global for synthesis questions."""
    q = f" {question.strip().lower()} "
    # synthesis cues win first — they often co-occur with year ranges
    if any(kw in q for kw in (" compare ", " contrast ", " trend ", " trends ",
                              " overall ", " summary ", " themes ", " patterns ",
                              " what does the report say ", " across the document ")):
        return "global"
    if any(q.lstrip().startswith(lead) for lead in _FACTOID_LEADERS):
        return "local"
    if _NUMERIC_RE.search(q):
        return "local"
    return "local"


def _extract_answer(stdout: str) -> str:
    """Strip the ``SUCCESS: <method> Search Response:`` banner if present."""
    marker_idx = stdout.find("Search Response:")
    if marker_idx >= 0:
        nl = stdout.find("\n", marker_idx)
        if nl >= 0:
            return stdout[nl + 1:].strip()
    return stdout.strip()


def _missing_required_parquets(workdir: Path) -> tuple[str, ...]:
    output_dir = workdir / "output"
    if not output_dir.exists():
        return _REQUIRED_QUERY_PARQUETS
    return tuple(name for name in _REQUIRED_QUERY_PARQUETS if not (output_dir / name).exists())


def _is_query_ready_workdir(workdir: Path) -> bool:
    return not _missing_required_parquets(workdir)


def graphrag_index_available(settings: Settings) -> bool:
    """Best-effort check for an indexed workdir without raising."""
    root = settings.graphrag_workdir
    if not root.exists():
        return False
    return any(d.is_dir() and _is_query_ready_workdir(d) for d in root.iterdir())


__all__ = [
    "IndexIncompleteError",
    "IndexNotFoundError",
    "MsGraphRagAgent",
    "MsGraphRagAnswer",
    "QueryMethod",
    "graphrag_index_available",
]
