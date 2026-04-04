"""Tests for inspector addon traffic capture."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccproxy.config import InspectorConfig
from ccproxy.inspector.addon import InspectorAddon, ProxyDirection


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
    """Tests for the request method trace capture."""

    @pytest.mark.asyncio
    async def test_request_works_without_storage(self, mock_flow: MagicMock) -> None:
        """request() should return early without storage configured."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=None, config=config)

        mock_flow.request.pretty_host = "api.anthropic.com"

        await addon.request(mock_flow)


class TestProxyModeDetection:
    """Tests for internal proxy mode detection via proxy_mode per-flow.

    ProxyDirection values are internal implementation details — they identify
    which mitmproxy listener handled a flow and are stored in the database.
    They are not user-facing concepts; inspect mode activates all listeners
    as a single unit.
    """

    @pytest.fixture
    def mock_storage(self) -> AsyncMock:
        """Create mock storage."""
        storage = AsyncMock()
        storage.create_trace = AsyncMock()
        return storage

    @pytest.mark.asyncio
    async def test_reverse_proxy_captures_traffic(self, mock_storage: AsyncMock) -> None:
        """Reverse listener flow should be captured with REVERSE mode identifier."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=mock_storage, config=config)

        flow = _make_mock_flow(reverse=True)
        flow.id = "flow-1"
        flow.request.pretty_host = "localhost"
        flow.request.method = "POST"
        flow.request.path = "/v1/chat/completions"
        flow.request.pretty_url = "http://localhost/v1/chat/completions"
        flow.request.content = None

        await addon.request(flow)
        assert mock_storage.create_trace.called

    @pytest.mark.asyncio
    async def test_forward_proxy_captures_traffic(self, mock_storage: AsyncMock) -> None:
        """Regular listener flow should be captured with FORWARD mode identifier."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=mock_storage, config=config)

        flow = _make_mock_flow(reverse=False)
        flow.id = "flow-1"
        flow.request.pretty_host = "api.anthropic.com"
        flow.request.method = "POST"
        flow.request.path = "/v1/messages"
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.request.content = None

        await addon.request(flow)
        assert mock_storage.create_trace.called

    @pytest.mark.asyncio
    async def test_forward_proxy_captures_langfuse(self, mock_storage: AsyncMock) -> None:
        """Regular listener should capture Langfuse API calls."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=mock_storage, config=config)

        flow = _make_mock_flow(reverse=False)
        flow.id = "flow-1"
        flow.request.pretty_host = "us.cloud.langfuse.com"
        flow.request.method = "GET"
        flow.request.path = "/api/public/projects"
        flow.request.pretty_url = "https://us.cloud.langfuse.com/api/public/projects"
        flow.request.content = None

        await addon.request(flow)
        assert mock_storage.create_trace.called

    @pytest.mark.asyncio
    async def test_proxy_direction_stored_correctly(self, mock_storage: AsyncMock) -> None:
        """ProxyDirection integer should be stored in trace data based on per-flow proxy_mode."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=mock_storage, config=config)

        # Test REVERSE direction
        flow_reverse = _make_mock_flow(reverse=True)
        flow_reverse.id = "flow-1"
        flow_reverse.request.pretty_host = "localhost"
        flow_reverse.request.method = "POST"
        flow_reverse.request.path = "/v1/chat/completions"
        flow_reverse.request.pretty_url = "http://localhost/v1/chat/completions"
        flow_reverse.request.content = None

        await addon.request(flow_reverse)
        call_args = mock_storage.create_trace.call_args[0][0]
        assert call_args["proxy_direction"] == ProxyDirection.REVERSE.value

        # Test FORWARD direction
        mock_storage.reset_mock()
        flow_forward = _make_mock_flow(reverse=False)
        flow_forward.id = "flow-2"
        flow_forward.request.pretty_host = "api.anthropic.com"
        flow_forward.request.method = "POST"
        flow_forward.request.path = "/v1/messages"
        flow_forward.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow_forward.request.content = None

        await addon.request(flow_forward)
        call_args = mock_storage.create_trace.call_args[0][0]
        assert call_args["proxy_direction"] == ProxyDirection.FORWARD.value


