from knowledge_extraction.application.services.graphrag_agent import RetrievalHit
from knowledge_extraction.application.services.graphrag_eval import (
    GraphRagEvalCase,
    aggregate_results,
    evaluate_case,
)


def _case() -> GraphRagEvalCase:
    return GraphRagEvalCase(
        case_id="superglue-performance-disambiguation",
        question="What is the performance of SuperGLUE?",
        query_rewrite=None,
        category="diagram",
        focus_terms=["superglue"],
        positive_terms=["superglue", "performance", "score"],
        min_positive_term_matches=1,
        domain_terms=["benchmark", "model", "leaderboard"],
        negative_terms=["adhesive", "cyanoacrylate", "bond strength", "sticking", "ca-glue"],
        expected_hit_kinds=["table", "chunk", "figure"],
        max_hit_rank=5,
        top_k=10,
    )


def test_superglue_eval_passes_for_benchmark_context() -> None:
    result = evaluate_case(
        _case(),
        [
            RetrievalHit(
                kind="table",
                id="t-superglue",
                score=1.2,
                text="SuperGLUE benchmark leaderboard performance score for model X is 89.4",
                meta={"page": 88},
            )
        ],
    )
    assert result.passed is True
    assert result.metrics["mrr"] == 1.0
    assert result.metrics["positive_recall_at_k"] == 1.0
    assert result.metrics["citation_recall"] == 1.0


def test_superglue_eval_fails_for_adhesive_context() -> None:
    result = evaluate_case(
        _case(),
        [
            RetrievalHit(
                kind="table",
                id="t-adhesive",
                score=1.1,
                text="Superglue adhesive bond strength performance on aluminum surfaces",
                meta={"page": 3},
            )
        ],
    )
    assert result.passed is False
    assert "negative-domain" in result.reason


def test_metrics_computed_on_failure() -> None:
    """Metrics must populate even when the case fails so failure analysis is possible."""
    case = _case()
    result = evaluate_case(case, [])
    assert result.passed is False
    assert result.metrics["mrr"] == 0.0
    assert result.metrics["positive_recall_at_k"] == 0.0


def test_mrr_reflects_first_positive_match_rank() -> None:
    case = _case()
    hits = [
        RetrievalHit(kind="chunk", id="c1", score=0.9, text="unrelated text about benchmarks", meta={}),
        RetrievalHit(kind="chunk", id="c2", score=0.8, text="another unrelated chunk", meta={}),
        RetrievalHit(kind="figure", id="f1", score=0.7,
                     text="SuperGLUE performance score on the benchmark leaderboard", meta={}),
    ]
    result = evaluate_case(case, hits)
    assert result.passed is True
    assert result.metrics["mrr"] == round(1.0 / 3, 3)


def test_required_evidence_id_gates_pass() -> None:
    case = _case()
    case.required_evidence_ids = ["pdf:88"]
    case.expected_hit_kinds = ["pdf_page", "chunk", "figure"]
    hit_with = RetrievalHit(kind="pdf_page", id="pdf:88", score=0.9,
                            text="SuperGLUE benchmark leaderboard performance score 89.4",
                            meta={"page": 88})
    result_with = evaluate_case(case, [hit_with])
    assert result_with.passed is True
    assert result_with.metrics["citation_recall"] == 1.0

    hit_without = RetrievalHit(kind="pdf_page", id="pdf:42", score=0.9,
                               text="SuperGLUE benchmark leaderboard performance score 89.4",
                               meta={"page": 42})
    result_without = evaluate_case(case, [hit_without])
    assert result_without.passed is False
    assert "required citations missing" in result_without.reason
    assert result_without.metrics["citation_recall"] == 0.0


def test_adversarial_case_passes_when_retrieval_is_weak() -> None:
    case = GraphRagEvalCase(
        case_id="adversarial-bitcoin",
        question="What was Bitcoin's price in March 2024?",
        query_rewrite=None,
        category="adversarial",
        focus_terms=[],
        positive_terms=[],
        min_positive_term_matches=0,
        domain_terms=[],
        negative_terms=[],
        expected_hit_kinds=[],
        max_hit_rank=5,
        top_k=10,
        is_adversarial=True,
        min_score_for_grounded=0.05,
    )
    weak = [RetrievalHit(kind="chunk", id="c1", score=0.01, text="incidental", meta={})]
    strong = [RetrievalHit(kind="chunk", id="c1", score=0.50, text="false confident hit", meta={})]
    none = []
    assert evaluate_case(case, weak).passed is True
    assert evaluate_case(case, strong).passed is False
    assert evaluate_case(case, none).passed is True


def test_aggregate_results_groups_by_category() -> None:
    case_a = _case()
    case_b = _case()
    case_b.case_id = "other"
    case_b.category = "tabular"
    pass_hit = RetrievalHit(
        kind="table", id="t1", score=1.0,
        text="SuperGLUE benchmark leaderboard performance score 89.4", meta={},
    )
    results = [evaluate_case(case_a, [pass_hit]), evaluate_case(case_b, [])]
    agg = aggregate_results(results)
    assert agg["overall"]["total"] == 2
    assert agg["overall"]["passed"] == 1
    assert agg["by_category"]["diagram"]["passed"] == 1
    assert agg["by_category"]["tabular"]["passed"] == 0


