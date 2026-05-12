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
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

# Context: any field bound here is attached to every emitted record.
_CTX: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("ke_log_ctx", default=None)  # type: ignore[arg-type]
_WIDE_STACK: contextvars.ContextVar[list[dict[str, int]]] = contextvars.ContextVar("ke_wide_stack", default=None)  # type: ignore[arg-type]
_RUN_TOKENS: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("ke_run_tokens", default=None)  # type: ignore[arg-type]


@dataclass(slots=True)
class ObservabilityConfig:
    heartbeat_interval_seconds: float = 30.0
    stall_threshold_seconds: float = 120.0
    heartbeat_enabled: bool = True


_OBSERVABILITY = ObservabilityConfig()

# Sentinel for "no progress probe value yet" — distinct from None which a probe
# may legitimately return.
_MISSING: Any = object()


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


def configure_observability(
    *,
    heartbeat_interval_seconds: float | None = None,
    stall_threshold_seconds: float | None = None,
    heartbeat_enabled: bool | None = None,
) -> None:
    global _OBSERVABILITY
    _OBSERVABILITY = ObservabilityConfig(
        heartbeat_interval_seconds=(
            heartbeat_interval_seconds
            if heartbeat_interval_seconds is not None
            else _OBSERVABILITY.heartbeat_interval_seconds
        ),
        stall_threshold_seconds=(
            stall_threshold_seconds if stall_threshold_seconds is not None else _OBSERVABILITY.stall_threshold_seconds
        ),
        heartbeat_enabled=heartbeat_enabled if heartbeat_enabled is not None else _OBSERVABILITY.heartbeat_enabled,
    )


def reset_run_token_totals() -> None:
    _RUN_TOKENS.set({"input_tokens": 0, "output_tokens": 0, "models": set()})


def get_run_token_totals() -> dict[str, Any]:
    state = _run_tokens_get()
    return {
        "input_tokens": int(state["input_tokens"]),
        "output_tokens": int(state["output_tokens"]),
        "total_tokens": int(state["input_tokens"] + state["output_tokens"]),
        "models": sorted(str(m) for m in state["models"]),
    }


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
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __setitem__(self, key: str, value: Any) -> None:
        with self.lock:
            self.fields[key] = value

    def __getitem__(self, key: str) -> Any:
        with self.lock:
            return self.fields[key]

    def update(self, **kv: Any) -> None:
        with self.lock:
            self.fields.update(kv)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.fields)


