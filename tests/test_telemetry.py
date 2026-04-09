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
