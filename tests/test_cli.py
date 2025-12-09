"""Tests for the ccproxy CLI."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from ccproxy.cli import (
    Install,
    Logs,
    Run,
    Start,
    Status,
    Stop,
    generate_handler_file,
    install_config,
    main,
    run_with_proxy,
    show_status,
    start_litellm,
    stop_litellm,
    view_logs,
)


class TestStartProxy:
    """Test suite for start_proxy function."""

    def test_litellm_no_config(self, tmp_path: Path, capsys) -> None:
        """Test litellm when config doesn't exist."""
        with pytest.raises(SystemExit) as exc_info:
            start_litellm(tmp_path)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Configuration not found" in captured.err
        assert "Run 'ccproxy install' first" in captured.err

    @patch("subprocess.run")
    def test_start_proxy_success(self, mock_run: Mock, tmp_path: Path) -> None:
        """Test successful litellm execution."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("litellm: config")

        mock_run.return_value = Mock(returncode=0)

        with pytest.raises(SystemExit) as exc_info:
            start_litellm(tmp_path)

        assert exc_info.value.code == 0
        # Check the command structure - first arg is the litellm executable path
        call_args = mock_run.call_args[0][0]
        assert call_args[0].endswith("litellm")
        assert call_args[1:] == ["--config", str(config_file)]

    @patch("subprocess.run")
    def test_litellm_with_args(self, mock_run: Mock, tmp_path: Path) -> None:
        """Test litellm with additional arguments."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("litellm: config")

        mock_run.return_value = Mock(returncode=0)

        with pytest.raises(SystemExit) as exc_info:
            start_litellm(tmp_path, args=["--debug", "--port", "8080"])

        assert exc_info.value.code == 0
        # Check the command structure - first arg is the litellm executable path
        call_args = mock_run.call_args[0][0]
        assert call_args[0].endswith("litellm")
        assert call_args[1:] == ["--config", str(config_file), "--debug", "--port", "8080"]

    @patch("subprocess.run")
    def test_litellm_command_not_found(self, mock_run: Mock, tmp_path: Path, capsys) -> None:
        """Test litellm when command is not found."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("litellm: config")

        mock_run.side_effect = FileNotFoundError()

        with pytest.raises(SystemExit) as exc_info:
            start_litellm(tmp_path)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "litellm command not found" in captured.err
        assert "pip install litellm" in captured.err

    @patch("subprocess.run")
    def test_litellm_keyboard_interrupt(self, mock_run: Mock, tmp_path: Path) -> None:
        """Test litellm with keyboard interrupt."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("litellm: config")

        mock_run.side_effect = KeyboardInterrupt()

        with pytest.raises(SystemExit) as exc_info:
            start_litellm(tmp_path)

        assert exc_info.value.code == 130

    @patch("subprocess.Popen")
    def test_litellm_detach_success(self, mock_popen: Mock, tmp_path: Path, capsys) -> None:
        """Test successful litellm execution in detached mode."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("litellm: config")

        mock_process = Mock()
        mock_process.pid = 12345
        mock_popen.return_value = mock_process

        with pytest.raises(SystemExit) as exc_info:
            start_litellm(tmp_path, detach=True)

        assert exc_info.value.code == 0

        # Check PID file was created
        pid_file = tmp_path / "litellm.lock"
        assert pid_file.exists()
        assert pid_file.read_text() == "12345"

        # Check output
        captured = capsys.readouterr()
        assert "LiteLLM started in background" in captured.out
        assert "Log file:" in captured.out
        assert str(tmp_path / "litellm.log") in captured.out

    @patch("os.kill")
    def test_litellm_detach_already_running(self, mock_kill: Mock, tmp_path: Path, capsys) -> None:
        """Test litellm detach when already running."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("litellm: config")

        # Create existing PID file
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("67890")

        # Mock process is still running
        mock_kill.return_value = None

        with pytest.raises(SystemExit) as exc_info:
            start_litellm(tmp_path, detach=True)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "LiteLLM is already running with PID 67890" in captured.err

    @patch("subprocess.Popen")
    @patch("os.kill")
    def test_litellm_detach_stale_pid(self, mock_kill: Mock, mock_popen: Mock, tmp_path: Path) -> None:
        """Test litellm detach with stale PID file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("litellm: config")

        # Create existing PID file
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("67890")

        # Mock process is not running (raises ProcessLookupError)
        mock_kill.side_effect = ProcessLookupError()

        mock_process = Mock()
        mock_process.pid = 12345
        mock_popen.return_value = mock_process

        with pytest.raises(SystemExit) as exc_info:
            start_litellm(tmp_path, detach=True)

        assert exc_info.value.code == 0

        # Check PID file was updated
        assert pid_file.read_text() == "12345"

    @patch("subprocess.Popen")
    @patch("os.kill")
    def test_litellm_detach_invalid_pid_file(self, mock_kill: Mock, mock_popen: Mock, tmp_path: Path) -> None:
        """Test litellm detach with invalid PID file content."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("litellm: config")

        # Create PID file with invalid content
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("not-a-number")

        mock_process = Mock()
        mock_process.pid = 12345
        mock_popen.return_value = mock_process

        with pytest.raises(SystemExit) as exc_info:
            start_litellm(tmp_path, detach=True)

        assert exc_info.value.code == 0
        # Check PID file was updated with new PID
        assert pid_file.read_text() == "12345"

    @patch("subprocess.Popen")
    def test_litellm_detach_file_not_found(self, mock_popen: Mock, tmp_path: Path) -> None:
        """Test litellm detach when command is not found."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("litellm: config")

        # Mock FileNotFoundError (command not found)
        mock_popen.side_effect = FileNotFoundError("Command not found")

        with pytest.raises(SystemExit) as exc_info:
            start_litellm(tmp_path, detach=True)

        assert exc_info.value.code == 1


class TestInstallConfig:
    """Test suite for install_config function."""

    @patch("ccproxy.cli.get_templates_dir")
    def test_install_fresh(self, mock_get_templates: Mock, tmp_path: Path, capsys) -> None:
        """Test fresh installation."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()

        # Create template files (ccproxy.py is no longer a template - it's auto-generated on start)
        (templates_dir / "ccproxy.yaml").write_text("test: config")
        (templates_dir / "config.yaml").write_text("litellm: config")

        mock_get_templates.return_value = templates_dir

        config_dir = tmp_path / "config"
        install_config(config_dir)

        assert (config_dir / "ccproxy.yaml").exists()
        assert (config_dir / "config.yaml").exists()
        # ccproxy.py is not installed - it's generated on startup

        captured = capsys.readouterr()
        assert "Installation complete!" in captured.out
        assert "Next steps:" in captured.out

    def test_install_exists_no_force(self, tmp_path: Path, capsys) -> None:
        """Test install when config already exists without force."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with pytest.raises(SystemExit) as exc_info:
            install_config(config_dir, force=False)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "already" in captured.out and "exists" in captured.out
        assert "Use --force to overwrite" in captured.out

    @patch("ccproxy.cli.get_templates_dir")
    def test_install_with_force(self, mock_get_templates: Mock, tmp_path: Path, capsys) -> None:
        """Test install with force overwrites existing files."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "ccproxy.yaml").write_text("new: config")
        (templates_dir / "config.yaml").write_text("new: litellm")

        mock_get_templates.return_value = templates_dir

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "ccproxy.yaml").write_text("old: config")

        install_config(config_dir, force=True)

        assert (config_dir / "ccproxy.yaml").read_text() == "new: config"
        captured = capsys.readouterr()
        assert "Copied ccproxy.yaml" in captured.out

    @patch("ccproxy.cli.get_templates_dir")
    def test_install_template_not_found(self, mock_get_templates: Mock, tmp_path: Path, capsys) -> None:
        """Test install when template file is missing."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        # Only create some template files
        (templates_dir / "ccproxy.yaml").write_text("test: config")

        mock_get_templates.return_value = templates_dir

        config_dir = tmp_path / "config"
        install_config(config_dir)

        captured = capsys.readouterr()
        assert "Warning: Template config.yaml not found" in captured.err
        # ccproxy.py is no longer a template, so no warning expected

    def test_install_template_dir_error(self, tmp_path: Path) -> None:
        """Test install when get_templates_dir raises RuntimeError."""
        config_dir = tmp_path / "config"

        with patch("ccproxy.cli.get_templates_dir", side_effect=RuntimeError("Templates not found")):
            with pytest.raises(SystemExit) as exc_info:
                install_config(config_dir)
            assert exc_info.value.code == 1

    def test_install_skip_existing_file(self, tmp_path: Path, capsys) -> None:
        """Test install skips existing files without force flag."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "ccproxy.yaml").write_text("template content")

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "ccproxy.yaml").write_text("existing content")

        with patch("ccproxy.cli.get_templates_dir", return_value=templates_dir):
            with pytest.raises(SystemExit) as exc_info:
                install_config(config_dir)
            assert exc_info.value.code == 1

        # Verify file wasn't overwritten
        assert (config_dir / "ccproxy.yaml").read_text() == "existing content"


