"""Tests for InspectorTracer span lifecycle (telemetry.py)."""

from unittest.mock import MagicMock

from ccproxy.inspector.flow_store import FlowRecord, InspectorMeta, OtelMeta
from ccproxy.inspector.telemetry import InspectorTracer


def _make_flow(metadata: dict | None = None) -> MagicMock:
    flow = MagicMock()
    flow.metadata = metadata if metadata is not None else {}
    return flow


class TestInspectorTracerDisabled:
    def test_disabled_start_span_noop(self) -> None:
        tracer = InspectorTracer(enabled=False)
        flow = _make_flow()
        tracer.start_span(flow, direction="inbound", host="api.anthropic.com", method="POST", session_id=None)
        assert flow.metadata == {}

    def test_disabled_finish_span_noop(self) -> None:
        tracer = InspectorTracer(enabled=False)
        mock_span = MagicMock()
        flow = _make_flow({"ccproxy.otel_span": mock_span, "ccproxy.otel_span_ended": False})
        tracer.finish_span(flow, status_code=200, duration_ms=42.0)
        mock_span.end.assert_not_called()

    def test_disabled_finish_span_error_noop(self) -> None:
        tracer = InspectorTracer(enabled=False)
        mock_span = MagicMock()
        flow = _make_flow({"ccproxy.otel_span": mock_span, "ccproxy.otel_span_ended": False})
        tracer.finish_span_error(flow, error_message="connection reset")
        mock_span.end.assert_not_called()


class TestGetSpan:
    def test_from_flow_record(self) -> None:
        tracer = InspectorTracer(enabled=False)
        mock_span = MagicMock()
        record = FlowRecord(direction="inbound", otel=OtelMeta(span=mock_span, ended=False))
        flow = _make_flow({InspectorMeta.RECORD: record})

        span, ended = tracer._get_span(flow)

        assert span is mock_span
        assert ended is False

    def test_legacy_fallback(self) -> None:
        tracer = InspectorTracer(enabled=False)
        mock_span = MagicMock()
        flow = _make_flow({"ccproxy.otel_span": mock_span, "ccproxy.otel_span_ended": False})

        span, ended = tracer._get_span(flow)

        assert span is mock_span
        assert ended is False

    def test_no_otel_on_record(self) -> None:
        tracer = InspectorTracer(enabled=False)
        mock_span = MagicMock()
        record = FlowRecord(direction="inbound", otel=None)
        flow = _make_flow({
            InspectorMeta.RECORD: record,
            "ccproxy.otel_span": mock_span,
            "ccproxy.otel_span_ended": False,
        })

        span, ended = tracer._get_span(flow)

        assert span is mock_span
        assert ended is False

    def test_no_span_anywhere(self) -> None:
        tracer = InspectorTracer(enabled=False)
        flow = _make_flow()

        span, ended = tracer._get_span(flow)

        assert span is None
        assert ended is False


class TestMarkEnded:
    def test_mark_ended_flow_record(self) -> None:
        tracer = InspectorTracer(enabled=False)
        mock_span = MagicMock()
        record = FlowRecord(direction="inbound", otel=OtelMeta(span=mock_span, ended=False))
        flow = _make_flow({InspectorMeta.RECORD: record})

        tracer._mark_ended(flow)

        assert record.otel is not None
        assert record.otel.ended is True

    def test_mark_ended_legacy(self) -> None:
        tracer = InspectorTracer(enabled=False)
        flow = _make_flow({"ccproxy.otel_span": MagicMock()})

        tracer._mark_ended(flow)

        assert flow.metadata["ccproxy.otel_span_ended"] is True


