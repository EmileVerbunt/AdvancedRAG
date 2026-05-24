from __future__ import annotations

from dataclasses import dataclass

from knowledge_extraction.application.services.text_dedup import (
    dedupe_by_text,
    text_fingerprint,
)


@dataclass(slots=True)
class _Item:
    text: str
    score: float = 0.0


def test_fingerprint_normalizes_whitespace_and_case() -> None:
    a = text_fingerprint("Hello   World\n\nFoo")
    b = text_fingerprint("HELLO world  foo")
    assert a == b == "hello world foo"


def test_fingerprint_truncates_to_prefix_chars() -> None:
    fp = text_fingerprint("abcdef" * 100, prefix_chars=10)
    assert fp == "abcdefabcd"
    assert len(fp) == 10


def test_fingerprint_empty_returns_empty() -> None:
    assert text_fingerprint("") == ""
    assert text_fingerprint("   \t\n  ") == ""


def test_dedupe_keeps_first_occurrence_after_sort() -> None:
    items = [
        _Item("Same chunk text from doc A", score=0.9),
        _Item("Different content entirely", score=0.7),
        _Item("Same chunk text from doc A", score=0.5),  # duplicate of #0
        _Item("Yet another paragraph", score=0.4),
    ]
    out = dedupe_by_text(items, key=lambda i: i.text)
    assert [i.score for i in out] == [0.9, 0.7, 0.4]


def test_dedupe_preserves_items_with_empty_text() -> None:
    """Empty-text items have no fingerprint so they cannot collide; keep all."""
    items = [_Item(""), _Item(""), _Item("content"), _Item("")]
    out = dedupe_by_text(items, key=lambda i: i.text)
    assert len(out) == 4


def test_dedupe_uses_prefix_for_long_text() -> None:
    """Two chunks with identical leading text but different tails collapse."""
    a = "Lorem ipsum dolor sit amet. " * 30 + " variant A"
    b = "Lorem ipsum dolor sit amet. " * 30 + " variant B"
    items = [_Item(a), _Item(b)]
    out = dedupe_by_text(items, key=lambda i: i.text, prefix_chars=100)
    assert len(out) == 1


def test_dedupe_on_dicts() -> None:
    """Evidence panel use case: dict items keyed by snippet field."""
    items = [
        {"kind": "chunk", "snippet": "AI Index 2025 reports..."},
        {"kind": "claim", "snippet": "AI Index 2025 reports..."},  # different kind, same text
        {"kind": "chunk", "snippet": "China leads in robot installations"},
    ]
    out = dedupe_by_text(items, key=lambda d: str(d["snippet"]))
    assert len(out) == 2
    assert out[0]["kind"] == "chunk"
    assert out[1]["snippet"].startswith("China leads")
