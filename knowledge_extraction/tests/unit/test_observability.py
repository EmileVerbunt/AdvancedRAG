"""Tests for the observability layer: wide events, bound context, JSON formatter."""
from __future__ import annotations

import io
import json
import logging
import time

import pytest

from knowledge_extraction.infrastructure.telemetry.observability import (
    _ConsoleFormatter,
    _JsonFormatter,
    bound,
    configure_observability,
    get_run_token_totals,
    new_run_id,
    reset_run_token_totals,
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
    assert rec["input_tokens_self"] == 0
    assert rec["output_tokens_self"] == 0


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
    record.total_tokens_total = 999
    out = formatter.format(record)
    assert "event=stage.run" in out
    assert "stage=extract" in out
    assert "duration_ms=123" in out
    assert "model=gpt-5.4" in out
    assert "total_tokens_total=999" in out


def test_wide_event_tracks_nested_token_totals_and_run_totals(json_handler):
    reset_run_token_totals()
    with wide_event("outer", model="gpt-5.4") as outer:
        outer["input_tokens"] = 10
        with wide_event("inner", model="gpt-5.4") as inner:
            inner["input_tokens"] = 3
            inner["output_tokens"] = 4
        outer["output_tokens"] = 5

    records = _last_records(json_handler)
    inner_rec = next(r for r in records if r["event"] == "inner")
    outer_rec = next(r for r in records if r["event"] == "outer")
    assert inner_rec["input_tokens_self"] == 3
    assert inner_rec["output_tokens_self"] == 4
    assert inner_rec["total_tokens_total"] == 7
    assert outer_rec["input_tokens_self"] == 10
    assert outer_rec["output_tokens_self"] == 5
    assert outer_rec["total_tokens_total"] == 22

    run_totals = get_run_token_totals()
    assert run_totals["input_tokens"] == 13
    assert run_totals["output_tokens"] == 9
    assert run_totals["total_tokens"] == 22
    assert run_totals["models"] == ["gpt-5.4"]


def test_wide_event_emits_heartbeat_and_stall_markers(json_handler):
    configure_observability(
        heartbeat_enabled=True,
        heartbeat_interval_seconds=0.01,
        stall_threshold_seconds=0.02,
    )
    try:
        with wide_event("test.stall", model="gpt-5.4"):
            time.sleep(0.05)
    finally:
        configure_observability(
            heartbeat_enabled=True,
            heartbeat_interval_seconds=30.0,
            stall_threshold_seconds=120.0,
        )

    records = _last_records(json_handler)
    assert any(r.get("event") == "test.stall.heartbeat" for r in records)
    assert any(r.get("event") == "test.stall.stalled" for r in records)


def test_progress_probe_resets_stall_timer(json_handler):
    """A probe whose value keeps changing must keep the .stalled warning quiet."""
    configure_observability(
        heartbeat_enabled=True,
        heartbeat_interval_seconds=0.005,
        stall_threshold_seconds=0.02,
    )
    counter = {"n": 0}

    def probe() -> int:
        counter["n"] += 1
        return counter["n"]

    try:
        with wide_event("test.progress", progress_probe=probe):
            time.sleep(0.06)
    finally:
        configure_observability(
            heartbeat_enabled=True,
            heartbeat_interval_seconds=30.0,
            stall_threshold_seconds=120.0,
        )

    records = _last_records(json_handler)
    heartbeats = [r for r in records if r.get("event") == "test.progress.heartbeat"]
    assert heartbeats, "expected heartbeats to be emitted"
    assert any("progress" in r for r in heartbeats), "expected probe value in payload"
    assert not any(r.get("event") == "test.progress.stalled" for r in records), \
        "probe progress should suppress stall warning"


def test_progress_probe_failure_does_not_crash_heartbeat(json_handler):
    """If the probe raises, the heartbeat must keep ticking without the value."""
    configure_observability(
        heartbeat_enabled=True,
        heartbeat_interval_seconds=0.01,
        stall_threshold_seconds=10.0,
    )

    def bad_probe() -> int:
        raise RuntimeError("boom")

    try:
        with wide_event("test.bad_probe", progress_probe=bad_probe):
            time.sleep(0.04)
    finally:
        configure_observability(
            heartbeat_enabled=True,
            heartbeat_interval_seconds=30.0,
            stall_threshold_seconds=120.0,
        )

    records = _last_records(json_handler)
    heartbeats = [r for r in records if r.get("event") == "test.bad_probe.heartbeat"]
    assert heartbeats, "heartbeat must continue even when probe raises"
