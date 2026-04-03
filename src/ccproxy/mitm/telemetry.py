"""OpenTelemetry span emission for MITM traffic capture.

Provides a MitmTracer that emits OTel spans for each HTTP flow, with
graceful degradation when OTel packages are not installed.

Three operational modes:
1. OTel enabled + packages present → real tracer with OTLP export
2. OTel disabled + API package present → no-op tracer (zero overhead)
3. No OTel packages at all → stub (zero overhead, no imports)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mitmproxy import http

    from ccproxy.mitm.addon import ProxyDirection

logger = logging.getLogger(__name__)

# Module-level provider reference for shutdown
_provider: Any = None

# OTel span metadata keys in flow.metadata
_SPAN_KEY = "ccproxy.otel_span"
_SPAN_ENDED_KEY = "ccproxy.otel_span_ended"

# Provider hostname → gen_ai.system mapping
_PROVIDER_MAP = {
    "api.anthropic.com": "anthropic",
    "api.openai.com": "openai",
    "generativelanguage.googleapis.com": "google",
    "openrouter.ai": "openrouter",
}


def _infer_provider(host: str) -> str:
    """Map request hostname to LLM provider name."""
    return _PROVIDER_MAP.get(host, host)


class MitmTracer:
    """Wraps OTel span lifecycle for MITM addon flows.

    Handles tracer initialization, span creation per-flow, and attribute
    mapping. When disabled or when OTel packages are absent, all methods
    are no-ops.
    """

    def __init__(
        self,
        enabled: bool = False,
        otlp_endpoint: str = "http://localhost:4317",
        service_name: str = "ccproxy-mitm",
    ) -> None:
        self._tracer: Any = None
        self._enabled = enabled

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
        direction: ProxyDirection,
        host: str,
        method: str,
        session_id: str | None,
    ) -> None:
        """Start an OTel span for an HTTP request flow.

        The span is stored in flow.metadata and ended in finish_span() or
        finish_span_error().
        """
        if not self._enabled or self._tracer is None:
            return

        try:
            direction_name = direction.name.lower()
            span_name = f"ccproxy.{direction_name}.{method} {host}"

            span = self._tracer.start_span(span_name)

            # HTTP semantic conventions
            request = flow.request
            span.set_attribute("http.request.method", method)
            span.set_attribute("url.full", request.pretty_url)
            span.set_attribute("server.address", host)
            span.set_attribute("server.port", request.port)
            span.set_attribute("url.path", request.path)
            span.set_attribute("url.scheme", request.scheme)

            # ccproxy-specific
            span.set_attribute("ccproxy.proxy_direction", direction_name)
            span.set_attribute("ccproxy.trace_id", flow.id)

            if session_id:
                span.set_attribute("ccproxy.session_id", session_id)

            # LLM-specific attributes
            path = request.path
            if "/messages" in path or "/completions" in path:
                span.set_attribute("gen_ai.system", _infer_provider(host))
                span.set_attribute("gen_ai.operation.name", "chat")

            flow.metadata[_SPAN_KEY] = span
            flow.metadata[_SPAN_ENDED_KEY] = False

        except Exception as e:
            logger.debug("Error starting OTel span: %s", e)

    def finish_span(
        self,
        flow: http.HTTPFlow,
        status_code: int,
        duration_ms: float | None,
    ) -> None:
        """End an OTel span with response data."""
        if not self._enabled:
            return

        span = flow.metadata.get(_SPAN_KEY)
        if span is None or flow.metadata.get(_SPAN_ENDED_KEY):
            return

        try:
            span.set_attribute("http.response.status_code", status_code)
            if duration_ms is not None:
                span.set_attribute("ccproxy.duration_ms", duration_ms)

            # Mark error status for 4xx/5xx
            if status_code >= 400:
                from opentelemetry.trace import StatusCode

                span.set_status(StatusCode.ERROR, f"HTTP {status_code}")

            span.end()
            flow.metadata[_SPAN_ENDED_KEY] = True

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

        span = flow.metadata.get(_SPAN_KEY)
        if span is None or flow.metadata.get(_SPAN_ENDED_KEY):
            return

        try:
            from opentelemetry.trace import StatusCode

            span.set_status(StatusCode.ERROR, error_message)
            span.set_attribute("error.message", error_message)
            span.end()
            flow.metadata[_SPAN_ENDED_KEY] = True

        except Exception as e:
            logger.debug("Error finishing OTel span with error: %s", e)

def _init_otel_tracer(service_name: str, otlp_endpoint: str) -> Any:
    """Initialize the real OTel tracer with OTLP gRPC exporter.

    Raises:
        ImportError: If opentelemetry packages are not installed
    """
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
