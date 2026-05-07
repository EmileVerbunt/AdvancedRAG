"""Tests for the observability layer: wide events, bound context, JSON formatter."""
from __future__ import annotations

import io
import json
import logging

import pytest

from knowledge_extraction.infrastructure.telemetry.observability import (
    _ConsoleFormatter,
    _JsonFormatter,
    bound,
    new_run_id,
    setup_logging,
    wide_event,
)


@pytest.fixture
def json_handler():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    prev = list(root.handlers)
    root.handlers = [h]
    prev_level = root.level
    root.setLevel(logging.INFO)
    try:
        yield buf
    finally:
        root.handlers = prev
        root.setLevel(prev_level)


def _last_records(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_wide_event_emits_one_record_with_duration(json_handler):
    with wide_event("test.work", widget="frob") as ev:
        ev["count"] = 42
    rec = _last_records(json_handler)[-1]
    assert rec["event"] == "test.work"
    assert rec["status"] == "ok"
    assert rec["count"] == 42
    assert rec["widget"] == "frob"
    assert isinstance(rec["duration_ms"], int)


def test_wide_event_records_error_and_reraises(json_handler):
    with pytest.raises(ValueError), wide_event("test.boom"):
        raise ValueError("nope")
    rec = _last_records(json_handler)[-1]
    assert rec["event"] == "test.boom"
    assert rec["status"] == "error"
    assert rec["error_type"] == "ValueError"
    assert "nope" in rec["error"]


def test_bound_context_is_attached_and_isolated(json_handler):
    with bound(run_id="r1", document_id="d1"), wide_event("scoped.event"):
        pass
    with wide_event("outside.event"):
        pass
    records = _last_records(json_handler)
    inner = next(r for r in records if r["event"] == "scoped.event")
    outer = next(r for r in records if r["event"] == "outside.event")
    assert inner["run_id"] == "r1"
    assert inner["document_id"] == "d1"
    assert "run_id" not in outer
    assert "document_id" not in outer


def test_setup_logging_writes_to_file(tmp_path):
    log_dir = tmp_path / "logs"
    setup_logging(log_dir=log_dir, run_id="testrun", console_format="json")
    log = logging.getLogger("ke")
    with bound(run_id=new_run_id()), wide_event("test.file_sink", k="v"):
        pass
    log.info("plain")
    for h in list(logging.getLogger().handlers):
        h.flush()
    files = list(log_dir.glob("run-*-testrun.jsonl"))
    assert len(files) == 1
    contents = files[0].read_text(encoding="utf-8").splitlines()
    assert any('"event":"test.file_sink"' in line for line in contents)
    assert any('"k":"v"' in line for line in contents)


def test_console_formatter_includes_high_signal_fields():
    formatter = _ConsoleFormatter()
    record = logging.LogRecord(
        name="ke", level=logging.INFO, pathname="x", lineno=1, msg="hi", args=(), exc_info=None,
    )
    record.event = "stage.run"
    record.stage = "extract"
    record.duration_ms = 123
    record.model = "gpt-5.4"
    out = formatter.format(record)
    assert "event=stage.run" in out
    assert "stage=extract" in out
    assert "duration_ms=123" in out
    assert "model=gpt-5.4" in out
