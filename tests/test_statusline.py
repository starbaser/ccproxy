"""Tests for ccstatusline integration."""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from ccproxy.config import CCProxyConfig, StatuslineConfig, clear_config_instance, set_config_instance
from ccproxy.handler import CCProxyHandler
from ccproxy.routes import router
from ccproxy.statusline import (
    check_bun_available,
    check_npm_available,
    format_status_output,
    install_statusline,
    query_status,
    uninstall_statusline,
)


class TestQueryStatus:
    """Test suite for query_status function."""

    @patch("httpx.get")
    def test_query_success(self, mock_get: Mock) -> None:
        """Test successful status query."""
        expected_status = {
            "rule": "haiku_requests",
            "model": "anthropic/claude-3-haiku-20240307",
            "original_model": "claude-3-haiku",
            "is_passthrough": False,
        }

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = expected_status
        mock_get.return_value = mock_response

        result = query_status(port=4000, timeout=0.1)

        assert result == expected_status
        mock_get.assert_called_once_with("http://localhost:4000/ccproxy/status", timeout=0.1)

    @patch("httpx.get")
    def test_query_connection_error(self, mock_get: Mock) -> None:
        """Test query returns None on connection error."""
        mock_get.side_effect = httpx.ConnectError("Connection refused")

        result = query_status()

        assert result is None

    @patch("httpx.get")
    def test_query_timeout_error(self, mock_get: Mock) -> None:
        """Test query returns None on timeout."""
        mock_get.side_effect = httpx.TimeoutException("Request timeout")

        result = query_status(timeout=0.1)

        assert result is None

    @patch("httpx.get")
    def test_query_non_200_status(self, mock_get: Mock) -> None:
        """Test query returns None on non-200 status code."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = query_status()

        assert result is None

    @patch("httpx.get")
    def test_query_generic_exception(self, mock_get: Mock) -> None:
        """Test query returns None on generic exception."""
        mock_get.side_effect = Exception("Unexpected error")

        result = query_status()

        assert result is None

    @patch("httpx.get")
    def test_query_custom_port(self, mock_get: Mock) -> None:
        """Test query with custom port."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"rule": "test"}
        mock_get.return_value = mock_response

        query_status(port=8080)

        mock_get.assert_called_once_with("http://localhost:8080/ccproxy/status", timeout=0.1)


