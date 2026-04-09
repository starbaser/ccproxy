"""Tests for outbound route handlers (beta headers, auth failure observation)."""

import logging
from unittest.mock import MagicMock

import pytest

from ccproxy.constants import ANTHROPIC_BETA_HEADERS
from ccproxy.inspector.router import InspectorRouter


def _make_outbound_flow(
    beta_header: str | None = None,
    status_code: int = 200,
) -> MagicMock:
    flow = MagicMock()
    headers: dict[str, str] = {}
    if beta_header is not None:
        headers["anthropic-beta"] = beta_header
    flow.request.headers = headers
    flow.request.path = "/v1/messages"
    flow.request.method = "POST"
    flow.request.pretty_host = "api.anthropic.com"
    flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
    flow.response = MagicMock()
    flow.response.status_code = status_code
    flow.metadata = {"ccproxy.direction": "outbound"}
    flow.id = "test-outbound-1"
    return flow


def _setup_router() -> InspectorRouter:
    router = InspectorRouter(name="test_outbound", request_passthrough=True, response_passthrough=True)
    from ccproxy.inspector.routes.outbound import register_outbound_routes

    register_outbound_routes(router)
    return router


class TestBetaHeaders:
    def test_merges_when_header_present(self) -> None:
        router = _setup_router()
        flow = _make_outbound_flow(beta_header="existing-feature")
        router.request(flow)

        merged = flow.request.headers["anthropic-beta"]
        for h in ANTHROPIC_BETA_HEADERS:
            assert h in merged
        assert "existing-feature" in merged

    def test_noop_when_header_absent(self) -> None:
        router = _setup_router()
        flow = _make_outbound_flow(beta_header=None)
        router.request(flow)
        assert "anthropic-beta" not in flow.request.headers

    def test_deduplicates_existing_headers(self) -> None:
        router = _setup_router()
        flow = _make_outbound_flow(beta_header=ANTHROPIC_BETA_HEADERS[0])
        router.request(flow)

        merged = flow.request.headers["anthropic-beta"]
        parts = [h.strip() for h in merged.split(",")]
        assert parts.count(ANTHROPIC_BETA_HEADERS[0]) == 1

    def test_skips_non_outbound_flow(self) -> None:
        router = _setup_router()
        flow = _make_outbound_flow(beta_header="test")
        flow.metadata = {"ccproxy.direction": "inbound"}
        original = flow.request.headers.get("anthropic-beta")
        router.request(flow)
        assert flow.request.headers.get("anthropic-beta") == original


class TestAuthFailureObservation:
    def test_logs_401(self, caplog: pytest.LogCaptureFixture) -> None:
        router = _setup_router()
        flow = _make_outbound_flow(status_code=401)
        with caplog.at_level(logging.WARNING):
            router.response(flow)
        assert "401" in caplog.text

    def test_logs_403(self, caplog: pytest.LogCaptureFixture) -> None:
        router = _setup_router()
        flow = _make_outbound_flow(status_code=403)
        with caplog.at_level(logging.WARNING):
            router.response(flow)
        assert "403" in caplog.text

    def test_ignores_200(self) -> None:
        router = _setup_router()
        flow = _make_outbound_flow(status_code=200)
        router.response(flow)  # Should not log or raise

    def test_ignores_500(self) -> None:
        router = _setup_router()
        flow = _make_outbound_flow(status_code=500)
        router.response(flow)

    def test_skips_non_outbound_flow(self) -> None:
        router = _setup_router()
        flow = _make_outbound_flow(status_code=401)
        flow.metadata = {"ccproxy.direction": "inbound"}
        router.response(flow)  # Should not log


class TestIsOutbound:
    def test_outbound_when_metadata_set(self) -> None:
        from ccproxy.inspector.routes.outbound import _is_outbound

        flow = MagicMock()
        flow.metadata = {"ccproxy.direction": "outbound"}
        assert _is_outbound(flow) is True

    def test_not_outbound_when_inbound(self) -> None:
        from ccproxy.inspector.routes.outbound import _is_outbound

        flow = MagicMock()
        flow.metadata = {"ccproxy.direction": "inbound"}
        assert _is_outbound(flow) is False

    def test_not_outbound_when_no_metadata(self) -> None:
        from ccproxy.inspector.routes.outbound import _is_outbound

        flow = MagicMock()
        flow.metadata = {}
        assert _is_outbound(flow) is False