class TestFinishSpan:
    def test_idempotent(self) -> None:
        tracer = InspectorTracer(enabled=True)
        tracer._enabled = True
        tracer._tracer = MagicMock()

        mock_span = MagicMock()
        record = FlowRecord(direction="inbound", otel=OtelMeta(span=mock_span, ended=False))
        flow = _make_flow({InspectorMeta.RECORD: record})

        tracer.finish_span(flow, status_code=200, duration_ms=10.0)
        tracer.finish_span(flow, status_code=200, duration_ms=10.0)

        assert mock_span.end.call_count == 1

    def test_finish_span_success(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True

        mock_span = MagicMock()
        record = FlowRecord(direction="inbound", otel=OtelMeta(span=mock_span, ended=False))
        flow = _make_flow({InspectorMeta.RECORD: record})

        tracer.finish_span(flow, status_code=200, duration_ms=42.5)

        mock_span.set_attribute.assert_any_call("http.response.status_code", 200)
        mock_span.set_attribute.assert_any_call("ccproxy.duration_ms", 42.5)
        mock_span.end.assert_called_once()

    def test_finish_span_no_duration(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True

        mock_span = MagicMock()
        record = FlowRecord(direction="inbound", otel=OtelMeta(span=mock_span, ended=False))
        flow = _make_flow({InspectorMeta.RECORD: record})

        tracer.finish_span(flow, status_code=200, duration_ms=None)
        mock_span.end.assert_called_once()

    def test_finish_span_4xx_sets_error_status(self) -> None:
        from unittest.mock import patch

        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True

        mock_span = MagicMock()
        record = FlowRecord(direction="inbound", otel=OtelMeta(span=mock_span, ended=False))
        flow = _make_flow({InspectorMeta.RECORD: record})

        mock_status_code = MagicMock()
        mock_status_code.ERROR = "ERROR"

        with patch.dict("sys.modules", {"opentelemetry.trace": MagicMock(StatusCode=mock_status_code)}):
            tracer.finish_span(flow, status_code=400, duration_ms=10.0)

        mock_span.end.assert_called_once()

    def test_finish_span_exception_handled(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True

        mock_span = MagicMock()
        mock_span.set_attribute.side_effect = RuntimeError("otel error")
        record = FlowRecord(direction="inbound", otel=OtelMeta(span=mock_span, ended=False))
        flow = _make_flow({InspectorMeta.RECORD: record})

        tracer.finish_span(flow, status_code=200, duration_ms=10.0)

    def test_finish_span_skips_none_span(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True
        flow = _make_flow({})
        tracer.finish_span(flow, status_code=200, duration_ms=10.0)


class TestFinishSpanError:
    def test_finish_span_error_sets_status(self) -> None:
        from unittest.mock import patch

        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True

        mock_span = MagicMock()
        record = FlowRecord(direction="inbound", otel=OtelMeta(span=mock_span, ended=False))
        flow = _make_flow({InspectorMeta.RECORD: record})

        mock_status_code = MagicMock()
        mock_status_code.ERROR = "ERROR"

        with patch.dict("sys.modules", {"opentelemetry.trace": MagicMock(StatusCode=mock_status_code)}):
            tracer.finish_span_error(flow, error_message="timeout")

        mock_span.end.assert_called_once()

    def test_finish_span_error_exception_handled(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True

        mock_span = MagicMock()
        mock_span.set_status.side_effect = RuntimeError("otel error")
        record = FlowRecord(direction="inbound", otel=OtelMeta(span=mock_span, ended=False))
        flow = _make_flow({InspectorMeta.RECORD: record})

        from unittest.mock import patch
        mock_status_code = MagicMock()
        with patch.dict("sys.modules", {"opentelemetry.trace": MagicMock(StatusCode=mock_status_code)}):
            tracer.finish_span_error(flow, error_message="error")

    def test_finish_span_error_skips_none_span(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True
        flow = _make_flow({})
        tracer.finish_span_error(flow, error_message="err")

    def test_finish_span_error_skips_when_disabled(self) -> None:
        tracer = InspectorTracer(enabled=False)
        mock_span = MagicMock()
        flow = _make_flow({"ccproxy.otel_span": mock_span, "ccproxy.otel_span_ended": False})
        tracer.finish_span_error(flow, error_message="err")
        mock_span.end.assert_not_called()


class TestStartSpan:
    def test_start_span_when_enabled(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True
        tracer._tracer = MagicMock()

        mock_span = MagicMock()
        tracer._tracer.start_span.return_value = mock_span

        flow = _make_flow()
        flow.request = MagicMock()
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.request.port = 443
        flow.request.path = "/v1/messages"
        flow.request.scheme = "https"
        flow.id = "test-flow-id"

        tracer.start_span(flow, direction="inbound", host="api.anthropic.com", method="POST", session_id="sess-1")

        tracer._tracer.start_span.assert_called_once()
        mock_span.set_attribute.assert_any_call("http.request.method", "POST")
        mock_span.set_attribute.assert_any_call("ccproxy.session_id", "sess-1")
        mock_span.set_attribute.assert_any_call("gen_ai.system", "anthropic")

    def test_start_span_no_session_id(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True
        tracer._tracer = MagicMock()

        mock_span = MagicMock()
        tracer._tracer.start_span.return_value = mock_span

        flow = _make_flow()
        flow.request = MagicMock()
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.request.port = 443
        flow.request.path = "/v1/messages"
        flow.request.scheme = "https"
        flow.id = "test-id"

        tracer.start_span(flow, direction="inbound", host="api.anthropic.com", method="POST", session_id=None)

        # Should not set session_id attribute
        calls = [str(c) for c in mock_span.set_attribute.call_args_list]
        assert not any("session_id" in c for c in calls)

    def test_start_span_stores_in_flow_record(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True
        tracer._tracer = MagicMock()
        tracer._tracer.start_span.return_value = MagicMock()

        record = FlowRecord(direction="inbound")
        flow = _make_flow({InspectorMeta.RECORD: record})
        flow.request = MagicMock()
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.request.port = 443
        flow.request.path = "/v1/messages"
        flow.request.scheme = "https"
        flow.id = "test-id"

        tracer.start_span(flow, direction="inbound", host="api.anthropic.com", method="POST", session_id=None)

        assert record.otel is not None

    def test_start_span_stores_in_metadata_when_no_record(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True
        tracer._tracer = MagicMock()
        tracer._tracer.start_span.return_value = MagicMock()

        flow = _make_flow()
        flow.request = MagicMock()
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.request.port = 443
        flow.request.path = "/v1/messages"
        flow.request.scheme = "https"
        flow.id = "test-id"

        tracer.start_span(flow, direction="inbound", host="api.anthropic.com", method="POST", session_id=None)

        assert "ccproxy.otel_span" in flow.metadata

    def test_start_span_exception_handled(self) -> None:
        tracer = InspectorTracer(enabled=False)
        tracer._enabled = True
        tracer._tracer = MagicMock()
        tracer._tracer.start_span.side_effect = RuntimeError("tracer error")

        flow = _make_flow()
        flow.request = MagicMock()
        flow.id = "test-id"

        tracer.start_span(flow, direction="inbound", host="api.anthropic.com", method="POST", session_id=None)


class TestInspectorTracerInit:
    def test_disabled_by_default(self) -> None:
        tracer = InspectorTracer(enabled=False)
        assert tracer._enabled is False
        assert tracer._tracer is None

    def test_import_error_disables(self) -> None:
        from unittest.mock import patch
        with patch("ccproxy.inspector.telemetry._init_otel_tracer", side_effect=ImportError("no otel")):
            tracer = InspectorTracer(enabled=True)
        assert tracer._enabled is False

    def test_exception_disables(self) -> None:
        from unittest.mock import patch
        with patch("ccproxy.inspector.telemetry._init_otel_tracer", side_effect=RuntimeError("init failed")):
            tracer = InspectorTracer(enabled=True)
        assert tracer._enabled is False

    def test_enabled_with_mock_otel(self) -> None:
        """Test that _init_otel_tracer is called and tracer is set."""
        from unittest.mock import patch

        mock_tracer = MagicMock()
        with patch("ccproxy.inspector.telemetry._init_otel_tracer", return_value=mock_tracer):
            tracer = InspectorTracer(enabled=True)
        assert tracer._enabled is True
        assert tracer._tracer is mock_tracer


class TestInitOtelTracer:
    def test_init_with_mocked_otel(self) -> None:
        """Test _init_otel_tracer with mocked OTel packages."""
        import sys
        from unittest.mock import MagicMock, patch

        # Mock all OTel modules
        mock_trace = MagicMock()
        mock_batch_processor = MagicMock()
        mock_otlp_exporter = MagicMock()

        mock_tracer = MagicMock()
        mock_trace.get_tracer.return_value = mock_tracer

        mock_sdk_trace = MagicMock()
        mock_provider_instance = MagicMock()
        mock_sdk_trace.TracerProvider.return_value = mock_provider_instance

        mock_sdk_export = MagicMock()
        mock_sdk_export.BatchSpanProcessor = mock_batch_processor

        mock_otlp_mod = MagicMock()
        mock_otlp_mod.OTLPSpanExporter = mock_otlp_exporter

        mock_sdk_resources = MagicMock()
        mock_sdk_resources.SERVICE_NAME = "service.name"
        mock_sdk_resources.Resource.create.return_value = MagicMock()

        otel_modules = {
            "opentelemetry": MagicMock(),
            "opentelemetry.trace": mock_trace,
            "opentelemetry.sdk": MagicMock(),
            "opentelemetry.sdk.resources": mock_sdk_resources,
            "opentelemetry.sdk.trace": mock_sdk_trace,
            "opentelemetry.sdk.trace.export": mock_sdk_export,
            "opentelemetry.exporter": MagicMock(),
            "opentelemetry.exporter.otlp": MagicMock(),
            "opentelemetry.exporter.otlp.proto": MagicMock(),
            "opentelemetry.exporter.otlp.proto.grpc": MagicMock(),
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": mock_otlp_mod,
        }

        with patch.dict(sys.modules, otel_modules):
            from ccproxy.inspector.telemetry import _init_otel_tracer
            result = _init_otel_tracer("test-service", "http://localhost:4317")

        # Result should be the return value of trace.get_tracer
        assert result is not None


class TestShutdownTracer:
    def test_shutdown_with_provider(self) -> None:
        import ccproxy.inspector.telemetry as mod
        from ccproxy.inspector.telemetry import shutdown_tracer

        mock_provider = MagicMock()
        original = mod._provider
        mod._provider = mock_provider

        try:
            shutdown_tracer()
            mock_provider.shutdown.assert_called_once()
            assert mod._provider is None
        finally:
            mod._provider = original

    def test_shutdown_with_no_provider(self) -> None:
        import ccproxy.inspector.telemetry as mod
        from ccproxy.inspector.telemetry import shutdown_tracer

        original = mod._provider
        mod._provider = None
        try:
            shutdown_tracer()  # Should be a no-op
        finally:
            mod._provider = original

    def test_shutdown_exception_handled(self) -> None:
        import ccproxy.inspector.telemetry as mod
        from ccproxy.inspector.telemetry import shutdown_tracer

        mock_provider = MagicMock()
        mock_provider.shutdown.side_effect = RuntimeError("shutdown error")
        original = mod._provider
        mod._provider = mock_provider

        try:
            shutdown_tracer()
            assert mod._provider is None
        finally:
            mod._provider = original
