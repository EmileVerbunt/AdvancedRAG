"""OpenTelemetry tracing setup with local JSONL sink + optional OTLP export."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import orjson

log = logging.getLogger(__name__)
_CONFIGURED = False


def _trace_id_hex(value: int) -> str:
    return f"{value:032x}"


def _span_id_hex(value: int) -> str:
    return f"{value:016x}"


class _JsonlSpanExporter:
    """Write spans to a local JSONL file for offline inspection."""

    def __init__(self, sink_path: Path) -> None:
        self._sink_path = sink_path
        self._sink_path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans) -> object:  # pragma: no cover - signature required by OTEL
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            with self._sink_path.open("ab") as f:
                for s in spans:
                    ctx = s.get_span_context()
                    payload = {
                        "trace_id": _trace_id_hex(ctx.trace_id),
                        "span_id": _span_id_hex(ctx.span_id),
                        "parent_span_id": _span_id_hex(s.parent.span_id) if s.parent is not None else None,
                        "name": s.name,
                        "kind": str(s.kind),
                        "start_time_unix_nano": s.start_time,
                        "end_time_unix_nano": s.end_time,
                        "duration_ms": int((s.end_time - s.start_time) / 1_000_000),
                        "status_code": s.status.status_code.name,
                        "status_description": s.status.description,
                        "attributes": dict(s.attributes or {}),
                        "events": [
                            {"name": ev.name, "timestamp_unix_nano": ev.timestamp, "attributes": dict(ev.attributes or {})}
                            for ev in s.events
                        ],
                        "resource": dict(s.resource.attributes),
                    }
                    f.write(orjson.dumps(payload))
                    f.write(b"\n")
            return SpanExportResult.SUCCESS
        except OSError as exc:
            log.warning("otel.local_export_failed path=%s error=%s", self._sink_path, exc)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:  # pragma: no cover - required by OTEL
        return None


@contextmanager
def span(name: str, **attrs: object) -> Iterator[object | None]:
    """Tracing span wrapper that no-ops when OTEL is unavailable."""
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
        from opentelemetry.trace import Status, StatusCode
    except Exception:
        yield None
        return

    tracer = trace.get_tracer("knowledge_extraction")
    with tracer.start_as_current_span(name) as current:
        try:
            from knowledge_extraction.infrastructure.telemetry.observability import get_context

            for k, v in get_context().items():
                current.set_attribute(f"ctx.{k}", _attr_value(v))
        except Exception:
            pass
        for k, v in attrs.items():
            current.set_attribute(k, _attr_value(v))
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def setup_otel(
    enabled: bool,
    endpoint: str | None,
    *,
    local_sink_path: Path | None,
    service_name: str = "knowledge_extraction",
) -> None:
    global _CONFIGURED
    if not enabled or _CONFIGURED:
        return

    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception as exc:
        log.warning("otel.disabled reason=%s", exc)
        return

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))

    if local_sink_path is not None:
        provider.add_span_processor(BatchSpanProcessor(_JsonlSpanExporter(local_sink_path)))

    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        except Exception as exc:
            log.warning("otel.otlp_exporter_unavailable endpoint=%s error=%s", endpoint, exc)

    trace.set_tracer_provider(provider)
    _CONFIGURED = True


def _attr_value(value: object) -> Any:
    if isinstance(value, str | bool | int | float):
        return value
    if value is None:
        return "null"
    if isinstance(value, list | tuple | set):
        out: list[Any] = []
        for item in value:
            if isinstance(item, str | bool | int | float):
                out.append(item)
            else:
                out.append(str(item))
        return out
    return str(value)