class TestHandlerGeneration:
    """Test suite for generate_handler_file function."""

    def test_generate_handler_default(self, tmp_path: Path) -> None:
        """Test handler generation with default configuration."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Create minimal ccproxy.yaml with default handler
        (config_dir / "ccproxy.yaml").write_text(
            """
ccproxy:
  handler: "ccproxy.handler:CCProxyHandler"
"""
        )

        generate_handler_file(config_dir)

        handler_file = config_dir / "ccproxy.py"
        assert handler_file.exists()

        content = handler_file.read_text()
        assert "from ccproxy.handler import CCProxyHandler" in content
        assert "handler = CCProxyHandler()" in content
        assert "Auto-generated" in content
        assert "DO NOT EDIT" in content

    def test_generate_handler_custom(self, tmp_path: Path) -> None:
        """Test handler generation with custom handler class."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Create ccproxy.yaml with custom handler
        (config_dir / "ccproxy.yaml").write_text(
            """
ccproxy:
  handler: "mypackage.custom:MyCustomHandler"
"""
        )

        generate_handler_file(config_dir)

        handler_file = config_dir / "ccproxy.py"
        content = handler_file.read_text()
        assert "from mypackage.custom import MyCustomHandler" in content
        assert "handler = MyCustomHandler()" in content

    def test_generate_handler_no_colon(self, tmp_path: Path) -> None:
        """Test handler generation with module path only (no colon)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Handler without colon should use CCProxyHandler as class name
        (config_dir / "ccproxy.yaml").write_text(
            """
