"""Unit tests for `_extract_json` response cleanup in the Azure Foundry client."""
from __future__ import annotations

import orjson

from knowledge_extraction.infrastructure.llm.azure_foundry_client import _extract_json


def test_extract_json_passes_through_single_object() -> None:
    assert orjson.loads(_extract_json('{"tool": "finish", "args": {}}')) == {
        "tool": "finish",
        "args": {},
    }


def test_extract_json_strips_code_fence() -> None:
    text = '```json\n{"tool": "open_document", "args": {"document_id": "d1"}}\n```'
    assert orjson.loads(_extract_json(text))["tool"] == "open_document"


def test_extract_json_returns_first_of_duplicated_objects() -> None:
    # gpt-5.x has been observed emitting the object twice; the greedy first-{..last-}
    # span would concatenate both and break json parsing. Brace-matching returns the
    # first complete object so it round-trips cleanly.
    dup = (
        '{"thought":"a","tool":"search_document","args":{"document_id":"d1","query":"x"}}\n'
        '{"thought":"a","tool":"search_document","args":{"document_id":"d1","query":"x"}}'
    )
    parsed = orjson.loads(_extract_json(dup))
    assert parsed["tool"] == "search_document"
    assert parsed["args"] == {"document_id": "d1", "query": "x"}


def test_extract_json_handles_braces_inside_strings() -> None:
    text = '{"thought":"use {curly} braces","tool":"finish","args":{}}'
    parsed = orjson.loads(_extract_json(text))
    assert parsed["tool"] == "finish"
    assert parsed["thought"] == "use {curly} braces"
