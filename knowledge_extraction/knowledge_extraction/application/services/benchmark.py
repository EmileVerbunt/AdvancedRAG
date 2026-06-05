"""Pure benchmark logic for the 4-way backend comparison (``ke graphrag bench``).

This module turns the per-case :class:`GraphRagEvalResult` telemetry produced by
the eval runners into backend-level cost/latency summaries, reads ingestion cost
from the run logs, and renders the comparison as a JSON-serialisable dict and a
markdown report.

It performs no LLM/network work and only the file I/O needed to read existing
run logs and GraphRAG stats, which keeps it unit-testable without live Azure.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean

from knowledge_extraction.application.services.graphrag_eval import (
    GraphRagEvalResult,
    aggregate_results,
)


def _percentile(values: Sequence[int], pct: float) -> int | None:
    """Nearest-rank percentile of ``values`` (ints), or ``None`` when empty."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


@dataclass(slots=True)
class BackendCost:
    """Aggregated quality + cost/latency for one backend over a suite."""

    backend: str
    cases: int
    passed: int
    avg_mrr: float
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    latency_total_ms: int
    tokens_in: int | None
    tokens_out: int | None
    tokens_total: int | None
    # Averaged backend-specific numeric telemetry (e.g. ``avg_rounds`` for agentic).
    extra: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def summarize_cost(backend: str, results: Sequence[GraphRagEvalResult]) -> BackendCost:
    """Roll up per-case telemetry into a :class:`BackendCost`.

    Tokens are reported as ``None`` when *no* case exposed a count (e.g. the MS
    GraphRAG CLI), distinguishing "unknown" from a genuine zero (the mini
    baseline, which reports 0 because it never calls an LLM).
    """
    n = len(results)
    passed = sum(1 for r in results if r.passed)
    mrrs = [r.metrics.get("mrr", 0.0) for r in results]
    avg_mrr = mean(mrrs) if mrrs else 0.0

    lat = [r.latency_ms for r in results if r.latency_ms is not None]
    tin = [r.tokens_in for r in results if r.tokens_in is not None]
    tout = [r.tokens_out for r in results if r.tokens_out is not None]

    extra_acc: dict[str, list[float]] = {}
    for r in results:
        for key, value in r.extra.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            extra_acc.setdefault(key, []).append(float(value))
    extra = {f"avg_{k}": round(mean(v), 3) for k, v in extra_acc.items()}

    tokens_in = sum(tin) if tin else None
    tokens_out = sum(tout) if tout else None
    tokens_total = (
        (tokens_in or 0) + (tokens_out or 0) if (tin or tout) else None
    )

    return BackendCost(
        backend=backend,
        cases=n,
        passed=passed,
        avg_mrr=round(avg_mrr, 4),
        latency_p50_ms=_percentile(lat, 50),
        latency_p95_ms=_percentile(lat, 95),
        latency_total_ms=sum(lat),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_total=tokens_total,
        extra=extra,
    )


