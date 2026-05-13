"""Query rewriting for retrieval recall.

Two implementations behind one Protocol:

* :class:`LexicalQueryRewriter` — heuristic synonym/lemma expansion. Free, fast,
  no network calls. Good for date/release questions ("released" → "launched",
  "announced", "shipped") and ranking superlatives ("highest" → "top", "most").

* :class:`LlmQueryRewriter` — generates N short paraphrases via the Foundry
  chat client. Better recall at the cost of one extra LLM call per question.

Both return a list whose first element is ALWAYS the original question, so a
caller can blindly take ``rewriter.rewrite(q)`` without losing the user input.

Combined with :func:`reciprocal_rank_fusion` over per-variant retrieval results,
this is the simplest "multi-query RAG" pattern that consistently beats the
single-query baseline on factoid+synonym-heavy questions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")

# Synonym fan-out for the verbs and superlatives that show up most often in
# release/launch/comparison questions over research reports. Keep it tight:
# every extra term costs precision in BM25-style scoring.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "released": ("release", "launched", "launch", "announced", "available", "shipped", "debuted"),
    "release":  ("released", "launched", "launch", "announced", "available", "shipped"),
    "launched": ("launch", "released", "release", "announced"),
    "launch":   ("launched", "released", "release"),
    "announced": ("announcement", "released", "unveiled", "introduced"),
    "introduced": ("introduce", "released", "launched"),
    "available": ("released", "launched", "shipped"),
    "highest":  ("top", "most", "leading", "largest", "greatest"),
    "lowest":   ("bottom", "least", "smallest", "weakest"),
    "biggest":  ("largest", "top", "most", "greatest"),
    "smallest": ("lowest", "least"),
    "growth":   ("growing", "increase", "increased", "rose", "rise", "gain"),
    "decline":  ("declined", "decrease", "decreased", "fell", "drop", "dropped"),
    "compare":  ("comparison", "versus", "vs"),
    "trend":    ("trends", "trending", "trajectory"),
    "investment": ("invested", "funding", "funded", "spend", "spending"),
    "spending": ("spend", "investment", "expenditure"),
    "patents":  ("patent", "filings", "granted"),
    "regulation": ("regulations", "regulatory", "law", "laws", "rule", "rules"),
    "country":  ("countries", "nation", "nations"),
    "countries": ("country", "nation", "nations"),
    "model":    ("models", "llm", "system"),
    "models":   ("model", "llms", "systems"),
}

# Verb suffix collapse (very small lemmatizer). Order matters: longer first.
_SUFFIXES: tuple[str, ...] = ("ing", "ed", "es", "s")


class QueryRewriter(Protocol):
    """Returns a list of query variants. The first element MUST be the original."""

    def rewrite(self, question: str, n: int = 3) -> list[str]: ...


# ---------------------------------------------------------------------------
# Lexical (free, no-LLM)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LexicalQueryRewriter:
    """Heuristic expansion using a tight synonym + suffix table.

    Generates up to ``n`` variants by:
      1. Replacing one synonym-rich token at a time (keeps variants close to original).
      2. Stripping/adding ``ing|ed|es|s`` suffix variants for any unmatched verb-like token.
    """

    def rewrite(self, question: str, n: int = 3) -> list[str]:
        if not question.strip():
            return [question]
        variants: list[str] = [question]
        seen: set[str] = {_canon(question)}
        for variant in self._candidates(question):
            key = _canon(variant)
            if key in seen:
                continue
            seen.add(key)
            variants.append(variant)
            if len(variants) > n:
                break
        return variants

    def _candidates(self, question: str) -> list[str]:
        tokens = _tokenize(question)
        out: list[str] = []
        # Synonym substitution: one token at a time.
        for idx, tok in enumerate(tokens):
            for syn in _SYNONYMS.get(tok.lower(), ()):
                rebuilt = _replace_token(question, idx, syn)
                if rebuilt:
                    out.append(rebuilt)
        # Suffix variants for verb-like tokens that aren't in the synonym table.
        for idx, tok in enumerate(tokens):
            if tok.lower() in _SYNONYMS:
                continue
            for variant in _suffix_variants(tok):
                if variant.lower() == tok.lower():
                    continue
                rebuilt = _replace_token(question, idx, variant)
                if rebuilt:
                    out.append(rebuilt)
        return out


# ---------------------------------------------------------------------------
# LLM-backed
# ---------------------------------------------------------------------------


_REWRITER_SYSTEM = (
    "You rewrite user questions to maximize retrieval recall over a research "
    "corpus. For each question you receive, generate short alternative "
    "phrasings that:\n"
    "  - use synonyms for verbs (released -> launched, announced; compare -> versus)\n"
    "  - expand or contract acronyms (LLM <-> large language model)\n"
    "  - reorder clauses or change the question form\n"
    "  - keep the same intent and named entities\n"
    "Each variant must be at most 20 words. Do not explain, do not number them.\n"
    'Respond with a single JSON object: {"queries": ["variant 1", "variant 2", ...]}'
)


@dataclass(slots=True)
class LlmQueryRewriter:
    """Generates paraphrases via the Foundry chat client (async under the hood)."""

    llm: object  # AzureFoundryLLM, kept as object to avoid cyclic imports
    model: str
    fallback: QueryRewriter | None = None

    def rewrite(self, question: str, n: int = 3) -> list[str]:
        if not question.strip():
            return [question]
        coro = self._rewrite_async(question, n)
        try:
            return asyncio.run(coro)
        except RuntimeError as exc:
            # Already inside a running loop (rare in CLI; common in notebooks/servers).
            coro.close()  # avoid "coroutine was never awaited" RuntimeWarning
            log.warning("LlmQueryRewriter: cannot use asyncio.run (%s); using fallback", exc)
            if self.fallback is not None:
                return self.fallback.rewrite(question, n)
            return [question]
        except Exception as exc:  # pragma: no cover - defensive
            coro.close()
            log.warning("LlmQueryRewriter failed (%s); using fallback", exc)
            if self.fallback is not None:
                return self.fallback.rewrite(question, n)
            return [question]

    async def _rewrite_async(self, question: str, n: int) -> list[str]:
        user = f"Question: {question}\n\nReturn {n} alternative phrasings."
        response = await self.llm.complete_json(  # type: ignore[attr-defined]
            model=self.model,
            system=_REWRITER_SYSTEM,
            user=user,
            max_tokens=400,
            temperature=0.7,
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            log.warning("LlmQueryRewriter: non-JSON response, falling back to original")
            return [question]
        raw = payload.get("queries") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return [question]
        out: list[str] = [question]
        seen: set[str] = {_canon(question)}
        for item in raw:
            if not isinstance(item, str):
                continue
            cleaned = item.strip()
            if not cleaned:
                continue
            key = _canon(cleaned)
            if key in seen:
                continue
            seen.add(key)
            out.append(cleaned)
            if len(out) > n:
                break
        return out


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion[T](
    rankings: list[list[T]],
    *,
    key=lambda x: x,
    k: int = 60,
) -> list[T]:
    """Merge multiple ranked lists into one via Reciprocal Rank Fusion.

    Each item gets a score of ``sum(1 / (k + rank))`` over all rankings it appears in
    (1-indexed rank). ``key`` is called to derive a hashable identity from each item;
    by default the item itself is the key. Items with the same key in different
    rankings get their scores added; the highest score wins.

    Returns one merged list, ordered by descending RRF score, preserving the FIRST
    item instance seen for each key (so caller-set fields like ``score`` come from
    the highest-ranked occurrence).

    Reference: Cormack et al., "Reciprocal Rank Fusion outperforms Condorcet and
    individual Rank Learning Methods" (SIGIR 2009). k=60 is the standard default.
    """
    scores: dict[object, float] = {}
    first_seen: dict[object, object] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            ident = key(item)
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (k + rank)
            if ident not in first_seen:
                first_seen[ident] = item
    return [first_seen[ident] for ident, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _canon(text: str) -> str:
    """Lowercase + collapse whitespace for de-duping near-identical variants."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _replace_token(question: str, target_idx: int, replacement: str) -> str:
    """Replace the Nth token (0-indexed) in ``question`` with ``replacement``, preserving non-token chars."""
    out: list[str] = []
    cursor = 0
    seen = -1
    for match in _TOKEN_RE.finditer(question):
        seen += 1
        if seen == target_idx:
            out.append(question[cursor:match.start()])
            out.append(replacement)
            cursor = match.end()
            break
    if seen != target_idx:
        return ""
    out.append(question[cursor:])
    return "".join(out)


def _suffix_variants(token: str) -> list[str]:
    """For a verb-like token, return the bare stem plus common suffix variants."""
    low = token.lower()
    if len(low) <= 3:
        return []
    # Strip a known suffix, then re-attach others.
    stem = low
    for suffix in _SUFFIXES:
        if low.endswith(suffix) and len(low) - len(suffix) >= 3:
            stem = low[: -len(suffix)]
            break
    if stem == low:
        return []
    return [stem, stem + "s", stem + "ed", stem + "ing"]
