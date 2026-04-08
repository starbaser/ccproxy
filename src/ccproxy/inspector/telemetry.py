"""OpenTelemetry span emission for inspector traffic capture.

Provides an InspectorTracer that emits OTel spans for each HTTP flow, with
graceful degradation when OTel packages are not installed.

Three operational modes:
1. OTel enabled + packages present → real tracer with OTLP export
2. OTel disabled + API package present → no-op tracer (zero overhead)
3. No OTel packages at all → stub (zero overhead, no imports)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.inspector.flow_store import FlowRecord, InspectorMeta, OtelMeta

if TYPE_CHECKING:
    from mitmproxy import http

logger = logging.getLogger(__name__)

_provider: Any = None

_PROVIDER_MAP = {
    "api.anthropic.com": "anthropic",
    "api.openai.com": "openai",
    "generativelanguage.googleapis.com": "google",
    "openrouter.ai": "openrouter",
}


class InspectorTracer:
    """Wraps OTel span lifecycle for inspector addon flows."""

    def __init__(
        self,
        enabled: bool = False,
        otlp_endpoint: str = "http://localhost:4317",
        service_name: str = "ccproxy",
        provider_map: dict[str, str] | None = None,
    ) -> None:
        self._tracer: Any = None
        self._enabled = enabled
        self._provider_map = provider_map if provider_map is not None else _PROVIDER_MAP

        if not enabled:
            return

        try:
            self._tracer = _init_otel_tracer(service_name, otlp_endpoint)
            logger.info("OTel tracer initialized, exporting to %s", otlp_endpoint)
        except ImportError:
            logger.warning("opentelemetry packages not installed — OTel disabled")
            self._enabled = False
        except Exception as e:
            logger.warning("Failed to initialize OTel tracer: %s", e)
            self._enabled = False

    def start_span(
        self,
        flow: http.HTTPFlow,
        direction: str,
        host: str,
        method: str,
        session_id: str | None,
    ) -> None:
        """Start an OTel span for an HTTP request flow.

        The span is stored in the FlowRecord's OtelMeta and ended in
        finish_span() or finish_span_error().
        """
        if not self._enabled or self._tracer is None:
            return

        try:
            span_name = f"ccproxy.{direction}.{method} {host}"
            span = self._tracer.start_span(span_name)

            request = flow.request
            span.set_attribute("http.request.method", method)
            span.set_attribute("url.full", request.pretty_url)
            span.set_attribute("server.address", host)
            span.set_attribute("server.port", request.port)
            span.set_attribute("url.path", request.path)
            span.set_attribute("url.scheme", request.scheme)

            span.set_attribute("ccproxy.proxy_direction", direction)
            span.set_attribute("ccproxy.trace_id", flow.id)

            if session_id:
                span.set_attribute("ccproxy.session_id", session_id)

            path = request.path
            if "/messages" in path or "/completions" in path:
                span.set_attribute("gen_ai.system", self._provider_map.get(host, host))
                span.set_attribute("gen_ai.operation.name", "chat")

            record: FlowRecord | None = flow.metadata.get(InspectorMeta.RECORD)
            if record:
                record.otel = OtelMeta(span=span)
            else:
                flow.metadata["ccproxy.otel_span"] = span
                flow.metadata["ccproxy.otel_span_ended"] = False

        except Exception as e:
            logger.debug("Error starting OTel span: %s", e)

    def _get_span(self, flow: http.HTTPFlow) -> tuple[Any, bool]:
        """Retrieve span and ended flag from FlowRecord or legacy metadata."""
        record: FlowRecord | None = flow.metadata.get(InspectorMeta.RECORD)
        if record and record.otel:
            return record.otel.span, record.otel.ended
        return flow.metadata.get("ccproxy.otel_span"), flow.metadata.get("ccproxy.otel_span_ended", False)

    def _mark_ended(self, flow: http.HTTPFlow) -> None:
        record: FlowRecord | None = flow.metadata.get(InspectorMeta.RECORD)
        if record and record.otel:
            record.otel.ended = True
        else:
            flow.metadata["ccproxy.otel_span_ended"] = True

    def finish_span(
        self,
        flow: http.HTTPFlow,
        status_code: int,
        duration_ms: float | None,
    ) -> None:
        """End an OTel span with response data."""
        if not self._enabled:
            return

        span, ended = self._get_span(flow)
        if span is None or ended:
            return

        try:
            span.set_attribute("http.response.status_code", status_code)
            if duration_ms is not None:
                span.set_attribute("ccproxy.duration_ms", duration_ms)

            if status_code >= 400:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, f"HTTP {status_code}")

            span.end()
            self._mark_ended(flow)

        except Exception as e:
            logger.debug("Error finishing OTel span: %s", e)

    def finish_span_error(
        self,
        flow: http.HTTPFlow,
        error_message: str,
    ) -> None:
        """End an OTel span with an error."""
        if not self._enabled:
            return

        span, ended = self._get_span(flow)
        if span is None or ended:
            return

        try:
            from opentelemetry.trace import StatusCode

            span.set_status(StatusCode.ERROR, error_message)
            span.set_attribute("error.message", error_message)
            span.end()
            self._mark_ended(flow)

        except Exception as e:
            logger.debug("Error finishing OTel span with error: %s", e)


def _init_otel_tracer(service_name: str, otlp_endpoint: str) -> Any:
    """Initialize the real OTel tracer with OTLP gRPC exporter."""
    global _provider

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    exporter = OTLPSpanExporter(
        endpoint=otlp_endpoint,
        insecure=True,
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    _provider = provider
    return trace.get_tracer(service_name)


def shutdown_tracer() -> None:
    """Flush remaining spans and shut down the OTel tracer provider."""
    global _provider
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception as e:
            logger.warning("Error shutting down OTel provider: %s", e)
        _provider = None
