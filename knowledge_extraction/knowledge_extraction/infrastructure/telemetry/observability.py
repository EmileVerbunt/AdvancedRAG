"""Idiomatic observability: structured logging, wide events, bound context.

Design
------
* One JSON record per logical operation (Honeycomb-style "wide event"): every
  event carries the *full set* of dimensions for the unit of work (run_id,
  document_id, stage, model, tokens, latency, ontology_version, status, error).
* Context propagates implicitly via contextvars so every log call inside a
  bound scope is automatically enriched.
* Two sinks:
    - stderr, human-friendly (or JSON if LOG_FORMAT=json).
    - rotating JSONL file at LOG_FILE_PATH (always JSON).
* Standard library only (stdlib + orjson). No structlog dependency required.

Usage
-----
    setup_logging(settings)
    with bound(run_id="...", command="extract"):
        with wide_event("llm.complete_json", model="gpt-5.4") as ev:
            ev["input_tokens"] = 312
            ev["output_tokens"] = 87

Every emitted record contains: ts, level, logger, event, duration_ms,
status, msg, plus all bound context and ev fields.
"""
from __future__ import annotations

import contextvars
import logging
import sys
import time
import traceback
import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

# Context: any field bound here is attached to every emitted record.
_CTX: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("ke_log_ctx", default=None)  # type: ignore[arg-type]


def _ctx_get() -> dict[str, Any]:
    v = _CTX.get()
    return v if v is not None else {}

# Reserved LogRecord attribute names (skip when serializing extras)
_RESERVED = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info", "thread",
    "threadName", "taskName", "getMessage",
})


def get_context() -> dict[str, Any]:
    return dict(_ctx_get())


@contextmanager
def bound(**fields: Any) -> Iterator[None]:
    """Bind `fields` into the current logging context for the duration of the block."""
    current = _ctx_get()
    token = _CTX.set({**current, **fields})
    try:
        yield
    finally:
        _CTX.reset(token)


def bind(**fields: Any) -> None:
    """Permanently merge fields into the current context (no auto-unbind).

    Use sparingly — prefer `bound()` for scoped binding.
    """
    _CTX.set({**_ctx_get(), **fields})


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class WideEvent:
    """Mutable event accumulator. Use as a dict-like via ev[key] = value."""
    name: str
    fields: dict[str, Any] = field(default_factory=dict)
    started_ms: float = 0.0
    status: str = "ok"
    error: str | None = None
    error_type: str | None = None

    def __setitem__(self, key: str, value: Any) -> None:
        self.fields[key] = value

    def __getitem__(self, key: str) -> Any:
        return self.fields[key]

    def update(self, **kv: Any) -> None:
        self.fields.update(kv)


@contextmanager
def wide_event(name: str, *, level: int = logging.INFO, logger: str = "ke", **initial: Any) -> Iterator[WideEvent]:
    """Emit a single wide structured event covering the lifetime of the block.

    The event always logs once on exit, even on error, with duration_ms + status.
    Errors do not swallow the exception — they re-raise after logging.
    """
    log = logging.getLogger(logger)
    ev = WideEvent(name=name, fields=dict(initial), started_ms=time.perf_counter())
    try:
        yield ev
    except Exception as exc:
        ev.status = "error"
        ev.error = str(exc)[:512]
        ev.error_type = type(exc).__name__
        # capture last frame for triage without bloating event
        tb = traceback.extract_tb(exc.__traceback__)
        if tb:
            last = tb[-1]
            ev["error_at"] = f"{last.filename}:{last.lineno}"
        raise
    finally:
        duration_ms = int((time.perf_counter() - ev.started_ms) * 1000)
        payload = {
            "event": ev.name,
            "duration_ms": duration_ms,
            "status": ev.status,
            **ev.fields,
        }
        if ev.error:
            payload["error"] = ev.error
            payload["error_type"] = ev.error_type
        log.log(
            logging.ERROR if ev.status == "error" else level,
            ev.name,
            extra=payload,
        )


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per record, merging contextvars + record extras."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = _ctx_get()
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge context (bound) -- explicit fields take priority over bound ones.
        for k, v in ctx.items():
            payload.setdefault(k, _safe(v))
        # Merge extras (record.__dict__ keys that aren't reserved).
        for k, v in record.__dict__.items():
            if k in _RESERVED or k.startswith("_"):
                continue
            payload[k] = _safe(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return orjson.dumps(payload).decode("utf-8")


class _ConsoleFormatter(logging.Formatter):
    """Concise human-readable console formatter that surfaces wide-event fields inline."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = _ctx_get()
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        level = record.levelname.ljust(5)
        run = ctx.get("run_id", "")
        run_part = f" run={run}" if run else ""
        # Pick a small set of high-signal extras to surface inline.
        extras: list[str] = []
        for k in ("event", "stage", "duration_ms", "status", "model", "input_tokens",
                  "output_tokens", "nodes", "edges", "chunk_id", "ontology_version"):
            if k in record.__dict__ and record.__dict__[k] not in (None, ""):
                extras.append(f"{k}={record.__dict__[k]}")
        tail = (" " + " ".join(extras)) if extras else ""
        msg = record.getMessage()
        line = f"{ts} {level}{run_part} {record.name}: {msg}{tail}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: Path | None = None,
    run_id: str | None = None,
    console_format: str = "console",
) -> Path | None:
    """Initialise root logger with stderr + per-run JSONL file.

    A fresh log file is created for every pipeline invocation:
    ``<log_dir>/run-YYYYMMDD-HHMMSS-<run_id>.jsonl``. Returns the path to the
    file (or None if log_dir is not provided). Idempotent.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(_JsonFormatter() if console_format == "json" else _ConsoleFormatter())
    stderr.setLevel(level)
    root.addHandler(stderr)

    log_path: Path | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        rid = run_id or new_run_id()
        log_path = log_dir / f"run-{ts}-{rid}.jsonl"
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(_JsonFormatter())
        fh.setLevel(level)
        root.addHandler(fh)

    # Tame chatty third-party loggers.
    for noisy in ("httpx", "httpcore", "azure", "azure.identity", "azure.core",
                  "openai._base_client", "urllib3", "filelock", "docling"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_path


def _safe(v: Any) -> Any:
    """Convert non-JSON-native values to something orjson can serialise."""
    if isinstance(v, str | int | float | bool | type(None)):
        return v
    if isinstance(v, (list, tuple, set)):
        return [_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _safe(x) for k, x in v.items()}
    return str(v)


# -- async-friendly wrapper -------------------------------------------------

@contextmanager
def stage_event(stage: str, **initial: Any) -> Generator[WideEvent, None, None]:
    """Convenience: wide_event with name='pipeline.stage' and stage bound."""
    with bound(stage=stage), wide_event("pipeline.stage", stage=stage, **initial) as ev:
        yield ev
