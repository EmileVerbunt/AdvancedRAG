"""Optional OpenTelemetry tracing setup (no-op if disabled)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def span(name: str, **attrs: object) -> Iterator[None]:
    """Lightweight span context manager. Real OTEL wiring is added when enabled."""
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]

        tracer = trace.get_tracer("knowledge_extraction")
        with tracer.start_as_current_span(name) as s:
            for k, v in attrs.items():
                s.set_attribute(k, v)  # type: ignore[arg-type]
            yield
    except Exception:
        yield


def setup_otel(enabled: bool, endpoint: str | None) -> None:
    if not enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(resource=Resource.create({"service.name": "knowledge_extraction"}))
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
    except Exception:
        pass
