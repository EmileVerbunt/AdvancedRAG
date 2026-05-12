from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from knowledge_extraction.application.services.graphrag_agent import RetrievalHit


@dataclass(slots=True)
class GraphRagEvalCase:
    case_id: str
    question: str
    query_rewrite: str | None
    category: str
    focus_terms: list[str]
    positive_terms: list[str]
    min_positive_term_matches: int
    domain_terms: list[str]
    negative_terms: list[str]
    expected_hit_kinds: list[str]
    max_hit_rank: int
    min_domain_term_matches: int = 1
    top_k: int = 10
    # Optional citation gate: case passes only if EVERY id is in the top-N hit IDs.
    required_evidence_ids: list[str] = field(default_factory=list)
    # If true, case passes only when retrieval is "weak" (no hit beats min_score_for_grounded).
    # Used for adversarial / out-of-scope questions.
    is_adversarial: bool = False
    min_score_for_grounded: float = 0.05

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> GraphRagEvalCase:
        def _int_field(name: str, default: int) -> int:
            value = raw.get(name)
            return default if value is None else int(value)

        def _float_field(name: str, default: float) -> float:
            value = raw.get(name)
            return default if value is None else float(value)

        return cls(
            case_id=str(raw.get("case_id") or ""),
            question=str(raw.get("question") or ""),
            query_rewrite=(str(raw.get("query_rewrite")) if raw.get("query_rewrite") else None),
            category=str(raw.get("category") or "text"),
            focus_terms=[str(v) for v in raw.get("focus_terms", [])],  # type: ignore[arg-type]
            positive_terms=[str(v) for v in raw.get("positive_terms", [])],  # type: ignore[arg-type]
            min_positive_term_matches=_int_field("min_positive_term_matches", 1),
            domain_terms=[str(v) for v in raw.get("domain_terms", [])],  # type: ignore[arg-type]
            negative_terms=[str(v) for v in raw.get("negative_terms", [])],  # type: ignore[arg-type]
            expected_hit_kinds=[str(v) for v in raw.get("expected_hit_kinds", [])],  # type: ignore[arg-type]
            max_hit_rank=_int_field("max_hit_rank", 5),
            min_domain_term_matches=_int_field("min_domain_term_matches", 1),
            top_k=_int_field("top_k", 10),
            required_evidence_ids=[str(v) for v in raw.get("required_evidence_ids", [])],  # type: ignore[arg-type]
            is_adversarial=bool(raw.get("is_adversarial", False)),
            min_score_for_grounded=_float_field("min_score_for_grounded", 0.05),
        )


@dataclass(slots=True)
class GraphRagEvalResult:
    case_id: str
    question: str
    category: str
    passed: bool
    reason: str
    top_hit: RetrievalHit | None
    metrics: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        out = asdict(self)
        if self.top_hit is not None:
            out["top_hit"] = asdict(self.top_hit)
        return out