@dataclass(slots=True)
class IngestionStats:
    """One-off ingestion cost shared by every query backend, read from logs.

    The shared substrate (SQLite chunks/tables/figures/claims) is built once by
    ``ke ingest``; ``ms`` additionally pays a GraphRAG indexing cost captured by
    GraphRAG's own ``stats.json`` (which records runtime but not tokens).
    """

    ingest_duration_ms: int | None = None
    ingest_input_tokens: int | None = None
    ingest_output_tokens: int | None = None
    ingest_total_tokens: int | None = None
    ingest_log_file: str | None = None
    ms_index_runtime_s: float | None = None
    ms_index_stats_file: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def read_ingestion_stats(log_dir: Path, graphrag_dir: Path) -> IngestionStats:
    """Read shared ingest cost (latest ``ingest`` run) and MS index runtime.

    Returns an empty-ish :class:`IngestionStats` if no ingest run or GraphRAG
    stats are found; callers render the missing values as ``n/a``.
    """
    stats = IngestionStats()

    if log_dir.exists():
        # Run logs are named run-YYYYMMDD-HHMMSS-<id>.jsonl, so a reverse
        # filename sort orders them newest-first independent of file mtime
        # (which can be perturbed by copies/restores).
        runs = sorted(log_dir.glob("run-*.jsonl"), key=lambda p: p.name, reverse=True)
        for run in runs:
            records = _read_jsonl(run)
            is_ingest = any(rec.get("command") == "ingest" for rec in records)
            finish = next((r for r in records if r.get("event") == "run.finish"), None)
            if not is_ingest or finish is None:
                continue
            stats.ingest_duration_ms = _as_opt_int(finish.get("duration_ms"))
            stats.ingest_input_tokens = _as_opt_int(finish.get("input_tokens"))
            stats.ingest_output_tokens = _as_opt_int(finish.get("output_tokens"))
            stats.ingest_total_tokens = _as_opt_int(finish.get("total_tokens"))
            stats.ingest_log_file = str(run)
            break

    if graphrag_dir.exists():
        stats_files = sorted(
            graphrag_dir.glob("**/stats.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for sf in stats_files:
            try:
                data = json.loads(sf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            runtime = data.get("total_runtime") if isinstance(data, dict) else None
            if isinstance(runtime, (int, float)):
                stats.ms_index_runtime_s = float(runtime)
                stats.ms_index_stats_file = str(sf)
                break

    return stats


def _as_opt_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def build_report(
    suite: str,
    costs: Sequence[BackendCost],
    ingestion: IngestionStats,
    per_backend_results: dict[str, Sequence[GraphRagEvalResult]],
) -> dict[str, object]:
    """Assemble the full JSON-serialisable benchmark report."""
    return {
        "suite": suite,
        "ingestion": ingestion.to_dict(),
        "backends": [c.to_dict() for c in costs],
        "details": {
            name: {
                "aggregates": aggregate_results(list(results)),
                "results": [r.to_dict() for r in results],
            }
            for name, results in per_backend_results.items()
        },
    }


def _fmt_int(value: int | None) -> str:
    return "n/a" if value is None else f"{value:,}"


def _fmt_ms(value: int | None) -> str:
    return "n/a" if value is None else f"{value / 1000:.2f}s"


def render_markdown(
    suite: str,
    costs: Sequence[BackendCost],
    ingestion: IngestionStats,
) -> str:
    """Render the benchmark as a markdown report for ``work/benchmarks/*.md``."""
    lines: list[str] = []
    lines.append("# 4-Way Backend Benchmark")
    lines.append("")
    lines.append(f"Suite: `{suite}`")
    lines.append("")

    lines.append("## Quality + cost per backend")
    lines.append("")
    lines.append(
        "| backend | passed | avg MRR | p50 latency | p95 latency | total time "
        "| tokens in | tokens out | tokens total |"
    )
    lines.append(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    for c in costs:
        lines.append(
            f"| {c.backend} | {c.passed}/{c.cases} | {c.avg_mrr:.3f} "
            f"| {_fmt_ms(c.latency_p50_ms)} | {_fmt_ms(c.latency_p95_ms)} "
            f"| {_fmt_ms(c.latency_total_ms)} | {_fmt_int(c.tokens_in)} "
            f"| {_fmt_int(c.tokens_out)} | {_fmt_int(c.tokens_total)} |"
        )
    lines.append("")

    extra_backends = [c for c in costs if c.extra]
    if extra_backends:
        lines.append("## Backend-specific telemetry")
        lines.append("")
        for c in extra_backends:
            detail = ", ".join(f"{k}={v:g}" for k, v in sorted(c.extra.items()))
            lines.append(f"- **{c.backend}**: {detail}")
        lines.append("")

    lines.append("## Ingestion cost (one-off, shared substrate)")
    lines.append("")
    lines.append(f"- ingest time: {_fmt_ms(ingestion.ingest_duration_ms)}")
    lines.append(f"- ingest tokens in: {_fmt_int(ingestion.ingest_input_tokens)}")
    lines.append(f"- ingest tokens out: {_fmt_int(ingestion.ingest_output_tokens)}")
    lines.append(f"- ingest tokens total: {_fmt_int(ingestion.ingest_total_tokens)}")
    ms_runtime = (
        "n/a"
        if ingestion.ms_index_runtime_s is None
        else f"{ingestion.ms_index_runtime_s:.1f}s"
    )
    lines.append(f"- ms GraphRAG index runtime: {ms_runtime} (tokens not exposed by CLI)")
    lines.append("")
    lines.append(
        "> mini / lazy / agentic share the one-off ingest cost above and add no "
        "indexing step. ms additionally pays the GraphRAG index runtime. lazy and "
        "agentic spend their main compute at query time (see per-backend tokens)."
    )
    lines.append("")
    return "\n".join(lines)