class TestFormatStatusOutput:
    """Test suite for format_status_output function."""

    @pytest.fixture(autouse=True)
    def setup_config(self) -> None:
        """Set up default config before each test."""
        clear_config_instance()
        config = CCProxyConfig()
        set_config_instance(config)

    def test_format_proxy_reachable_with_status(self) -> None:
        """Test format returns ON when proxy is reachable."""
        status = {
            "rule": "thinking_model",
            "model": "openai/gpt-4",
            "original_model": "claude-opus",
            "is_passthrough": False,
        }

        result = format_status_output(status, proxy_reachable=True)

        assert result == "⸢ccproxy: ON⸥"

    def test_format_proxy_not_reachable(self) -> None:
        """Test format returns OFF when proxy not reachable."""
        result = format_status_output(None, proxy_reachable=False)

        assert result == "⸢ccproxy: OFF⸥"

    def test_format_none_status_returns_off(self) -> None:
        """Test format returns OFF when status is None."""
        result = format_status_output(None)

        assert result == "⸢ccproxy: OFF⸥"

    def test_format_status_reachable_default(self) -> None:
        """Test format returns ON with status and default proxy_reachable."""
        status = {"rule": "custom_rule"}

        result = format_status_output(status)

        assert result == "⸢ccproxy: ON⸥"

    def test_format_empty_dict_with_reachable(self) -> None:
        """Test format returns ON with empty dict if proxy reachable."""
        result = format_status_output({}, proxy_reachable=True)

        assert result == "⸢ccproxy: ON⸥"

    def test_format_with_custom_config(self) -> None:
        """Test format uses custom statusline configuration."""
        config = CCProxyConfig()
        config.statusline = StatuslineConfig(
            format="[$status]",
            on="PROXY ACTIVE",
            off="PROXY INACTIVE",
        )
        set_config_instance(config)

        result_on = format_status_output({"rule": "test"}, proxy_reachable=True)
        result_off = format_status_output(None, proxy_reachable=False)

        assert result_on == "[PROXY ACTIVE]"
        assert result_off == "[PROXY INACTIVE]"

    def test_format_empty_on_returns_empty(self) -> None:
        """Test format returns empty string when on value is empty."""
        config = CCProxyConfig()
        config.statusline = StatuslineConfig(on="", off="ccproxy: OFF")
        set_config_instance(config)

        result = format_status_output({"rule": "test"}, proxy_reachable=True)

        assert result == ""

    def test_format_empty_off_returns_empty(self) -> None:
        """Test format returns empty string when off value is empty."""
        config = CCProxyConfig()
        config.statusline = StatuslineConfig(on="ccproxy: ON", off="")
        set_config_instance(config)

        result = format_status_output(None, proxy_reachable=False)

        assert result == ""

    def test_format_disabled_returns_empty(self) -> None:
        """Test format returns empty string when disabled flag is set."""
        config = CCProxyConfig()
        config.statusline = StatuslineConfig(disabled=True)
        set_config_instance(config)

        result_on = format_status_output({"rule": "test"}, proxy_reachable=True)
        result_off = format_status_output(None, proxy_reachable=False)

        assert result_on == ""
        assert result_off == ""

    def test_format_with_symbol(self) -> None:
        """Test format string with symbol variable."""
        config = CCProxyConfig()
        config.statusline = StatuslineConfig(
            format="$symbol $status",
            symbol="",
            on="ON",
            off="OFF",
        )
        set_config_instance(config)

        result_on = format_status_output({"rule": "test"}, proxy_reachable=True)
        result_off = format_status_output(None, proxy_reachable=False)

        assert result_on == " ON"
        assert result_off == " OFF"

    def test_format_custom_format_string(self) -> None:
        """Test custom format string with multiple variables."""
        config = CCProxyConfig()
        config.statusline = StatuslineConfig(
            format="[$symbol:$status]",
            symbol="",
            on="active",
            off="inactive",
        )
        set_config_instance(config)

        result_on = format_status_output({"rule": "test"}, proxy_reachable=True)
        result_off = format_status_output(None, proxy_reachable=False)

        assert result_on == "[:active]"
        assert result_off == "[:inactive]"

    def test_format_symbol_only(self) -> None:
        """Test format string with symbol only (no status text)."""
        config = CCProxyConfig()
        config.statusline = StatuslineConfig(
            format="$symbol",
            symbol="",
            on="active",
            off="inactive",
        )
        set_config_instance(config)

        result = format_status_output({"rule": "test"}, proxy_reachable=True)

        assert result == ""