ccproxy:
  handler: "ccproxy.handler"
"""
        )

        generate_handler_file(config_dir)

        handler_file = config_dir / "ccproxy.py"
        content = handler_file.read_text()
        assert "from ccproxy.handler import CCProxyHandler" in content
        assert "handler = CCProxyHandler()" in content

    def test_generate_handler_missing_config(self, tmp_path: Path) -> None:
        """Test handler generation when ccproxy.yaml doesn't exist."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Should use default handler when config is missing
        generate_handler_file(config_dir)

        handler_file = config_dir / "ccproxy.py"
        assert handler_file.exists()
        content = handler_file.read_text()
        assert "from ccproxy.handler import CCProxyHandler" in content
        assert "handler = CCProxyHandler()" in content

    def test_generate_handler_malformed_yaml(self, tmp_path: Path) -> None:
        """Test handler generation with malformed YAML."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Create malformed YAML
        (config_dir / "ccproxy.yaml").write_text("invalid: {yaml: [")

        # Should fall back to default handler
        generate_handler_file(config_dir)

        handler_file = config_dir / "ccproxy.py"
        assert handler_file.exists()
        content = handler_file.read_text()
        assert "from ccproxy.handler import CCProxyHandler" in content

    def test_generate_handler_missing_handler_key(self, tmp_path: Path) -> None:
        """Test handler generation when handler key is missing from config."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Config without handler key
        (config_dir / "ccproxy.yaml").write_text(
            """
ccproxy:
  debug: true
"""
        )

        # Should fall back to default handler
        generate_handler_file(config_dir)

        handler_file = config_dir / "ccproxy.py"
        content = handler_file.read_text()
        assert "from ccproxy.handler import CCProxyHandler" in content

    def test_generate_handler_preserve_custom(self, tmp_path: Path) -> None:
        """Test that custom handler files are preserved (not overwritten)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        handler_file = config_dir / "ccproxy.py"
        handler_file.write_text("# custom user content")

        (config_dir / "ccproxy.yaml").write_text(
            """
ccproxy:
  handler: "new.module:NewHandler"
"""
        )

        generate_handler_file(config_dir)

        # Custom file should be preserved
        content = handler_file.read_text()
        assert "# custom user content" in content
        assert "from new.module import NewHandler" not in content

    def test_generate_handler_overwrite_autogenerated(self, tmp_path: Path) -> None:
        """Test that auto-generated files get overwritten with new content."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Create an auto-generated file with the marker
        handler_file = config_dir / "ccproxy.py"
        old_autogen_content = '''"""
Auto-generated handler file for LiteLLM callbacks.
This file is generated by ccproxy on startup.
DO NOT EDIT - changes will be overwritten.
"""
import sys

from ccproxy.handler import CCProxyHandler

handler = CCProxyHandler()
'''
        handler_file.write_text(old_autogen_content)

        # Configure new handler
        (config_dir / "ccproxy.yaml").write_text(
            """
ccproxy:
  handler: "new.module:NewHandler"
"""
        )

        # Generate handler file
        generate_handler_file(config_dir)

        # Verify it was overwritten with new content
        content = handler_file.read_text()
        assert "from new.module import NewHandler" in content
        assert "handler = NewHandler()" in content
        assert "Auto-generated handler file" in content
        assert "DO NOT EDIT" in content
        assert "from ccproxy.handler import CCProxyHandler" not in content

    def test_generate_handler_preserve_custom_file(self, tmp_path: Path, capsys) -> None:
        """Test that custom files (without auto-generated marker) are preserved."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Create a custom handler file WITHOUT the auto-generated marker
        handler_file = config_dir / "ccproxy.py"
        custom_content = '''"""
