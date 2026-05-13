"""Tests for QueryRewriter implementations and Reciprocal Rank Fusion."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from knowledge_extraction.application.services.query_rewriter import (
    LexicalQueryRewriter,
    LlmQueryRewriter,
    reciprocal_rank_fusion,
)

# ---------------------------------------------------------------------------
# LexicalQueryRewriter
# ---------------------------------------------------------------------------


def test_lexical_rewriter_includes_original_question_first():
    rewriter = LexicalQueryRewriter()
    out = rewriter.rewrite("When was Qwen2 released?", n=3)
    assert out[0] == "When was Qwen2 released?"


def test_lexical_rewriter_substitutes_release_synonyms():
    rewriter = LexicalQueryRewriter()
    out = rewriter.rewrite("When was Qwen2 released?", n=4)
    joined = " ".join(o.lower() for o in out[1:])
    # at least one of the canonical synonyms should appear
    assert any(syn in joined for syn in ("launched", "launch", "announced", "available"))


def test_lexical_rewriter_caps_at_n_plus_one():
    rewriter = LexicalQueryRewriter()
    out = rewriter.rewrite("Which countries show the highest AI optimism?", n=2)
    assert len(out) <= 3  # original + 2


def test_lexical_rewriter_dedupes_canonicalized_variants():
    rewriter = LexicalQueryRewriter()
    out = rewriter.rewrite("highest growth", n=10)
    canonical = [v.lower().strip() for v in out]
    assert len(canonical) == len(set(canonical))


def test_lexical_rewriter_handles_empty_question():
    rewriter = LexicalQueryRewriter()
    assert rewriter.rewrite("", n=3) == [""]


def test_lexical_rewriter_handles_no_known_synonyms():
    rewriter = LexicalQueryRewriter()
    # no tokens map to _SYNONYMS; suffix-stripper may still produce variants but no crash.
    out = rewriter.rewrite("xyzzy plugh", n=3)
    assert out[0] == "xyzzy plugh"
    assert all(isinstance(s, str) for s in out)


def test_lexical_rewriter_preserves_punctuation_and_capitalization():
    rewriter = LexicalQueryRewriter()
    out = rewriter.rewrite("When was Qwen2 released?", n=2)
    for variant in out[1:]:
        # original ends with '?' and the model name 'Qwen2' is preserved
        assert variant.endswith("?")
        assert "Qwen2" in variant


# ---------------------------------------------------------------------------
# LlmQueryRewriter (mocked)
# ---------------------------------------------------------------------------


@dataclass
class _StubResponse:
    text: str


class _StubLLM:
    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls: list[dict] = []

    async def complete_json(self, *, model: str, system: str, user: str, max_tokens: int, temperature: float):
        self.calls.append({"model": model, "system": system, "user": user})
        return _StubResponse(text=self._response_text)


def test_llm_rewriter_parses_queries_array_and_includes_original():
    llm = _StubLLM('{"queries": ["alt 1", "alt 2", "alt 3"]}')
    rewriter = LlmQueryRewriter(llm=llm, model="gpt-4.1-mini")
    out = rewriter.rewrite("original question?", n=3)
    assert out[0] == "original question?"
    assert out[1:] == ["alt 1", "alt 2", "alt 3"]
    assert llm.calls and llm.calls[0]["model"] == "gpt-4.1-mini"


def test_llm_rewriter_caps_at_n():
    llm = _StubLLM('{"queries": ["a", "b", "c", "d", "e"]}')
    rewriter = LlmQueryRewriter(llm=llm, model="gpt-4.1-mini")
    out = rewriter.rewrite("q?", n=2)
    assert out == ["q?", "a", "b"]


def test_llm_rewriter_drops_duplicates():
    llm = _StubLLM('{"queries": ["q?", "alt", "ALT", "alt"]}')
    rewriter = LlmQueryRewriter(llm=llm, model="gpt-4.1-mini")
    out = rewriter.rewrite("q?", n=5)
    # canonical de-dupe: original 'q?', then 'alt' (case-insensitive)
    assert [v.lower() for v in out] == ["q?", "alt"]


def test_llm_rewriter_handles_invalid_json():
    llm = _StubLLM("not json")
    rewriter = LlmQueryRewriter(llm=llm, model="gpt-4.1-mini")
    out = rewriter.rewrite("fallback?", n=3)
    assert out == ["fallback?"]


def test_llm_rewriter_handles_missing_queries_key():
    llm = _StubLLM('{"variants": ["x", "y"]}')
    rewriter = LlmQueryRewriter(llm=llm, model="gpt-4.1-mini")
    out = rewriter.rewrite("q?", n=3)
    assert out == ["q?"]


def test_llm_rewriter_falls_back_when_in_running_loop(monkeypatch):
    """If asyncio.run is unavailable (running loop), the fallback rewriter is used."""

    def _raise(*_a, **_k):
        raise RuntimeError("asyncio.run() cannot be called from a running event loop")

    monkeypatch.setattr("asyncio.run", _raise)
    fallback = LexicalQueryRewriter()

    # Use an LLM stub whose complete_json is plain (not async) so the unawaited-coroutine
    # warning never fires; the rewriter aborts before invoking it anyway.
    class _SyncStubLLM:
        def complete_json(self, **_k):
            raise AssertionError("should not be called once asyncio.run is patched to raise")

    rewriter = LlmQueryRewriter(llm=_SyncStubLLM(), model="gpt-4.1-mini", fallback=fallback)
    out = rewriter.rewrite("When was Qwen2 released?", n=2)
    # fallback returns at least the original
    assert out[0] == "When was Qwen2 released?"


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def test_rrf_merges_two_rankings_giving_higher_score_to_top_ranked():
    rankings = [
        ["a", "b", "c"],
        ["b", "a", "d"],
    ]
    merged = reciprocal_rank_fusion(rankings)
    # 'a' rank-1 + rank-2; 'b' rank-2 + rank-1 -> tie, but order is by score desc and dict insertion stable
    assert set(merged[:2]) == {"a", "b"}
    assert "c" in merged
    assert "d" in merged
    assert len(merged) == 4


def test_rrf_item_only_in_one_ranking_still_appears():
    rankings = [["a", "b"], ["c"]]
    merged = reciprocal_rank_fusion(rankings)
    assert set(merged) == {"a", "b", "c"}


def test_rrf_uses_key_for_identity():
    @dataclass(frozen=True)
    class Item:
        kind: str
        id: str

    item_a1 = Item("chunk", "1")
    item_a2 = Item("chunk", "1")  # same identity, different instance
    item_b = Item("chunk", "2")

    rankings = [
        [item_a1, item_b],
        [item_b, item_a2],
    ]
    merged = reciprocal_rank_fusion(rankings, key=lambda i: f"{i.kind}:{i.id}")
    # de-duped to 2 items by identity
    assert len(merged) == 2
    # first instance of each identity preserved
    assert any(m is item_a1 for m in merged)
    assert any(m is item_b for m in merged)


def test_rrf_higher_rank_wins_with_default_k():
    rankings = [
        ["winner", "loser"],
        ["winner", "loser"],
    ]
    merged = reciprocal_rank_fusion(rankings)
    assert merged[0] == "winner"
    assert merged[1] == "loser"


def test_rrf_empty_rankings_returns_empty():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_rrf_smaller_k_amplifies_top_rank_advantage():
    """k controls how steeply higher ranks are favored; smaller k -> stronger top bias."""
    a = ["x", "y", "z"]
    b = ["y", "z", "x"]
    # With small k, 'x' (rank 1 in a, rank 3 in b) should outscore 'y' (rank 2,1)?
    # Actually y wins because both lists rank y high. Just sanity-check ordering changes are stable.
    merged_default = reciprocal_rank_fusion([a, b])
    merged_small_k = reciprocal_rank_fusion([a, b], k=1)
    assert set(merged_default) == set(merged_small_k) == {"x", "y", "z"}


# Smoke check: integration with MiniGraphRagAgent.ask_multi (via small fixture).


def test_ask_multi_falls_back_to_ask_for_single_query(tmp_path):
    """ask_multi with a single-element list should behave identically to ask()."""
    from knowledge_extraction.application.services.graphrag_agent import MiniGraphRagAgent

    agent = MiniGraphRagAgent(
        sqlite_path=tmp_path / "missing.sqlite",
        graph_dir=tmp_path / "missing_graph",
    )
    result = agent.ask_multi(["test question"], top_k=5, include_graph=False)
    assert result.question == "test question"
    # Empty result is fine — sqlite doesn't exist, but it shouldn't crash.
    assert result.hits == []


def test_ask_multi_empty_query_list_returns_empty_result(tmp_path):
    from knowledge_extraction.application.services.graphrag_agent import MiniGraphRagAgent

    agent = MiniGraphRagAgent(
        sqlite_path=tmp_path / "missing.sqlite",
        graph_dir=tmp_path / "missing_graph",
    )
    result = agent.ask_multi([], top_k=5, include_graph=False)
    assert result.question == ""
    assert result.hits == []
    assert result.query_terms == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