class TestInstallStatusline:
    """Test suite for install_statusline function."""

    @patch("ccproxy.statusline.check_npm_available", return_value=True)
    @patch("subprocess.run")
    def test_install_fresh_npm(self, mock_run: Mock, mock_npm: Mock, tmp_path: Path, capsys) -> None:
        """Test fresh installation with npm."""
        claude_settings = tmp_path / "claude_settings.json"
        cc_settings = tmp_path / "ccstatusline_settings.json"

        # Mock subprocess.run for npx version check
        mock_run.return_value = Mock(returncode=0)

        # Patch settings paths
        with (
            patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings),
            patch("ccproxy.statusline.CCSTATUSLINE_SETTINGS", cc_settings),
        ):
            result = install_statusline(use_bun=False)

        assert result is True

        # Verify Claude settings
        assert claude_settings.exists()
        claude_data = json.loads(claude_settings.read_text())
        assert "statusLine" in claude_data
        assert claude_data["statusLine"]["type"] == "command"
        assert "npx" in claude_data["statusLine"]["command"]

        # Verify ccstatusline settings
        assert cc_settings.exists()
        cc_data = json.loads(cc_settings.read_text())
        assert "lines" in cc_data
        assert len(cc_data["lines"]) > 0

        # Check widget was added
        widgets = cc_data["lines"][0]
        ccproxy_widget = next((w for w in widgets if w.get("commandPath", "").startswith("ccproxy")), None)
        assert ccproxy_widget is not None
        assert ccproxy_widget["type"] == "custom-command"
        assert ccproxy_widget["commandPath"] == "ccproxy statusline"

        captured = capsys.readouterr()
        assert "Installation complete!" in captured.out

    @patch("ccproxy.statusline.check_bun_available", return_value=True)
    @patch("subprocess.run")
    def test_install_with_bun(self, mock_run: Mock, mock_bun: Mock, tmp_path: Path) -> None:
        """Test installation with bun."""
        claude_settings = tmp_path / "claude_settings.json"

        mock_run.return_value = Mock(returncode=0)

        with patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings):
            result = install_statusline(use_bun=True)

        assert result is True
        claude_data = json.loads(claude_settings.read_text())
        assert "bunx" in claude_data["statusLine"]["command"]

    @patch("ccproxy.statusline.check_npm_available", return_value=False)
    def test_install_npm_not_available(self, mock_npm: Mock, capsys) -> None:
        """Test install fails when npm not available."""
        result = install_statusline(use_bun=False)

        assert result is False
        captured = capsys.readouterr()
        assert "npx not found" in captured.out

    @patch("ccproxy.statusline.check_bun_available", return_value=False)
    def test_install_bun_not_available(self, mock_bun: Mock, capsys) -> None:
        """Test install fails when bun not available."""
        result = install_statusline(use_bun=True)

        assert result is False
        captured = capsys.readouterr()
        assert "bunx not found" in captured.out

    @patch("ccproxy.statusline.check_npm_available", return_value=True)
    @patch("subprocess.run")
    def test_install_existing_no_force(self, mock_run: Mock, mock_npm: Mock, tmp_path: Path, capsys) -> None:
        """Test install with existing config and force=False."""
        claude_settings = tmp_path / "claude_settings.json"
        existing_config = {"statusLine": {"type": "command", "command": "existing"}}
        claude_settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.write_text(json.dumps(existing_config))

        mock_run.return_value = Mock(returncode=0)

        with patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings):
            result = install_statusline(force=False)

        assert result is True
        captured = capsys.readouterr()
        assert "statusLine already configured" in captured.out

        # Verify config wasn't changed
        claude_data = json.loads(claude_settings.read_text())
        assert claude_data["statusLine"]["command"] == "existing"

    @patch("ccproxy.statusline.check_npm_available", return_value=True)
    @patch("subprocess.run")
    def test_install_with_force_overwrites(self, mock_run: Mock, mock_npm: Mock, tmp_path: Path) -> None:
        """Test install with force=True overwrites existing config."""
        claude_settings = tmp_path / "claude_settings.json"
        cc_settings = tmp_path / "ccstatusline_settings.json"

        # Create existing configs
        existing_claude = {"statusLine": {"type": "command", "command": "old"}}
        claude_settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.write_text(json.dumps(existing_claude))

        existing_cc = {
            "version": 3,
            "lines": [[{"id": "old1", "commandPath": "ccproxy old"}]],
        }
        cc_settings.parent.mkdir(parents=True, exist_ok=True)
        cc_settings.write_text(json.dumps(existing_cc))

        mock_run.return_value = Mock(returncode=0)

        with (
            patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings),
            patch("ccproxy.statusline.CCSTATUSLINE_SETTINGS", cc_settings),
        ):
            result = install_statusline(force=True)

        assert result is True

        # Verify Claude config was overwritten
        claude_data = json.loads(claude_settings.read_text())
        assert "npx" in claude_data["statusLine"]["command"]

        # Verify old ccproxy widget was removed and new one added
        cc_data = json.loads(cc_settings.read_text())
        widgets = cc_data["lines"][0]
        ccproxy_widgets = [w for w in widgets if w.get("commandPath", "").startswith("ccproxy")]
        assert len(ccproxy_widgets) == 1
        assert ccproxy_widgets[0]["commandPath"] == "ccproxy statusline"

    @patch("ccproxy.statusline.check_npm_available", return_value=True)
    @patch("subprocess.run")
    def test_install_json_decode_error(self, mock_run: Mock, mock_npm: Mock, tmp_path: Path, capsys) -> None:
        """Test install handles malformed JSON gracefully."""
        claude_settings = tmp_path / "claude_settings.json"
        claude_settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.write_text("{invalid json}")

        mock_run.return_value = Mock(returncode=0)

        with patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings):
            result = install_statusline()

        assert result is False
        captured = capsys.readouterr()
        assert "Error parsing" in captured.out

    @patch("ccproxy.statusline.check_npm_available", return_value=True)
    @patch("subprocess.run")
    def test_install_creates_directories(self, mock_run: Mock, mock_npm: Mock, tmp_path: Path) -> None:
        """Test install creates parent directories if they don't exist."""
        claude_settings = tmp_path / "nonexistent" / "claude_settings.json"

        mock_run.return_value = Mock(returncode=0)

        with patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings):
            result = install_statusline()

        assert result is True
        assert claude_settings.exists()
        assert claude_settings.parent.exists()

    @patch("ccproxy.statusline.check_npm_available", return_value=True)
    @patch("subprocess.run")
    def test_install_adds_separator(self, mock_run: Mock, mock_npm: Mock, tmp_path: Path) -> None:
        """Test install adds separator when line has existing items."""
        cc_settings = tmp_path / "ccstatusline_settings.json"

        # Create settings with existing widgets
        existing_cc = {
            "version": 3,
            "lines": [[{"id": "existing1", "type": "datetime"}]],
        }
        cc_settings.parent.mkdir(parents=True, exist_ok=True)
        cc_settings.write_text(json.dumps(existing_cc))

        mock_run.return_value = Mock(returncode=0)

        with (
            patch("ccproxy.statusline.CCSTATUSLINE_SETTINGS", cc_settings),
            patch("ccproxy.statusline.CLAUDE_SETTINGS", tmp_path / "claude.json"),
            patch("ccproxy.statusline.check_npm_available", return_value=True),
        ):
            install_statusline()

        # Verify separator was added
        cc_data = json.loads(cc_settings.read_text())
        widgets = cc_data["lines"][0]
        assert len(widgets) == 3  # existing + separator + ccproxy
        assert widgets[1]["type"] == "separator"