Custom handler file written by user.
"""
from ccproxy.handler import CCProxyHandler

class CustomHandler(CCProxyHandler):
    def custom_method(self):
        pass

handler = CustomHandler()
'''
        handler_file.write_text(custom_content)

        # Configure handler
        (config_dir / "ccproxy.yaml").write_text(
            """
ccproxy:
  handler: "ccproxy.handler:CCProxyHandler"
"""
        )

        # Generate handler file
        generate_handler_file(config_dir)

        # Verify file was NOT overwritten
        content = handler_file.read_text()
        assert content == custom_content
        assert "Custom handler file written by user" in content
        assert "custom_method" in content

        # Verify warning was printed to stderr
        captured = capsys.readouterr()
        assert "Custom ccproxy.py file detected" in captured.err
        assert "will NOT be overwritten" in captured.err

    def test_generate_handler_no_file_creates_new(self, tmp_path: Path) -> None:
        """Test that handler generation creates new file when none exists."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        handler_file = config_dir / "ccproxy.py"
        assert not handler_file.exists()

        # Configure handler
        (config_dir / "ccproxy.yaml").write_text(
            """
ccproxy:
  handler: "ccproxy.handler:CCProxyHandler"
"""
        )

        # Generate handler file
        generate_handler_file(config_dir)

        # Verify file was created
        assert handler_file.exists()
        content = handler_file.read_text()
        assert "from ccproxy.handler import CCProxyHandler" in content
        assert "handler = CCProxyHandler()" in content
        assert "Auto-generated handler file" in content

    def test_generate_handler_empty_file_treated_as_custom(self, tmp_path: Path, capsys) -> None:
        """Test that empty file is treated as custom and preserved."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Create empty file
        handler_file = config_dir / "ccproxy.py"
        handler_file.write_text("")

        # Configure handler
        (config_dir / "ccproxy.yaml").write_text(
            """
ccproxy:
  handler: "ccproxy.handler:CCProxyHandler"
"""
        )

        # Generate handler file
        generate_handler_file(config_dir)

        # Verify empty file was preserved (treated as custom)
        content = handler_file.read_text()
        assert content == ""

        # Verify warning was printed
        captured = capsys.readouterr()
        assert "Custom ccproxy.py file detected" in captured.err
        assert "will NOT be overwritten" in captured.err

    def test_generate_handler_whitespace_only_treated_as_custom(self, tmp_path: Path, capsys) -> None:
        """Test that whitespace-only file is treated as custom and preserved."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Create file with only whitespace
        handler_file = config_dir / "ccproxy.py"
        whitespace_content = "   \n\n\t\n  "
        handler_file.write_text(whitespace_content)

        # Configure handler
        (config_dir / "ccproxy.yaml").write_text(
            """
ccproxy:
  handler: "ccproxy.handler:CCProxyHandler"
"""
        )

        # Generate handler file
        generate_handler_file(config_dir)

        # Verify whitespace file was preserved
        content = handler_file.read_text()
        assert content == whitespace_content

        # Verify warning was printed
        captured = capsys.readouterr()
        assert "Custom ccproxy.py file detected" in captured.err
        assert "will NOT be overwritten" in captured.err


class TestRunWithProxy:
    """Test suite for run_with_proxy function."""

    def test_run_no_config(self, tmp_path: Path, capsys) -> None:
        """Test run when config doesn't exist."""
        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "test"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Configuration not found" in captured.err
        assert "Run 'ccproxy install' first" in captured.err

    @patch("ccproxy.mitm.process.is_running")
    @patch("subprocess.run")
    def test_run_with_proxy_success(self, mock_run: Mock, mock_mitm_running: Mock, tmp_path: Path) -> None:
        """Test successful command execution with proxy environment."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("""
litellm:
  host: 192.168.1.1
  port: 8888
