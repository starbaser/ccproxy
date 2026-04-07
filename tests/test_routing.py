"""Tests for vendored xepor routing framework."""

from unittest.mock import MagicMock

import pytest

from ccproxy.inspector.routing import FlowMeta, InspectorRouter, InterceptedAPI, RouteType


def _make_flow(host: str = "example.com", path: str = "/api/test", method: str = "GET") -> MagicMock:
    flow = MagicMock()
    flow.request.method = method
    flow.request.path = path
    flow.request.pretty_host = host
    flow.request.host = host
    flow.request.port = 443
    flow.request.scheme = "https"
    flow.request.pretty_url = f"https://{host}{path}"
    flow.request.headers = {}
    flow.response = MagicMock()
    flow.response.status_code = 200
    flow.metadata = {}
    flow.client_conn = MagicMock()
    flow.server_conn = MagicMock()
    return flow


class TestInspectorRouter:
    def test_sets_custom_name(self) -> None:
        router = InspectorRouter(name="test_router")
        assert router.name == "test_router"

    def test_distinct_names_for_multiple_instances(self) -> None:
        r1 = InspectorRouter(name="inbound")
        r2 = InspectorRouter(name="outbound")
        assert r1.name != r2.name


class TestRouteRegistration:
    def test_request_route_registered(self) -> None:
        api = InterceptedAPI(default_host="example.com")

        @api.route("/test", rtype=RouteType.REQUEST)
        def handler(flow: MagicMock) -> None:
            pass

        assert len(api.request_routes) == 1
        assert len(api.response_routes) == 0

    def test_response_route_registered(self) -> None:
        api = InterceptedAPI(default_host="example.com")

        @api.route("/test", rtype=RouteType.RESPONSE)
        def handler(flow: MagicMock) -> None:
            pass

        assert len(api.response_routes) == 1
        assert len(api.request_routes) == 0


class TestRouteDispatch:
    def test_handler_called_on_matching_path(self) -> None:
        api = InterceptedAPI(default_host="example.com")
        called = []

        @api.route("/api/test")
        def handler(flow: MagicMock) -> None:
            called.append(True)

        flow = _make_flow()
        api.request(flow)
        assert called

    def test_handler_receives_path_parameters(self) -> None:
        api = InterceptedAPI(default_host="example.com")
        captured: dict[str, str] = {}

        @api.route("/users/{user_id}/posts/{post_id}")
        def handler(flow: MagicMock, user_id: str = "", post_id: str = "") -> None:
            captured["user_id"] = user_id
            captured["post_id"] = post_id

        flow = _make_flow(path="/users/42/posts/99")
        api.request(flow)
        assert captured["user_id"] == "42"
        assert captured["post_id"] == "99"

    def test_unmatched_route_passthrough(self) -> None:
        api = InterceptedAPI(default_host="example.com", request_passthrough=True)

        @api.route("/specific")
        def handler(flow: MagicMock) -> None:
            pass

        flow = _make_flow(path="/other")
        api.request(flow)
        assert flow.metadata.get(FlowMeta.REQ_PASSTHROUGH) is True
        assert flow.response != api.default_response()

    def test_unmatched_route_whitelist_mode(self) -> None:
        api = InterceptedAPI(default_host="example.com", request_passthrough=False)

        @api.route("/allowed")
        def handler(flow: MagicMock) -> None:
            pass

        flow = _make_flow(path="/blocked")
        api.request(flow)
        assert flow.response.status_code == 404

    def test_blacklisted_domain_gets_default_response(self) -> None:
        api = InterceptedAPI(
            default_host="example.com",
            blacklist_domain=["evil.com"],
            request_passthrough=True,
        )
        flow = _make_flow(host="evil.com")
        api.request(flow)
        assert flow.response.status_code == 404

    def test_first_matching_route_wins(self) -> None:
        api = InterceptedAPI(default_host="example.com")
        order: list[int] = []

        @api.route("/{path}")
        def first(flow: MagicMock, **kwargs: object) -> None:
            order.append(1)

        @api.route("/{path}")
        def second(flow: MagicMock, **kwargs: object) -> None:
            order.append(2)

        flow = _make_flow()
        api.request(flow)
        assert order == [1]

    def test_host_specific_route_only_fires_for_matching_host(self) -> None:
        api = InterceptedAPI()
        called = []

        @api.route("/test", host="other.com")
        def handler(flow: MagicMock) -> None:
            called.append(True)

        flow = _make_flow(host="example.com", path="/test")
        api.request(flow)
        assert not called

    def test_response_handler_dispatched(self) -> None:
        api = InterceptedAPI(default_host="example.com")
        called = []

        @api.route("/test", rtype=RouteType.RESPONSE)
        def handler(flow: MagicMock) -> None:
            called.append(True)

        flow = _make_flow(path="/test")
        api.response(flow)
        assert called


class TestFindHandler:
    def test_returns_none_for_no_match(self) -> None:
        api = InterceptedAPI(default_host="example.com")
        handler, params = api.find_handler("example.com", "/nothing")
        assert handler is None
        assert params is None

    def test_returns_handler_and_params(self) -> None:
        api = InterceptedAPI(default_host="example.com")

        @api.route("/items/{id}")
        def handler(flow: MagicMock, id: str = "") -> None:
            pass

        h, params = api.find_handler("example.com", "/items/42")
        assert h is not None
        assert params is not None
        assert params.named["id"] == "42"


class TestErrorHandling:
    def test_catch_error_prevents_crash(self) -> None:
        api = InterceptedAPI(default_host="example.com")

        @api.route("/crash", catch_error=True)
        def handler(flow: MagicMock) -> None:
            raise ValueError("boom")

        flow = _make_flow(path="/crash")
        api.request(flow)  # Should not raise

    def test_return_error_sends_502(self) -> None:
        api = InterceptedAPI(default_host="example.com")

        @api.route("/crash", catch_error=True, return_error=True)
        def handler(flow: MagicMock) -> None:
            raise ValueError("error message")

        flow = _make_flow(path="/crash")
        api.request(flow)
        assert flow.response.status_code == 502


class TestPassthroughMetadata:
    def test_passthrough_skips_subsequent_dispatch(self) -> None:
        api = InterceptedAPI(default_host="example.com")
        called = []

        @api.route("/{path}")
        def handler(flow: MagicMock, **kwargs: object) -> None:
            called.append(True)

        flow = _make_flow()
        flow.metadata[FlowMeta.REQ_PASSTHROUGH] = True
        api.request(flow)
        assert not called
