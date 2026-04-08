"""Tests for inspector addon traffic capture."""

from unittest.mock import MagicMock

import pytest

from ccproxy.config import InspectorConfig
from ccproxy.inspector.addon import InspectorAddon


def _make_mock_flow(*, reverse: bool = True) -> MagicMock:
    """Create a mock HTTP flow with proxy_mode set for direction detection.

    Args:
        reverse: If True, simulate ReverseMode; if False, simulate RegularMode.
    """
    from mitmproxy.proxy.mode_specs import ProxyMode as MitmProxyMode

    flow = MagicMock()
    flow.request = MagicMock()
    flow.request.headers = {}
    flow.request.content = None
    flow.request.path = "/v1/messages"
    flow.metadata = {}

    # Set proxy_mode for per-flow direction detection
    if reverse:
        flow.client_conn.proxy_mode = MitmProxyMode.parse("reverse:http://localhost:4001@4002")
    else:
        flow.client_conn.proxy_mode = MitmProxyMode.parse("regular@4003")

    return flow


@pytest.fixture
def mock_flow() -> MagicMock:
    """Create a mock HTTP flow (reverse mode by default)."""
    return _make_mock_flow(reverse=True)


def _make_wg_flow(host: str = "api.anthropic.com", path: str = "/v1/messages") -> MagicMock:
    """Create a mock HTTP flow in WireGuard mode."""
    from mitmproxy.proxy.mode_specs import ProxyMode as MitmProxyMode

    flow = MagicMock()
    flow.request = MagicMock()
    flow.request.headers = {}
    flow.request.content = None
    flow.request.pretty_host = host
    flow.request.host = host
    flow.request.port = 443
    flow.request.scheme = "https"
    flow.request.method = "POST"
    flow.request.path = path
    flow.request.pretty_url = f"https://{host}{path}"
    flow.id = "wg-flow-1"
    flow.metadata = {}
    flow.client_conn.proxy_mode = MitmProxyMode.parse("wireguard@51820")
    return flow


class TestRequestMethod:
    """Tests for the request method."""

    @pytest.mark.asyncio
    async def test_request_runs_without_error(self, mock_flow: MagicMock) -> None:
        """request() should run without error."""
        config = InspectorConfig()
        addon = InspectorAddon(config=config)

        mock_flow.request.pretty_host = "api.anthropic.com"

        await addon.request(mock_flow)


class TestWireGuardForwarding:
    """Tests for WireGuard LLM API domain forwarding to LiteLLM."""

    @pytest.fixture(autouse=True)
    def _set_litellm_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CCPROXY_LITELLM_PORT", "4001")

    @pytest.mark.asyncio
    async def test_forwards_anthropic_to_litellm(self) -> None:
        """WireGuard flow to api.anthropic.com should be forwarded to LiteLLM."""
        config = InspectorConfig()
        addon = InspectorAddon(config=config)

        flow = _make_wg_flow(host="api.anthropic.com")
        await addon.request(flow)

        assert flow.request.host == "localhost"
        assert flow.request.port == 4001
        assert flow.request.scheme == "http"
        assert flow.request.headers["X-Forwarded-Host"] == "api.anthropic.com"

    @pytest.mark.asyncio
    async def test_forwards_openai_to_litellm(self) -> None:
        """WireGuard flow to api.openai.com should be forwarded to LiteLLM."""
        config = InspectorConfig()
        addon = InspectorAddon(config=config)

        flow = _make_wg_flow(host="api.openai.com")
        await addon.request(flow)

        assert flow.request.host == "localhost"
        assert flow.request.port == 4001
        assert flow.request.scheme == "http"

    @pytest.mark.asyncio
    async def test_non_llm_domain_passes_through(self) -> None:
        """WireGuard flow to non-LLM domains should not be forwarded."""
        config = InspectorConfig()
        addon = InspectorAddon(config=config)

        flow = _make_wg_flow(host="github.com", path="/api/v3/repos")
        await addon.request(flow)

        assert flow.request.host == "github.com"
        assert flow.request.port == 443
        assert flow.request.scheme == "https"

    @pytest.mark.asyncio
    async def test_reverse_flow_not_forwarded(self) -> None:
        """Reverse proxy flows should never be forwarded, even for LLM domains."""
        config = InspectorConfig()
        addon = InspectorAddon(config=config)

        flow = _make_mock_flow(reverse=True)
        flow.id = "rev-1"
        flow.request.pretty_host = "api.anthropic.com"
        flow.request.host = "api.anthropic.com"
        flow.request.method = "POST"
        flow.request.path = "/v1/messages"
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.request.content = None

        await addon.request(flow)
        # host should NOT have been rewritten
        assert flow.request.host == "api.anthropic.com"

    @pytest.mark.asyncio
    async def test_custom_forward_domains(self) -> None:
        """Custom forward_domains in config should be respected."""
        config = InspectorConfig(
            forward_domains=["custom-llm.example.com"],
        )
        addon = InspectorAddon(config=config)

        flow = _make_wg_flow(host="custom-llm.example.com")
        await addon.request(flow)
        assert flow.request.host == "localhost"
        assert flow.request.port == 4001

        # Default domain should NOT be forwarded when custom list replaces it
        flow2 = _make_wg_flow(host="api.anthropic.com")
        await addon.request(flow2)
        assert flow2.request.host == "api.anthropic.com"