""")

        mock_run.return_value = Mock(returncode=0)
        mock_mitm_running.return_value = (False, None)

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "test"])

        assert exc_info.value.code == 0

        # Check environment variables were set
        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert env["OPENAI_API_BASE"] == "http://192.168.1.1:8888"
        assert env["ANTHROPIC_BASE_URL"] == "http://192.168.1.1:8888"

    @patch("ccproxy.mitm.process.is_running")
    @patch("subprocess.run")
    def test_run_with_env_override(self, mock_run: Mock, mock_mitm_running: Mock, tmp_path: Path) -> None:
        """Test run with environment variable overrides."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("""
litellm:
  host: 192.168.1.1
  port: 8888
""")

        mock_run.return_value = Mock(returncode=0)
        mock_mitm_running.return_value = (False, None)

        with (
            patch.dict(os.environ, {"HOST": "10.0.0.1", "PORT": "9999"}),
            pytest.raises(SystemExit),
        ):
            run_with_proxy(tmp_path, ["echo", "test"])

        # Check environment variables use env overrides
        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert env["OPENAI_API_BASE"] == "http://10.0.0.1:9999"

    @patch("ccproxy.mitm.process.is_running")
    @patch("subprocess.run")
    def test_run_with_mitm_running(self, mock_run: Mock, mock_mitm_running: Mock, tmp_path: Path) -> None:
        """Test run with mitmproxy running routes through mitm."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("""
litellm:
  host: 127.0.0.1
  port: 4000
ccproxy:
  mitm:
    port: 8081
""")

        mock_run.return_value = Mock(returncode=0)
        mock_mitm_running.return_value = (True, 12345)

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "test"])

        assert exc_info.value.code == 0

        # Check environment variables route through mitmproxy
        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert env["HTTPS_PROXY"] == "http://localhost:8081"
        assert env["HTTP_PROXY"] == "http://localhost:8081"
        assert env["OPENAI_API_BASE"] == "http://localhost:8081"
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8081"

    @patch("ccproxy.mitm.process.is_running")
    @patch("subprocess.run")
    def test_run_with_mitm_not_running(self, mock_run: Mock, mock_mitm_running: Mock, tmp_path: Path) -> None:
        """Test run with mitmproxy not running routes directly to LiteLLM."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("""
litellm:
  host: 127.0.0.1
  port: 4000
ccproxy:
  mitm:
    port: 8081
