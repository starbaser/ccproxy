"""Tests for outbound route handlers (beta headers, Claude Code identity, auth failure observation)."""

import json
import logging
from unittest.mock import MagicMock

import pytest

from ccproxy.constants import ANTHROPIC_BETA_HEADERS, CLAUDE_CODE_SYSTEM_PREFIX
from ccproxy.inspector.flow_store import InspectorMeta
from ccproxy.inspector.router import InspectorRouter


def _make_flow(
    beta_header: str | None = None,
    status_code: int = 200,
    direction: str = "inbound",
    oauth_injected: bool = False,
    anthropic_version: str | None = "2023-06-01",
    body: dict | None = None,
) -> MagicMock:
    flow = MagicMock()
    headers: dict[str, str] = {}
    if beta_header is not None:
        headers["anthropic-beta"] = beta_header
    if oauth_injected:
        headers["x-ccproxy-oauth-injected"] = "1"
    if anthropic_version is not None:
        headers["anthropic-version"] = anthropic_version
    flow.request.headers = headers
    flow.request.path = "/v1/messages"
    flow.request.method = "POST"
    flow.request.pretty_host = "api.anthropic.com"
    flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
    flow.request.content = json.dumps(body).encode() if body is not None else b""
    flow.response = MagicMock()
    flow.response.status_code = status_code
    flow.metadata = {InspectorMeta.DIRECTION: direction}
    flow.id = "test-flow-1"
    return flow


def _setup_router() -> InspectorRouter:
    router = InspectorRouter(name="test_outbound", request_passthrough=True, response_passthrough=True)
    from ccproxy.inspector.routes.outbound import register_outbound_routes

    register_outbound_routes(router)
    return router


class TestBetaHeaders:
    def test_merges_when_header_present(self) -> None:
        router = _setup_router()
        flow = _make_flow(beta_header="existing-feature")
        router.request(flow)

        merged = flow.request.headers["anthropic-beta"]
        for h in ANTHROPIC_BETA_HEADERS:
            assert h in merged
        assert "existing-feature" in merged

    def test_noop_when_header_absent(self) -> None:
        router = _setup_router()
        flow = _make_flow(beta_header=None)
        router.request(flow)
        assert "anthropic-beta" not in flow.request.headers

    def test_deduplicates_existing_headers(self) -> None:
        router = _setup_router()
        flow = _make_flow(beta_header=ANTHROPIC_BETA_HEADERS[0])
        router.request(flow)

        merged = flow.request.headers["anthropic-beta"]
        parts = [h.strip() for h in merged.split(",")]
        assert parts.count(ANTHROPIC_BETA_HEADERS[0]) == 1

    def test_noop_on_non_inbound_flow(self) -> None:
        router = _setup_router()
        flow = _make_flow(beta_header="test", direction="outbound")
        router.request(flow)
        assert flow.request.headers.get("anthropic-beta") == "test"


class TestClaudeCodeIdentity:
    def test_injects_prefix_when_oauth_and_anthropic(self) -> None:
        router = _setup_router()
        flow = _make_flow(oauth_injected=True, body={"system": "Be helpful."})
        router.request(flow)

        body = json.loads(flow.request.content)
        assert body["system"].startswith(CLAUDE_CODE_SYSTEM_PREFIX)
        assert "Be helpful." in body["system"]

    def test_injects_prefix_with_empty_system(self) -> None:
        router = _setup_router()
        flow = _make_flow(oauth_injected=True, body={"system": ""})
        router.request(flow)

        body = json.loads(flow.request.content)
        assert body["system"] == CLAUDE_CODE_SYSTEM_PREFIX

    def test_injects_prefix_when_system_absent(self) -> None:
        router = _setup_router()
        flow = _make_flow(oauth_injected=True, body={"messages": []})
        router.request(flow)

        body = json.loads(flow.request.content)
        assert body["system"] == CLAUDE_CODE_SYSTEM_PREFIX

    def test_skips_when_prefix_already_present(self) -> None:
        router = _setup_router()
        existing = CLAUDE_CODE_SYSTEM_PREFIX + "\n\nOriginal."
        flow = _make_flow(oauth_injected=True, body={"system": existing})
        router.request(flow)

        body = json.loads(flow.request.content)
        assert body["system"] == existing

    def test_skips_when_no_oauth_injected(self) -> None:
        router = _setup_router()
        flow = _make_flow(oauth_injected=False, body={"system": "Be helpful."})
        router.request(flow)

        body = json.loads(flow.request.content)
        assert body["system"] == "Be helpful."

    def test_skips_when_not_anthropic_request(self) -> None:
        router = _setup_router()
        flow = _make_flow(oauth_injected=True, anthropic_version=None, body={"system": "Be helpful."})
        router.request(flow)

        body = json.loads(flow.request.content)
        assert body["system"] == "Be helpful."

    def test_skips_on_non_inbound_flow(self) -> None:
        router = _setup_router()
        flow = _make_flow(oauth_injected=True, direction="outbound", body={"system": "Be helpful."})
        router.request(flow)

        body = json.loads(flow.request.content)
        assert body["system"] == "Be helpful."

    def test_noop_on_empty_body(self) -> None:
        router = _setup_router()
        flow = _make_flow(oauth_injected=True)
        flow.request.content = b""
        router.request(flow)  # Should not raise

    def test_noop_on_invalid_json(self) -> None:
        router = _setup_router()
        flow = _make_flow(oauth_injected=True)
        flow.request.content = b"not-json"
        router.request(flow)  # Should not raise


class TestAuthFailureObservation:
    def test_logs_401(self, caplog: pytest.LogCaptureFixture) -> None:
        router = _setup_router()
        flow = _make_flow(status_code=401)
        with caplog.at_level(logging.WARNING):
            router.response(flow)
        assert "401" in caplog.text

    def test_logs_403(self, caplog: pytest.LogCaptureFixture) -> None:
        router = _setup_router()
        flow = _make_flow(status_code=403)
        with caplog.at_level(logging.WARNING):
            router.response(flow)
        assert "403" in caplog.text

    def test_ignores_200(self, caplog: pytest.LogCaptureFixture) -> None:
        router = _setup_router()
        flow = _make_flow(status_code=200)
        with caplog.at_level(logging.WARNING):
            router.response(flow)
        assert "Auth failure" not in caplog.text

    def test_ignores_500(self, caplog: pytest.LogCaptureFixture) -> None:
        router = _setup_router()
        flow = _make_flow(status_code=500)
        with caplog.at_level(logging.WARNING):
            router.response(flow)
        assert "Auth failure" not in caplog.text