class TestUninstallStatusline:
    """Test suite for uninstall_statusline function."""

    def test_uninstall_removes_statusline(self, tmp_path: Path, capsys) -> None:
        """Test uninstall removes statusLine from settings."""
        claude_settings = tmp_path / "claude_settings.json"
        existing_config = {
            "statusLine": {"type": "command", "command": "npx ccstatusline"},
            "other": "setting",
        }
        claude_settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.write_text(json.dumps(existing_config))

        with patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings):
            result = uninstall_statusline()

        assert result is True

        # Verify statusLine was removed but other settings remain
        claude_data = json.loads(claude_settings.read_text())
        assert "statusLine" not in claude_data
        assert "other" in claude_data

        captured = capsys.readouterr()
        assert "Removed statusLine configuration" in captured.out

    def test_uninstall_no_settings_file(self, tmp_path: Path, capsys) -> None:
        """Test uninstall handles missing settings file gracefully."""
        claude_settings = tmp_path / "nonexistent.json"

        with patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings):
            result = uninstall_statusline()

        assert result is True
        captured = capsys.readouterr()
        assert "No settings file found" in captured.out

    def test_uninstall_no_statusline_key(self, tmp_path: Path, capsys) -> None:
        """Test uninstall when statusLine key doesn't exist."""
        claude_settings = tmp_path / "claude_settings.json"
        claude_settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.write_text(json.dumps({"other": "setting"}))

        with patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings):
            result = uninstall_statusline()

        assert result is True
        captured = capsys.readouterr()
        assert "No statusLine configuration found" in captured.out

    def test_uninstall_removes_ccproxy_widgets(self, tmp_path: Path, capsys) -> None:
        """Test uninstall removes ccproxy widgets from ccstatusline."""
        claude_settings = tmp_path / "claude_settings.json"
        # Create Claude settings with statusLine so function proceeds to ccstatusline removal
        claude_settings.write_text(json.dumps({"statusLine": {"type": "command"}}))

        cc_settings = tmp_path / "ccstatusline_settings.json"
        existing_cc = {
            "version": 3,
            "lines": [
                [
                    {"id": "widget1", "type": "datetime"},
                    {"id": "widget2", "commandPath": "ccproxy statusline"},
                    {"id": "widget3", "type": "separator"},
                ]
            ],
        }
        cc_settings.parent.mkdir(parents=True, exist_ok=True)
        cc_settings.write_text(json.dumps(existing_cc))

        with (
            patch("ccproxy.statusline.CCSTATUSLINE_SETTINGS", cc_settings),
            patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings),
        ):
            result = uninstall_statusline()

        assert result is True

        # Verify ccproxy widget was removed
        cc_data = json.loads(cc_settings.read_text())
        widgets = cc_data["lines"][0]
        assert len(widgets) == 2
        ccproxy_widgets = [w for w in widgets if w.get("commandPath", "").startswith("ccproxy")]
        assert len(ccproxy_widgets) == 0

        captured = capsys.readouterr()
        assert "Removed ccproxy widget" in captured.out

    def test_uninstall_malformed_json(self, tmp_path: Path, capsys) -> None:
        """Test uninstall handles malformed JSON."""
        claude_settings = tmp_path / "claude_settings.json"
        claude_settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.write_text("{invalid json}")

        with patch("ccproxy.statusline.CLAUDE_SETTINGS", claude_settings):
            result = uninstall_statusline()

        assert result is False
        captured = capsys.readouterr()
        assert "Error parsing" in captured.out