""")

        mock_run.return_value = Mock(returncode=0)
        mock_mitm_running.return_value = (False, None)

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "test"])

        assert exc_info.value.code == 0

        # Check environment variables route directly to LiteLLM
        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert env["OPENAI_API_BASE"] == "http://127.0.0.1:4000"
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
        # HTTP_PROXY should not be set when mitm is not running
        assert "HTTPS_PROXY" not in env or env.get("HTTPS_PROXY") == os.environ.get("HTTPS_PROXY")
        assert "HTTP_PROXY" not in env or env.get("HTTP_PROXY") == os.environ.get("HTTP_PROXY")

    @patch("subprocess.run")
    def test_run_command_not_found(self, mock_run: Mock, tmp_path: Path, capsys) -> None:
        """Test run with non-existent command."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("litellm: {}")

        mock_run.side_effect = FileNotFoundError()

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["nonexistent", "command"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Command not found: nonexistent" in captured.err

    @patch("subprocess.run")
    def test_run_command_keyboard_interrupt(self, mock_run: Mock, tmp_path: Path) -> None:
        """Test run with keyboard interrupt."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("litellm: {}")

        mock_run.side_effect = KeyboardInterrupt()

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "test"])

        assert exc_info.value.code == 130  # Standard exit code for Ctrl+C


class TestStopLiteLLM:
    """Test suite for stop_litellm function."""

    def test_stop_no_pid_file(self, tmp_path: Path, capsys) -> None:
        """Test stop when PID file doesn't exist."""
        result = stop_litellm(tmp_path)

        assert result is False
        captured = capsys.readouterr()
        assert "No LiteLLM server is running (PID file not found)" in captured.err

    @patch("os.kill")
    @patch("time.sleep")
    def test_stop_successful(self, mock_sleep: Mock, mock_kill: Mock, tmp_path: Path, capsys) -> None:
        """Test successful stop of running process."""
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("12345")

        # First call: check if running (returns None)
        # Second call: send SIGTERM (returns None)
        # Third call: check if still running (raises ProcessLookupError - stopped)
        mock_kill.side_effect = [None, None, ProcessLookupError()]

        result = stop_litellm(tmp_path)

        assert result is True
        assert not pid_file.exists()  # PID file should be removed

        captured = capsys.readouterr()
        assert "Stopping LiteLLM server (PID: 12345)" in captured.out
        assert "LiteLLM server stopped successfully (PID: 12345)" in captured.out

        # Verify kill calls
        assert mock_kill.call_count == 3
        mock_kill.assert_any_call(12345, 0)  # Check if running
        mock_kill.assert_any_call(12345, 15)  # SIGTERM

    @patch("os.kill")
    @patch("time.sleep")
    def test_stop_force_kill(self, mock_sleep: Mock, mock_kill: Mock, tmp_path: Path, capsys) -> None:
        """Test force kill when process doesn't respond to SIGTERM."""
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("12345")

        # Process keeps running after SIGTERM
        mock_kill.side_effect = [None, None, None, None]

        result = stop_litellm(tmp_path)

        assert result is True
        assert not pid_file.exists()

        captured = capsys.readouterr()
        assert "Force killed LiteLLM server (PID: 12345)" in captured.out

        # Verify kill calls
        assert mock_kill.call_count == 4
        mock_kill.assert_any_call(12345, 9)  # SIGKILL

    @patch("os.kill")
    def test_stop_stale_pid(self, mock_kill: Mock, tmp_path: Path, capsys) -> None:
        """Test stop with stale PID file."""
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("12345")

        # Process not running
        mock_kill.side_effect = ProcessLookupError()

        result = stop_litellm(tmp_path)

        assert result is False
        assert not pid_file.exists()  # Stale PID file should be removed

        captured = capsys.readouterr()
        assert "LiteLLM server was not running (stale PID: 12345)" in captured.out

    def test_stop_invalid_pid_file(self, tmp_path: Path, capsys) -> None:
        """Test stop with invalid PID file content."""
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("invalid-pid")

        result = stop_litellm(tmp_path)

        assert result is False
        captured = capsys.readouterr()
        assert "Error reading PID file" in captured.err


class TestViewLogs:
    """Test suite for view_logs function."""

    def test_logs_no_file(self, tmp_path: Path, capsys) -> None:
        """Test logs when log file doesn't exist."""
        with pytest.raises(SystemExit) as exc_info:
            view_logs(tmp_path)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No log file found" in captured.err
        assert str(tmp_path / "litellm.log") in captured.err

    @patch("subprocess.run")
    def test_logs_follow(self, mock_run: Mock, tmp_path: Path) -> None:
        """Test logs with follow option."""
        log_file = tmp_path / "litellm.log"
        log_file.write_text("log content")

        mock_run.return_value = Mock(returncode=0)

        with pytest.raises(SystemExit) as exc_info:
            view_logs(tmp_path, follow=True)

        assert exc_info.value.code == 0
        mock_run.assert_called_once_with(["tail", "-f", str(log_file)])

    @patch("subprocess.run")
    def test_logs_follow_keyboard_interrupt(self, mock_run: Mock, tmp_path: Path) -> None:
        """Test logs follow with keyboard interrupt."""
        log_file = tmp_path / "litellm.log"
        log_file.write_text("log content")

        mock_run.side_effect = KeyboardInterrupt()

        with pytest.raises(SystemExit) as exc_info:
            view_logs(tmp_path, follow=True)

        assert exc_info.value.code == 0

    def test_logs_empty_file(self, tmp_path: Path, capsys) -> None:
        """Test logs with empty log file."""
        log_file = tmp_path / "litellm.log"
        log_file.write_text("")

        with pytest.raises(SystemExit) as exc_info:
            view_logs(tmp_path)

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Log file is empty" in captured.out

    def test_logs_short_content(self, tmp_path: Path, capsys) -> None:
        """Test logs with short content (no pager)."""
        log_file = tmp_path / "litellm.log"
        content = "\n".join([f"Line {i}" for i in range(10)])
        log_file.write_text(content)

        with pytest.raises(SystemExit) as exc_info:
            view_logs(tmp_path, lines=20)

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Line 0" in captured.out
        assert "Line 9" in captured.out

    @patch("subprocess.Popen")
    def test_logs_long_content_with_pager(self, mock_popen: Mock, tmp_path: Path) -> None:
        """Test logs with long content (uses pager)."""
        log_file = tmp_path / "litellm.log"
        content = "\n".join([f"Line {i}" for i in range(30)])
        log_file.write_text(content)

        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"", b"")
        mock_popen.return_value = mock_process

        with pytest.raises(SystemExit) as exc_info:
            view_logs(tmp_path, lines=25)

        assert exc_info.value.code == 0
        mock_popen.assert_called_once()

        # Verify last 25 lines were passed to pager
        call_args = mock_process.communicate.call_args[0][0].decode()
        assert "Line 5" in call_args
        assert "Line 29" in call_args
        assert "Line 4" not in call_args

    @patch("subprocess.Popen")
    @patch.dict(os.environ, {"PAGER": "cat"})
    def test_logs_with_cat_pager(self, mock_popen: Mock, tmp_path: Path) -> None:
        """Test logs with cat as pager."""
        log_file = tmp_path / "litellm.log"
        content = "Some log content"
        log_file.write_text(content)

        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"", b"")
        mock_popen.return_value = mock_process

        with pytest.raises(SystemExit) as exc_info:
            view_logs(tmp_path)

        assert exc_info.value.code == 0
        mock_popen.assert_called_once_with(["cat"], stdin=subprocess.PIPE)