def evaluate_case(case: GraphRagEvalCase, hits: list[RetrievalHit]) -> GraphRagEvalResult:
    """Evaluate a retrieval case and produce pass/fail + numeric quality metrics.

    Metrics produced (always, even on fail):
      * positive_recall_at_k  — fraction of positive_terms covered by top-K hits
      * positive_precision_at_k — fraction of top-K hits that contain at least one positive term
      * mrr — reciprocal rank of the first top-K hit that contains any positive term
      * citation_recall — fraction of required_evidence_ids found in hit IDs (1.0 if none required)
      * top_score — score of the first hit (used for adversarial-grounding check)
    """
    if not hits:
        return GraphRagEvalResult(
            case_id=case.case_id, question=case.question, category=case.category,
            passed=case.is_adversarial,
            reason="no retrieval hits" + (" (expected for adversarial)" if case.is_adversarial else ""),
            top_hit=None,
            metrics={"positive_recall_at_k": 0.0, "positive_precision_at_k": 0.0,
                     "mrr": 0.0, "citation_recall": 1.0 if not case.required_evidence_ids else 0.0,
                     "top_score": 0.0},
        )

    top_hit = hits[0]
    k = max(1, case.max_hit_rank)
    ranked_hits = hits[:k]

    # --- Adversarial case: PASS if top score is below grounding threshold ---
    if case.is_adversarial:
        passed = top_hit.score < case.min_score_for_grounded
        return GraphRagEvalResult(
            case_id=case.case_id, question=case.question, category=case.category,
            passed=passed,
            reason=(
                f"top_score={top_hit.score:.3f} below {case.min_score_for_grounded} (correctly ungrounded)"
                if passed else
                f"top_score={top_hit.score:.3f} >= {case.min_score_for_grounded} (system over-confidently retrieved)"
            ),
            top_hit=top_hit,
            metrics={"top_score": top_hit.score, "positive_recall_at_k": 0.0,
                     "positive_precision_at_k": 0.0, "mrr": 0.0, "citation_recall": 0.0},
        )

    # --- Numeric metrics over top-K (computed unconditionally) ---
    pos_norm = [_normalize_text(t) for t in case.positive_terms if t]
    neg_norm = [_normalize_text(t) for t in case.negative_terms if t]
    hit_texts = [_normalize_text(h.text) for h in ranked_hits]

    positive_terms_found = sum(1 for term in pos_norm if any(term in t for t in hit_texts))
    positive_recall = positive_terms_found / len(pos_norm) if pos_norm else 0.0

    hits_with_positive = sum(1 for t in hit_texts if any(term in t for term in pos_norm))
    positive_precision = hits_with_positive / len(hit_texts) if hit_texts else 0.0

    mrr = 0.0
    for rank, t in enumerate(hit_texts, start=1):
        if any(term in t for term in pos_norm):
            mrr = 1.0 / rank
            break

    cited_ids = {h.id for h in ranked_hits}
    citation_recall = (
        sum(1 for rid in case.required_evidence_ids if rid in cited_ids) / len(case.required_evidence_ids)
        if case.required_evidence_ids else 1.0
    )

    metrics: dict[str, float] = {
        "positive_recall_at_k": round(positive_recall, 3),
        "positive_precision_at_k": round(positive_precision, 3),
        "mrr": round(mrr, 3),
        "citation_recall": round(citation_recall, 3),
        "top_score": round(top_hit.score, 3),
    }

    # --- Pass/fail gates (in priority order — first failure wins) ---
    if case.expected_hit_kinds and not any(h.kind in set(case.expected_hit_kinds) for h in ranked_hits):
        return GraphRagEvalResult(
            case_id=case.case_id, question=case.question, category=case.category,
            passed=False, reason="expected evidence type not found in top ranked hits",
            top_hit=top_hit, metrics=metrics,
        )

    focus_terms = [_normalize_text(t) for t in case.focus_terms if t]
    scoped_hits = ranked_hits
    if focus_terms:
        scoped_hits = [h for h in ranked_hits if any(term in _normalize_text(h.text) for term in focus_terms)]
    if not scoped_hits:
        return GraphRagEvalResult(
            case_id=case.case_id, question=case.question, category=case.category,
            passed=False, reason="no focus-term-specific hit was retrieved",
            top_hit=top_hit, metrics=metrics,
        )

    scoped_corpus = " ".join(_normalize_text(h.text) for h in scoped_hits)
    positive_matches = sum(1 for term in pos_norm if term in scoped_corpus)
    has_positive = positive_matches >= max(1, case.min_positive_term_matches)
    domain_matches = sum(1 for term in (_normalize_text(t) for t in case.domain_terms) if term in scoped_corpus)
    has_domain = domain_matches >= max(0, case.min_domain_term_matches)
    has_negative = any(term in scoped_corpus for term in neg_norm)

    if has_negative:
        return GraphRagEvalResult(
            case_id=case.case_id, question=case.question, category=case.category,
            passed=False, reason="retrieval leaked negative-domain terms",
            top_hit=top_hit, metrics=metrics,
        )

    if not has_positive:
        return GraphRagEvalResult(
            case_id=case.case_id, question=case.question, category=case.category,
            passed=False,
            reason=f"missing expected positive terms ({positive_matches}/{case.min_positive_term_matches} matched)",
            top_hit=top_hit, metrics=metrics,
        )

    if not has_domain:
        return GraphRagEvalResult(
            case_id=case.case_id, question=case.question, category=case.category,
            passed=False, reason="did not retrieve required domain evidence",
            top_hit=top_hit, metrics=metrics,
        )

    if case.required_evidence_ids and citation_recall < 1.0:
        missing = [rid for rid in case.required_evidence_ids if rid not in cited_ids]
        return GraphRagEvalResult(
            case_id=case.case_id, question=case.question, category=case.category,
            passed=False,
            reason=f"required citations missing from top-{k}: {missing}",
            top_hit=top_hit, metrics=metrics,
        )

    return GraphRagEvalResult(
        case_id=case.case_id, question=case.question, category=case.category,
        passed=True, reason="retrieved focus-domain evidence without negative-domain confusion",
        top_hit=top_hit, metrics=metrics,
    )


def aggregate_results(results: list[GraphRagEvalResult]) -> dict[str, object]:
    """Per-category and overall aggregates for senior-stakeholder readouts."""
    overall = {
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "avg_mrr": _avg(r.metrics.get("mrr", 0.0) for r in results),
        "avg_precision_at_k": _avg(r.metrics.get("positive_precision_at_k", 0.0) for r in results),
        "avg_recall_at_k": _avg(r.metrics.get("positive_recall_at_k", 0.0) for r in results),
        "avg_citation_recall": _avg(r.metrics.get("citation_recall", 0.0) for r in results),
    }
    by_category: dict[str, dict[str, object]] = {}
    for r in results:
        bucket = by_category.setdefault(r.category, {"total": 0, "passed": 0, "mrr": [], "precision": []})
        bucket["total"] = int(bucket["total"]) + 1  # type: ignore[arg-type]
        if r.passed:
            bucket["passed"] = int(bucket["passed"]) + 1  # type: ignore[arg-type]
        bucket["mrr"].append(r.metrics.get("mrr", 0.0))  # type: ignore[union-attr]
        bucket["precision"].append(r.metrics.get("positive_precision_at_k", 0.0))  # type: ignore[union-attr]
    for _cat, agg in by_category.items():
        mrrs = agg.pop("mrr")
        precs = agg.pop("precision")
        agg["avg_mrr"] = _avg(mrrs)  # type: ignore[arg-type]
        agg["avg_precision_at_k"] = _avg(precs)  # type: ignore[arg-type]
    return {"overall": overall, "by_category": by_category}


def _avg(values) -> float:
    vals = list(values)
    return round(sum(vals) / len(vals), 3) if vals else 0.0


def _normalize_text(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip()