@contextmanager
def wide_event(
    name: str,
    *,
    level: int = logging.INFO,
    logger: str = "ke",
    progress_probe: Callable[[], Any] | None = None,
    **initial: Any,
) -> Iterator[WideEvent]:
    """Emit a single wide structured event covering the lifetime of the block.

    The event always logs once on exit, even on error, with duration_ms + status.
    Errors do not swallow the exception — they re-raise after logging.

    If ``progress_probe`` is supplied, the heartbeat thread calls it each
    interval; whenever the returned value changes since the previous beat the
    stall timer is reset and the new value is emitted in the heartbeat payload.
    Use this when wrapping a long-running subprocess so that real progress
    suppresses spurious ``.stalled`` warnings.
    """
    log = logging.getLogger(logger)
    ev = WideEvent(name=name, fields=dict(initial), started_ms=time.perf_counter())
    from knowledge_extraction.infrastructure.telemetry.otel_setup import span as otel_span

    parent_stack = list(_wide_stack_get())
    parent_stack.append({"input_tokens_children": 0, "output_tokens_children": 0})
    stack_token = _WIDE_STACK.set(parent_stack)
    heartbeat_stop = threading.Event()
    heartbeat_state: dict[str, Any] = {
        "count": 0,
        "stalled": False,
        "last_progress": _MISSING,
        "last_progress_ms": int((time.perf_counter() - ev.started_ms) * 1000),
    }

    def _safe_probe() -> Any:
        if progress_probe is None:
            return _MISSING
        try:
            return progress_probe()
        except Exception:  # probe must never crash the heartbeat
            return _MISSING

    def _heartbeat_loop() -> None:
        interval = max(0.001, float(_OBSERVABILITY.heartbeat_interval_seconds))
        stall_after = max(interval, float(_OBSERVABILITY.stall_threshold_seconds))
        while not heartbeat_stop.wait(interval):
            heartbeat_state["count"] += 1
            elapsed_ms = int((time.perf_counter() - ev.started_ms) * 1000)
            current_progress = _safe_probe()
            if current_progress is not _MISSING and current_progress != heartbeat_state["last_progress"]:
                heartbeat_state["last_progress"] = current_progress
                heartbeat_state["last_progress_ms"] = elapsed_ms
                heartbeat_state["stalled"] = False  # progress detected, clear stall flag
            payload = {
                "event": f"{ev.name}.heartbeat",
                "duration_ms": elapsed_ms,
                "status": ev.status,
                "heartbeat": True,
                "heartbeat_count": heartbeat_state["count"],
                **ev.snapshot(),
            }
            if heartbeat_state["last_progress"] is not _MISSING:
                payload["progress"] = heartbeat_state["last_progress"]
                payload["progress_age_ms"] = elapsed_ms - int(heartbeat_state["last_progress_ms"])
            log.log(logging.INFO, f"{ev.name}.heartbeat", extra=payload)
            since_progress = elapsed_ms - int(heartbeat_state["last_progress_ms"])
            if since_progress >= int(stall_after * 1000) and not heartbeat_state["stalled"]:
                heartbeat_state["stalled"] = True
                log.log(
                    logging.WARNING,
                    f"{ev.name}.stalled",
                    extra={
                        "event": f"{ev.name}.stalled",
                        "duration_ms": elapsed_ms,
                        "status": ev.status,
                        "heartbeat": True,
                        "heartbeat_count": heartbeat_state["count"],
                        "stall_threshold_seconds": stall_after,
                        "progress_age_ms": since_progress,
                        **ev.snapshot(),
                    },
                )

    heartbeat_thread: threading.Thread | None = None
    if _OBSERVABILITY.heartbeat_enabled:
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            name=f"wide-event-heartbeat:{name}",
            daemon=True,
        )
        heartbeat_thread.start()

    with otel_span(name, **initial) as ot_span:
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
            stack = list(_wide_stack_get())
            frame = stack[-1] if stack else {"input_tokens_children": 0, "output_tokens_children": 0}
            self_input_tokens, self_output_tokens = _extract_token_fields(ev.fields)
            total_input_tokens = self_input_tokens + int(frame["input_tokens_children"])
            total_output_tokens = self_output_tokens + int(frame["output_tokens_children"])
            model = str(ev.fields.get("model", _ctx_get().get("model", ""))).strip()
            run_tokens = _run_tokens_get()
            run_tokens["input_tokens"] += self_input_tokens
            run_tokens["output_tokens"] += self_output_tokens
            if model:
                run_tokens["models"].add(model)

            duration_ms = int((time.perf_counter() - ev.started_ms) * 1000)
            payload = {
                "event": ev.name,
                "duration_ms": duration_ms,
                "status": ev.status,
                **ev.fields,
                "input_tokens_self": self_input_tokens,
                "output_tokens_self": self_output_tokens,
                "total_tokens_self": self_input_tokens + self_output_tokens,
                "input_tokens_total": total_input_tokens,
                "output_tokens_total": total_output_tokens,
                "total_tokens_total": total_input_tokens + total_output_tokens,
                "heartbeat_count": heartbeat_state["count"],
                "stalled": heartbeat_state["stalled"],
            }
            if ev.error:
                payload["error"] = ev.error
                payload["error_type"] = ev.error_type
            if ot_span is not None:
                for k, v in payload.items():
                    if k == "event":
                        continue
                    ot_span.set_attribute(f"event.{k}", _otel_attr(v))
            log.log(
                logging.ERROR if ev.status == "error" else level,
                ev.name,
                extra=payload,
            )
            if stack:
                stack.pop()
                if stack:
                    stack[-1]["input_tokens_children"] += total_input_tokens
                    stack[-1]["output_tokens_children"] += total_output_tokens
            _WIDE_STACK.set(stack)
            _RUN_TOKENS.set(run_tokens)
            _WIDE_STACK.reset(stack_token)
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1.0)


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
                  "output_tokens", "input_tokens_self", "output_tokens_self", "total_tokens_self",
                  "input_tokens_total", "output_tokens_total", "total_tokens_total",
                  "nodes", "edges", "chunk_id", "ontology_version"):
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


def _otel_attr(v: Any) -> Any:
    if isinstance(v, str | bool | int | float):
        return v
    if v is None:
        return "null"
    if isinstance(v, list | tuple | set):
        out: list[Any] = []
        for x in v:
            if isinstance(x, str | bool | int | float):
                out.append(x)
            else:
                out.append(str(x))
        return out
    if isinstance(v, dict):
        return orjson.dumps(_safe(v)).decode("utf-8")
    return str(v)


def _wide_stack_get() -> list[dict[str, int]]:
    v = _WIDE_STACK.get()
    return list(v) if v is not None else []


def _run_tokens_get() -> dict[str, Any]:
    v = _RUN_TOKENS.get()
    if v is None:
        return {"input_tokens": 0, "output_tokens": 0, "models": set()}
    return {
        "input_tokens": int(v.get("input_tokens", 0)),
        "output_tokens": int(v.get("output_tokens", 0)),
        "models": set(v.get("models", set())),
    }


def _extract_token_fields(fields: dict[str, Any]) -> tuple[int, int]:
    input_candidates = (
        fields.get("input_tokens"),
        fields.get("prompt_tokens"),
    )
    output_candidates = (
        fields.get("output_tokens"),
        fields.get("completion_tokens"),
    )
    input_tokens = _first_int(input_candidates)
    output_tokens = _first_int(output_candidates)
    return input_tokens, output_tokens


def _first_int(candidates: tuple[Any, ...]) -> int:
    for candidate in candidates:
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, float):
            return int(candidate)
        if isinstance(candidate, str):
            parsed = candidate.strip()
            if parsed.isdigit():
                return int(parsed)
    return 0


# -- async-friendly wrapper -------------------------------------------------

@contextmanager
def stage_event(stage: str, **initial: Any) -> Generator[WideEvent, None, None]:
    """Convenience: wide_event with name='pipeline.stage' and stage bound."""
    with bound(stage=stage), wide_event("pipeline.stage", stage=stage, **initial) as ev:
        yield ev