class TestShowStatus:
    """Test suite for show_status function."""

    @patch("os.kill")
    def test_status_json_proxy_running(self, mock_kill: Mock, tmp_path: Path, capsys) -> None:
        """Test status JSON output with proxy running."""
        # Create config files
        ccproxy_config = tmp_path / "ccproxy.yaml"
        ccproxy_config.write_text("litellm: {}")

        litellm_config = tmp_path / "config.yaml"
        litellm_config.write_text("""
litellm_settings:
  callbacks:
    - ccproxy.handler
    - langfuse
""")

        user_hooks = tmp_path / "ccproxy.py"
        user_hooks.write_text("# hooks")

        log_file = tmp_path / "litellm.log"
        log_file.write_text("log content")

        # Create PID file
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("12345")

        # Mock process is running
        mock_kill.return_value = None

        show_status(tmp_path, json_output=True)

        captured = capsys.readouterr()
        status = json.loads(captured.out)
        assert status["proxy"] is True
        assert status["config"]["ccproxy.yaml"] == str(ccproxy_config)
        assert status["config"]["config.yaml"] == str(litellm_config)
        assert status["config"]["ccproxy.py"] == str(user_hooks)
        assert status["callbacks"] == ["ccproxy.handler", "langfuse"]
        assert status["log"] == str(log_file)

    def test_status_json_proxy_stopped(self, tmp_path: Path, capsys) -> None:
        """Test status JSON output with proxy stopped."""
        # Create only config files
        ccproxy_config = tmp_path / "ccproxy.yaml"
        ccproxy_config.write_text("litellm: {}")

        litellm_config = tmp_path / "config.yaml"
        litellm_config.write_text("litellm_settings: {}")

        show_status(tmp_path, json_output=True)

        captured = capsys.readouterr()
        status = json.loads(captured.out)
        assert status["proxy"] is False
        assert status["config"]["ccproxy.yaml"] == str(ccproxy_config)
        assert status["config"]["config.yaml"] == str(litellm_config)
        assert "ccproxy.py" not in status["config"]
        assert status["callbacks"] == []
        assert status["log"] is None

    def test_status_json_no_config(self, tmp_path: Path, capsys) -> None:
        """Test status JSON output with no config files."""
        show_status(tmp_path, json_output=True)

        captured = capsys.readouterr()
        status = json.loads(captured.out)
        assert status["proxy"] is False
        assert status["config"] == {}
        assert status["callbacks"] == []
        assert status["log"] is None

    @patch("os.kill")
    def test_status_json_with_stale_pid(self, mock_kill: Mock, tmp_path: Path, capsys) -> None:
        """Test status JSON output with stale PID file."""
        # Create PID file
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("12345")

        # Mock process is not running
        mock_kill.side_effect = ProcessLookupError()

        show_status(tmp_path, json_output=True)

        captured = capsys.readouterr()
        status = json.loads(captured.out)
        assert status["proxy"] is False

    @patch("os.kill")
    def test_status_rich_output_proxy_running(self, mock_kill: Mock, tmp_path: Path, capsys) -> None:
        """Test status rich output with proxy running."""
        # Create config files
        ccproxy_config = tmp_path / "ccproxy.yaml"
        ccproxy_config.write_text("litellm: {}")

        litellm_config = tmp_path / "config.yaml"
        litellm_config.write_text("""
litellm_settings:
  callbacks:
    - ccproxy.handler
""")

        log_file = tmp_path / "litellm.log"
        log_file.write_text("log content")

        # Create PID file
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("12345")

        # Mock process is running
        mock_kill.return_value = None

        show_status(tmp_path, json_output=False)

        captured = capsys.readouterr()
        assert "ccproxy Status" in captured.out
        assert "proxy" in captured.out
        assert "true" in captured.out
        assert "config" in captured.out
        assert "ccproxy.yaml" in captured.out
        assert "callbacks" in captured.out
        assert "ccproxy.handler" in captured.out

    def test_status_rich_output_no_callbacks(self, tmp_path: Path, capsys) -> None:
        """Test status rich output with no callbacks configured."""
        litellm_config = tmp_path / "config.yaml"
        litellm_config.write_text("litellm_settings: {}")

        show_status(tmp_path, json_output=False)

        captured = capsys.readouterr()
        assert "No callbacks configured" in captured.out

    def test_status_rich_output_no_config(self, tmp_path: Path, capsys) -> None:
        """Test status rich output with no config files."""
        show_status(tmp_path, json_output=False)

        captured = capsys.readouterr()
        assert "No config files found" in captured.out