class TestWireGuardForwarding:
    """Tests for WireGuard LLM API domain forwarding to LiteLLM."""

    @pytest.fixture(autouse=True)
    def _set_litellm_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CCPROXY_LITELLM_PORT", "4001")

    @pytest.fixture
    def mock_storage(self) -> AsyncMock:
        storage = AsyncMock()
        storage.create_trace = AsyncMock()
        return storage

    @pytest.mark.asyncio
    async def test_forwards_anthropic_to_litellm(self, mock_storage: AsyncMock) -> None:
        """WireGuard flow to api.anthropic.com should be forwarded to LiteLLM."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=mock_storage, config=config)

        flow = _make_wg_flow(host="api.anthropic.com")
        await addon.request(flow)

        assert flow.request.host == "localhost"
        assert flow.request.port == 4001
        assert flow.request.scheme == "http"
        assert flow.request.headers["X-Forwarded-Host"] == "api.anthropic.com"

    @pytest.mark.asyncio
    async def test_forwards_openai_to_litellm(self, mock_storage: AsyncMock) -> None:
        """WireGuard flow to api.openai.com should be forwarded to LiteLLM."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=mock_storage, config=config)

        flow = _make_wg_flow(host="api.openai.com")
        await addon.request(flow)

        assert flow.request.host == "localhost"
        assert flow.request.port == 4001
        assert flow.request.scheme == "http"

    @pytest.mark.asyncio
    async def test_non_llm_domain_passes_through(self, mock_storage: AsyncMock) -> None:
        """WireGuard flow to non-LLM domains should not be forwarded."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=mock_storage, config=config)

        flow = _make_wg_flow(host="github.com", path="/api/v3/repos")
        await addon.request(flow)

        assert flow.request.host == "github.com"
        assert flow.request.port == 443
        assert flow.request.scheme == "https"

    @pytest.mark.asyncio
    async def test_reverse_flow_not_forwarded(self, mock_storage: AsyncMock) -> None:
        """Reverse proxy flows should never be forwarded, even for LLM domains."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=mock_storage, config=config)

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
    async def test_custom_forward_domains(self, mock_storage: AsyncMock) -> None:
        """Custom forward_domains in config should be respected."""
        config = InspectorConfig(
            forward_domains=["custom-llm.example.com"],
        )
        addon = InspectorAddon(storage=mock_storage, config=config)

        flow = _make_wg_flow(host="custom-llm.example.com")
        await addon.request(flow)
        assert flow.request.host == "localhost"
        assert flow.request.port == 4001

        # Default domain should NOT be forwarded when custom list replaces it
        flow2 = _make_wg_flow(host="api.anthropic.com")
        await addon.request(flow2)
        assert flow2.request.host == "api.anthropic.com"

    @pytest.mark.asyncio
    async def test_trace_captures_original_host(self, mock_storage: AsyncMock) -> None:
        """Trace should record the original host, not the rewritten one."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=mock_storage, config=config)

        flow = _make_wg_flow(host="api.anthropic.com")
        await addon.request(flow)

        trace_data = mock_storage.create_trace.call_args[0][0]
        assert trace_data["host"] == "api.anthropic.com"

    @pytest.mark.asyncio
    async def test_forwarding_works_without_storage(self) -> None:
        """Forwarding should still rewrite the request even without storage."""
        config = InspectorConfig()
        addon = InspectorAddon(storage=None, config=config)

        flow = _make_wg_flow(host="api.anthropic.com")
        await addon.request(flow)

        assert flow.request.host == "localhost"
        assert flow.request.port == 4001
        assert flow.request.scheme == "http"
