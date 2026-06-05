import json
from pathlib import Path

from knowledge_extraction.application.services.benchmark import (
    BackendCost,
    IngestionStats,
    _percentile,
    build_report,
    read_ingestion_stats,
    render_markdown,
    summarize_cost,
)
from knowledge_extraction.application.services.graphrag_eval import GraphRagEvalResult


def _result(
    case_id: str,
    *,
    passed: bool = True,
    mrr: float = 1.0,
    latency_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    extra: dict[str, float | str] | None = None,
) -> GraphRagEvalResult:
    r = GraphRagEvalResult(
        case_id=case_id,
        question="q",
        category="text",
        passed=passed,
        reason="",
        top_hit=None,
        metrics={"mrr": mrr},
    )
    r.latency_ms = latency_ms
    r.tokens_in = tokens_in
    r.tokens_out = tokens_out
    r.extra = extra or {}
    return r


def test_percentile_basic() -> None:
    assert _percentile([], 50) is None
    assert _percentile([42], 95) == 42
    assert _percentile([10, 20, 30, 40], 50) == 25
    assert _percentile([10, 20, 30, 40], 0) == 10
    assert _percentile([10, 20, 30, 40], 100) == 40


def test_summarize_cost_sums_tokens_and_latency() -> None:
    results = [
        _result("a", passed=True, mrr=1.0, latency_ms=100, tokens_in=10, tokens_out=5),
        _result("b", passed=False, mrr=0.0, latency_ms=300, tokens_in=20, tokens_out=15),
    ]
    cost = summarize_cost("lazy", results)
    assert cost.backend == "lazy"
    assert cost.cases == 2
    assert cost.passed == 1
    assert cost.avg_mrr == 0.5
    assert cost.latency_total_ms == 400
    assert cost.latency_p50_ms == 200
    assert cost.tokens_in == 30
    assert cost.tokens_out == 20
    assert cost.tokens_total == 50


def test_summarize_cost_tokens_none_when_unavailable() -> None:
    # ms backend: latency present, tokens never reported -> tokens stay None.
    results = [
        _result("a", latency_ms=5000, tokens_in=None, tokens_out=None, extra={"method": "local"}),
        _result("b", latency_ms=7000, tokens_in=None, tokens_out=None, extra={"method": "global"}),
    ]
    cost = summarize_cost("ms", results)
    assert cost.tokens_in is None
    assert cost.tokens_out is None
    assert cost.tokens_total is None
    assert cost.latency_total_ms == 12000


def test_summarize_cost_mini_zero_tokens_is_not_none() -> None:
    results = [_result("a", latency_ms=12, tokens_in=0, tokens_out=0)]
    cost = summarize_cost("mini", results)
    assert cost.tokens_in == 0
    assert cost.tokens_total == 0


def test_summarize_cost_averages_numeric_extras_skips_strings() -> None:
    results = [
        _result("a", extra={"rounds": 1.0, "critic_confidence": 0.8, "method": "x"}),
        _result("b", extra={"rounds": 3.0, "critic_confidence": 0.6, "method": "y"}),
    ]
    cost = summarize_cost("agentic", results)
    assert cost.extra["avg_rounds"] == 2.0
    assert cost.extra["avg_critic_confidence"] == 0.7
    assert "avg_method" not in cost.extra


def test_read_ingestion_stats_parses_latest_ingest_run(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    # An eval run (should be ignored) and an ingest run (should be read).
    (log_dir / "run-20240101-000000-eval.jsonl").write_text(
        "\n".join([
            json.dumps({"event": "run.start", "command": "graphrag"}),
            json.dumps({"event": "run.finish", "command": "graphrag", "duration_ms": 999}),
        ]),
        encoding="utf-8",
    )
    ingest = log_dir / "run-20240102-000000-ingest.jsonl"
    ingest.write_text(
        "\n".join([
            json.dumps({"event": "run.start", "command": "ingest"}),
            json.dumps({
                "event": "run.finish", "command": "ingest",
                "duration_ms": 123456, "input_tokens": 1000,
                "output_tokens": 200, "total_tokens": 1200,
            }),
        ]),
        encoding="utf-8",
    )

    graphrag_dir = tmp_path / "graphrag"
    out = graphrag_dir / "1.0.0" / "output"
    out.mkdir(parents=True)
    (out / "stats.json").write_text(json.dumps({"total_runtime": 42.5}), encoding="utf-8")

    stats = read_ingestion_stats(log_dir, graphrag_dir)
    assert stats.ingest_duration_ms == 123456
    assert stats.ingest_total_tokens == 1200
    assert stats.ingest_input_tokens == 1000
    assert stats.ms_index_runtime_s == 42.5
    assert stats.ingest_log_file is not None


def test_read_ingestion_stats_missing_dirs_returns_empty() -> None:
    stats = read_ingestion_stats(Path("does/not/exist"), Path("nope/either"))
    assert stats.ingest_duration_ms is None
    assert stats.ms_index_runtime_s is None


def test_render_markdown_includes_backends_and_na() -> None:
    costs = [
        summarize_cost("mini", [_result("a", latency_ms=10, tokens_in=0, tokens_out=0)]),
        summarize_cost("ms", [_result("a", latency_ms=5000)]),
    ]
    ingestion = IngestionStats(ingest_duration_ms=1000, ingest_total_tokens=None)
    md = render_markdown("suite.json", costs, ingestion)
    assert "# 4-Way Backend Benchmark" in md
    assert "| mini |" in md
    assert "| ms |" in md
    assert "n/a" in md  # ms tokens + missing ingest tokens


def test_build_report_is_json_serialisable() -> None:
    runs = {"mini": [_result("a", latency_ms=10, tokens_in=0, tokens_out=0)]}
    costs = [summarize_cost("mini", runs["mini"])]
    report = build_report("suite.json", costs, IngestionStats(), runs)
    # Round-trips through json without error.
    text = json.dumps(report)
    assert "backends" in json.loads(text)
    assert isinstance(costs[0], BackendCost)


def test_eval_result_telemetry_serialises_in_to_dict() -> None:
    r = _result("a", latency_ms=50, tokens_in=3, tokens_out=4, extra={"rounds": 2.0})
    d = r.to_dict()
    assert d["latency_ms"] == 50
    assert d["tokens_in"] == 3
    assert d["extra"] == {"rounds": 2.0}


def test_ms_eval_maps_auto_method_to_none(monkeypatch) -> None:
    """`--method auto` must reach the agent as None (auto-route), not the literal
    string "auto", which the GraphRAG CLI rejects."""
    from types import SimpleNamespace

    from knowledge_extraction.application.services.graphrag_eval import GraphRagEvalCase
    from knowledge_extraction.cli import main

    seen: dict[str, object] = {}

    class _FakeMsAgent:
        def __init__(self, _settings: object) -> None:
            pass

        def ask(self, _question: str, *, method, **_kwargs):  # type: ignore[no-untyped-def]
            seen["method"] = method
            return SimpleNamespace(answer="grounded answer", method=method or "global", duration_ms=10)

    monkeypatch.setattr(main, "MsGraphRagAgent", _FakeMsAgent)
    case = GraphRagEvalCase(
        case_id="c", question="q", query_rewrite=None, category="text",
        focus_terms=[], positive_terms=[], min_positive_term_matches=0,
        domain_terms=[], negative_terms=[], expected_hit_kinds=[],
        max_hit_rank=5, top_k=10,
    )
    results = main._run_ms_eval(
        [case], object(), method="auto",
        community_level=2, response_type="Multiple Paragraphs", timeout_seconds=10,
    )
    assert seen["method"] is None
    assert results[0].latency_ms == 10