class TestMainFunction:
    """Test suite for main CLI function using Tyro."""

    @patch("ccproxy.cli.start_litellm")
    def test_main_litellm_command(self, mock_litellm: Mock, tmp_path: Path) -> None:
        """Test main with litellm command."""
        cmd = Start(args=["--debug", "--port", "8080"])
        main(cmd, config_dir=tmp_path)

        mock_litellm.assert_called_once_with(tmp_path, args=["--debug", "--port", "8080"], detach=False)

    @patch("ccproxy.cli.start_litellm")
    def test_main_litellm_no_args(self, mock_litellm: Mock, tmp_path: Path) -> None:
        """Test main with litellm command without args."""
        cmd = Start()
        main(cmd, config_dir=tmp_path)

        mock_litellm.assert_called_once_with(tmp_path, args=None, detach=False)

    @patch("ccproxy.cli.start_litellm")
    def test_main_litellm_detach(self, mock_litellm: Mock, tmp_path: Path) -> None:
        """Test main with litellm command in detach mode."""
        cmd = Start(detach=True)
        main(cmd, config_dir=tmp_path)

        mock_litellm.assert_called_once_with(tmp_path, args=None, detach=True)

    @patch("ccproxy.cli.install_config")
    def test_main_install_command(self, mock_install: Mock, tmp_path: Path) -> None:
        """Test main with install command."""
        cmd = Install(force=True)
        main(cmd, config_dir=tmp_path)

        mock_install.assert_called_once_with(tmp_path, force=True)

    @patch("ccproxy.cli.run_with_proxy")
    def test_main_run_command(self, mock_run: Mock, tmp_path: Path) -> None:
        """Test main with run command."""
        cmd = Run(command=["echo", "hello", "world"])
        main(cmd, config_dir=tmp_path)

        mock_run.assert_called_once_with(tmp_path, ["echo", "hello", "world"])

    def test_main_run_no_args(self, tmp_path: Path, capsys) -> None:
        """Test main run command without arguments."""
        cmd = Run(command=[])

        with pytest.raises(SystemExit) as exc_info:
            main(cmd, config_dir=tmp_path)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No command specified" in captured.err
        assert "Usage: ccproxy run <command>" in captured.err

    def test_main_default_config_dir(self, tmp_path: Path) -> None:
        """Test main uses default config directory when not specified."""
        with (
            patch.object(Path, "home", return_value=tmp_path),
            patch("ccproxy.cli.start_litellm") as mock_litellm,
        ):
            cmd = Start()
            main(cmd)

            # Check that litellm was called with the default config dir
            mock_litellm.assert_called_once_with(tmp_path / ".ccproxy", args=None, detach=False)

    @patch("ccproxy.cli.stop_litellm")
    def test_main_stop_command(self, mock_stop: Mock, tmp_path: Path) -> None:
        """Test main with stop command."""
        cmd = Stop()
        mock_stop.return_value = True  # Simulate successful stop

        with pytest.raises(SystemExit) as exc_info:
            main(cmd, config_dir=tmp_path)

        assert exc_info.value.code == 0
        mock_stop.assert_called_once_with(tmp_path)

    @patch("ccproxy.cli.view_logs")
    def test_main_logs_command(self, mock_logs: Mock, tmp_path: Path) -> None:
        """Test main with logs command."""
        cmd = Logs(follow=True, lines=50)
        main(cmd, config_dir=tmp_path)

        mock_logs.assert_called_once_with(tmp_path, follow=True, lines=50)

    @patch("ccproxy.cli.show_status")
    def test_main_status_command(self, mock_status: Mock, tmp_path: Path) -> None:
        """Test main with status command."""
        cmd = Status(json=False)
        main(cmd, config_dir=tmp_path)

        mock_status.assert_called_once_with(tmp_path, json_output=False)

    @patch("ccproxy.cli.show_status")
    def test_main_status_command_json(self, mock_status: Mock, tmp_path: Path) -> None:
        """Test main with status command with JSON output."""
        cmd = Status(json=True)
        main(cmd, config_dir=tmp_path)

        mock_status.assert_called_once_with(tmp_path, json_output=True)
