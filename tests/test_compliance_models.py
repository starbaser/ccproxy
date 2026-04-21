"""Tests for ccproxy.compliance.models.apply_husk."""

from __future__ import annotations

from mitmproxy import http
from mitmproxy.test import tflow

from ccproxy.compliance.models import apply_husk
from ccproxy.pipeline.context import Context


def _husk(
    method: str = "POST",
    url: str = "https://seed.example/v1/endpoint",
    headers: dict[str, str] | None = None,
    content: bytes = b'{"seed": true}',
) -> http.Request:
    return http.Request.make(
        method,
        url,
        content,
        headers or {"x-seed": "a", "content-type": "application/json"},
    )


def _target_flow() -> http.HTTPFlow:
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "GET",
        "http://orig.example:8080/old",
        b"",
        {"x-old": "1", "content-type": "text/plain"},
    )
    return flow


class TestApplyHusk:
    def test_preserves_transport_routing(self) -> None:
        flow = _target_flow()
        ctx = Context.from_flow(flow)
        apply_husk(_husk(url="https://seed.example:4443/v1/endpoint?q=1"), ctx)
        assert flow.request.scheme == "http"
        assert flow.request.host == "orig.example"
        assert flow.request.port == 8080
        assert flow.request.path == "/old"

    def test_replaces_headers(self) -> None:
        flow = _target_flow()
        ctx = Context.from_flow(flow)
        apply_husk(_husk(headers={"x-seed": "a", "x-trace": "b"}), ctx)
        assert "x-old" not in flow.request.headers
        assert flow.request.headers["x-seed"] == "a"
        assert flow.request.headers["x-trace"] == "b"

    def test_replaces_content(self) -> None:
        flow = _target_flow()
        ctx = Context.from_flow(flow)
        apply_husk(_husk(content=b'{"new": 2}'), ctx)
        assert flow.request.content == b'{"new": 2}'

    def test_idempotent_applied_twice(self) -> None:
        flow = _target_flow()
        ctx = Context.from_flow(flow)
        husk = _husk()
        apply_husk(husk, ctx)
        apply_husk(husk, ctx)
        assert flow.request.host == "orig.example"
        assert flow.request.content == b'{"seed": true}'

    def test_syncs_ctx_body_from_husk_content(self) -> None:
        flow = _target_flow()
        ctx = Context.from_flow(flow)
        apply_husk(_husk(content=b'{"model": "seed-model"}'), ctx)
        assert ctx._body == {"model": "seed-model"}

    def test_non_json_husk_content_leaves_empty_body(self) -> None:
        flow = _target_flow()
        ctx = Context.from_flow(flow)
        apply_husk(_husk(content=b"not json {"), ctx)
        assert ctx._body == {}
        assert flow.request.content == b"not json {"

    def test_non_dict_json_husk_content_leaves_empty_body(self) -> None:
        flow = _target_flow()
        ctx = Context.from_flow(flow)
        apply_husk(_husk(content=b"[1, 2, 3]"), ctx)
        assert ctx._body == {}

    def test_preserves_auth_headers(self) -> None:
        flow = _target_flow()
        flow.request.headers["authorization"] = "Bearer tok-123"
        flow.request.headers["x-api-key"] = "sk-abc"
        ctx = Context.from_flow(flow)
        apply_husk(_husk(headers={"x-seed": "a"}), ctx)
        assert flow.request.headers["authorization"] == "Bearer tok-123"
        assert flow.request.headers["x-api-key"] == "sk-abc"
        assert flow.request.headers["x-seed"] == "a"
        assert "x-old" not in flow.request.headers