class TestWireGuardDirectionDetection:
    """Tests for Phase 3 WIREGUARD_CLI vs WIREGUARD_GW detection."""

    @pytest.fixture(autouse=True)
    def _set_litellm_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CCPROXY_LITELLM_PORT", "4001")

    def _make_addon(self, wg_cli_port: int = 51820, wg_gateway_port: int = 51821) -> InspectorAddon:
        return InspectorAddon(
            config=InspectorConfig(),
            wg_cli_port=wg_cli_port,
            wg_gateway_port=wg_gateway_port,
        )

    @pytest.mark.asyncio
    async def test_wireguard_cli_direction(self) -> None:
        addon = self._make_addon(wg_cli_port=51820, wg_gateway_port=51821)
        flow = _make_wg_flow(host="api.anthropic.com")
        # Port 51820 != gateway port 51821 → WIREGUARD_CLI
        await addon.request(flow)
        assert flow.metadata.get("ccproxy.direction") == "inbound"
        # Should also forward to LiteLLM
        assert flow.request.host == "localhost"

    @pytest.mark.asyncio
    async def test_wireguard_gw_direction(self) -> None:
        from mitmproxy.proxy.mode_specs import ProxyMode as MitmProxyMode

        addon = self._make_addon(wg_cli_port=51820, wg_gateway_port=51821)
        flow = _make_wg_flow(host="api.anthropic.com")
        flow.client_conn.proxy_mode = MitmProxyMode.parse("wireguard@51821")
        await addon.request(flow)
        assert flow.metadata.get("ccproxy.direction") == "outbound"
        # Should NOT forward to LiteLLM (would cause infinite loop)
        assert flow.request.host == "api.anthropic.com"

    @pytest.mark.asyncio
    async def test_reverse_direction_is_inbound(self) -> None:
        addon = self._make_addon()
        flow = _make_mock_flow(reverse=True)
        flow.id = "rev-dir-1"
        flow.request.pretty_host = "localhost"
        flow.request.host = "localhost"
        flow.request.method = "POST"
        flow.request.path = "/v1/messages"
        flow.request.pretty_url = "http://localhost/v1/messages"
        flow.request.content = None
        await addon.request(flow)
        assert flow.metadata.get("ccproxy.direction") == "inbound"

    @pytest.mark.asyncio
    async def test_wireguard_cli_does_not_forward_non_llm(self) -> None:
        addon = self._make_addon(wg_cli_port=51820, wg_gateway_port=51821)
        flow = _make_wg_flow(host="github.com", path="/api/v3")
        await addon.request(flow)
        assert flow.metadata.get("ccproxy.direction") == "inbound"
        assert flow.request.host == "github.com"

    def test_direction_is_string_literal(self) -> None:
        """Direction metadata uses string literals, not an enum."""
        from mitmproxy.proxy.mode_specs import ProxyMode as MitmProxyMode

        addon = self._make_addon(wg_cli_port=51820, wg_gateway_port=51821)
        flow = _make_wg_flow(host="api.anthropic.com")
        # Confirm _get_direction returns a string literal
        direction = addon._get_direction(flow)
        assert direction == "inbound"

        flow2 = _make_wg_flow(host="api.anthropic.com")
        flow2.client_conn.proxy_mode = MitmProxyMode.parse("wireguard@51821")
        direction2 = addon._get_direction(flow2)
        assert direction2 == "outbound"