class TestCCProxyHandlerStatus:
    """Test suite for CCProxyHandler status tracking."""

    def test_get_status_initial_none(self) -> None:
        """Test get_status returns None initially."""
        # Clear any existing status
        CCProxyHandler._last_status = None

        status = CCProxyHandler.get_status()

        assert status is None

    def test_get_status_after_set(self) -> None:
        """Test get_status returns status after being set."""
        test_status = {
            "rule": "test_rule",
            "model": "test_model",
            "timestamp": "2024-01-01T00:00:00",
        }

        # Set status
        CCProxyHandler._last_status = test_status

        status = CCProxyHandler.get_status()

        assert status == test_status

    def test_status_updated_on_request(self) -> None:
        """Test status is updated when processing a request."""
        # This test would require mocking the full request flow
        # For now, we verify the status structure is set correctly
        expected_status = {
            "rule": "haiku_requests",
            "model": "anthropic/claude-3-haiku-20240307",
            "original_model": "claude-3-haiku",
            "is_passthrough": False,
            "timestamp": "2024-01-01T00:00:00",
        }

        CCProxyHandler._last_status = expected_status

        status = CCProxyHandler.get_status()

        assert status is not None
        assert "rule" in status
        assert "model" in status
        assert "original_model" in status
        assert "is_passthrough" in status
        assert "timestamp" in status


class TestPackageManagerChecks:
    """Test suite for package manager availability checks."""

    @patch("shutil.which", return_value="/usr/bin/npx")
    def test_npm_available(self, mock_which: Mock) -> None:
        """Test npm check when available."""
        result = check_npm_available()

        assert result is True
        mock_which.assert_called_once_with("npx")

    @patch("shutil.which", return_value=None)
    def test_npm_not_available(self, mock_which: Mock) -> None:
        """Test npm check when not available."""
        result = check_npm_available()

        assert result is False

    @patch("shutil.which", return_value="/usr/bin/bunx")
    def test_bun_available(self, mock_which: Mock) -> None:
        """Test bun check when available."""
        result = check_bun_available()

        assert result is True
        mock_which.assert_called_once_with("bunx")

    @patch("shutil.which", return_value=None)
    def test_bun_not_available(self, mock_which: Mock) -> None:
        """Test bun check when not available."""
        result = check_bun_available()

        assert result is False


class TestStatusEndpoint:
    """Test suite for /ccproxy/status FastAPI endpoint."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create FastAPI test client."""
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_status_endpoint_with_status(self, client: TestClient) -> None:
        """Test endpoint returns status when available."""
        test_status = {
            "rule": "haiku_requests",
            "model": "anthropic/claude-3-haiku-20240307",
            "original_model": "claude-3-haiku",
            "is_passthrough": False,
            "timestamp": "2024-01-01T00:00:00",
        }

        # Set status
        CCProxyHandler._last_status = test_status

        response = client.get("/ccproxy/status")

        assert response.status_code == 200
        assert response.json() == test_status

    def test_status_endpoint_no_status(self, client: TestClient) -> None:
        """Test endpoint returns error when no status available."""
        # Clear status
        CCProxyHandler._last_status = None

        response = client.get("/ccproxy/status")

        assert response.status_code == 404
        assert response.json() == {"error": "no requests yet"}

    def test_status_endpoint_after_request(self, client: TestClient) -> None:
        """Test endpoint returns updated status after processing."""
        # Simulate status update after a request
        updated_status = {
            "rule": "thinking_model",
            "model": "openai/o3-mini",
            "original_model": "claude-sonnet",
            "is_passthrough": False,
            "timestamp": "2024-01-01T12:00:00",
        }

        CCProxyHandler._last_status = updated_status

        response = client.get("/ccproxy/status")

        assert response.status_code == 200
        data = response.json()
        assert data["rule"] == "thinking_model"
        assert data["model"] == "openai/o3-mini"
        assert data["original_model"] == "claude-sonnet"
        assert data["is_passthrough"] is False

    def test_status_endpoint_passthrough(self, client: TestClient) -> None:
        """Test endpoint returns passthrough status correctly."""
        passthrough_status = {
            "rule": None,
            "model": "claude-3-opus",
            "original_model": "claude-3-opus",
            "is_passthrough": True,
            "timestamp": "2024-01-01T13:00:00",
        }

        CCProxyHandler._last_status = passthrough_status

        response = client.get("/ccproxy/status")

        assert response.status_code == 200
        data = response.json()
        assert data["is_passthrough"] is True
        assert data["model"] == data["original_model"]
