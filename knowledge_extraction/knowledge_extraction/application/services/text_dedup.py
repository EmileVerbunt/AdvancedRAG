"""Text-prefix de-duplication shared across retrieval + evidence assembly.

The SQLite store can legitimately contain near-duplicate chunks — most often
because the same PDF was ingested twice (e.g. a `--pages N` slice run plus a
full run produces two documents with the same chunk text but different
``document_id``). Retrievers must not blow user-visible top-K with these
twins; nor should the chat evidence panel show the same paragraph twice.

This module deliberately stays tiny — no class, one function, no dependencies.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable

_WS_RE = re.compile(r"\s+")


def text_fingerprint(text: str, *, prefix_chars: int = 200) -> str:
    """Return a normalized fingerprint of ``text``'s leading ``prefix_chars``.

    Lowercases, collapses whitespace, and trims. Tuned to catch chunks whose
    leading content matches but whose tail might differ slightly (e.g. trailing
    page-footer text).
    """
    if not text:
        return ""
    normalized = _WS_RE.sub(" ", text.strip().lower())
    return normalized[:prefix_chars]


def dedupe_by_text[T](
    items: Iterable[T],
    *,
    key: Callable[[T], str],
    prefix_chars: int = 200,
) -> list[T]:
    """Keep the first occurrence of each text fingerprint, preserving order.

    Items whose ``key(item)`` is empty are kept as-is (they cannot collide).
    Use after sorting by score so the highest-ranked twin survives.
    """
    seen: set[str] = set()
    out: list[T] = []
    for item in items:
        fp = text_fingerprint(key(item), prefix_chars=prefix_chars)
        if not fp:
            out.append(item)
            continue
        if fp in seen:
            continue
        seen.add(fp)
        out.append(item)
    return out


__all__ = ["dedupe_by_text", "text_fingerprint"]
