"""Tests for the ccproxy CLI."""

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from ccproxy.cli import (
    Init,
    Logs,
    Run,
    Start,
    Status,
    init_config,
    main,
    run_with_proxy,
    setup_logging,
    show_status,
    view_logs,
)
from ccproxy.config import clear_config_instance


class TestInitConfig:
    @patch("ccproxy.cli.get_templates_dir")
    def test_init_fresh(self, mock_get_templates: Mock, tmp_path: Path, capsys) -> None:
        """Test fresh initialization."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()

        # Only ccproxy.yaml is initialized; ccproxy.py is auto-generated on start
        (templates_dir / "ccproxy.yaml").write_text("test: config")

        mock_get_templates.return_value = templates_dir

        config_dir = tmp_path / "config"
        init_config(config_dir)

        assert (config_dir / "ccproxy.yaml").exists()

        captured = capsys.readouterr()
        assert "Configuration installed to:" in captured.out
        assert "Next steps:" in captured.out

    @patch("ccproxy.cli.get_templates_dir")
    def test_init_exists_no_force(self, mock_get_templates: Mock, tmp_path: Path, capsys) -> None:
        """Test init skips existing files without force and reports nothing to initialize."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "ccproxy.yaml").write_text("template content")

        mock_get_templates.return_value = templates_dir

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "ccproxy.yaml").write_text("existing content")

        init_config(config_dir, force=False)

        assert (config_dir / "ccproxy.yaml").read_text() == "existing content"
        captured = capsys.readouterr()
        assert "already exists" in captured.out
        assert "use --force" in captured.out
        assert "Nothing to install" in captured.out

    @patch("ccproxy.cli.get_templates_dir")
    def test_init_with_force(self, mock_get_templates: Mock, tmp_path: Path, capsys) -> None:
        """Test init with force overwrites existing files."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "ccproxy.yaml").write_text("new: config")

        mock_get_templates.return_value = templates_dir

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "ccproxy.yaml").write_text("old: config")

        init_config(config_dir, force=True)

        assert (config_dir / "ccproxy.yaml").read_text() == "new: config"
        captured = capsys.readouterr()
        assert "Installed ccproxy.yaml" in captured.out

    @patch("ccproxy.cli.get_templates_dir")
    def test_init_template_not_found(self, mock_get_templates: Mock, tmp_path: Path, capsys) -> None:
        """Test init when template file is missing."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        # No template files present

        mock_get_templates.return_value = templates_dir

        config_dir = tmp_path / "config"
        init_config(config_dir)

        captured = capsys.readouterr()
        assert "Warning: Template ccproxy.yaml not found" in captured.err

    def test_init_template_dir_error(self, tmp_path: Path) -> None:
        """Test init when get_templates_dir raises RuntimeError."""
        config_dir = tmp_path / "config"

        with patch("ccproxy.cli.get_templates_dir", side_effect=RuntimeError("Templates not found")):
            with pytest.raises(SystemExit) as exc_info:
                init_config(config_dir)
            assert exc_info.value.code == 1

    def test_init_skip_existing_file(self, tmp_path: Path, capsys) -> None:
        """Test init skips existing files without force flag."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "ccproxy.yaml").write_text("template content")

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "ccproxy.yaml").write_text("existing content")

        with patch("ccproxy.cli.get_templates_dir", return_value=templates_dir):
            init_config(config_dir)

        assert (config_dir / "ccproxy.yaml").read_text() == "existing content"
        captured = capsys.readouterr()
        assert "Skipping ccproxy.yaml" in captured.out
        assert "Nothing to install" in captured.out


class TestRunWithProxy:
    def test_run_no_config(self, tmp_path: Path, capsys) -> None:
        """Test run when config doesn't exist."""
        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "test"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Configuration not found" in captured.err
        assert "Run 'ccproxy init' first" in captured.err

    @patch("subprocess.run")
    def test_run_with_proxy_success(self, mock_run: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test successful command execution with proxy environment."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("""
ccproxy:
  host: 192.168.1.1
  port: 8888
""")

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("CCPROXY_PORT", raising=False)
        monkeypatch.delenv("CCPROXY_HOST", raising=False)
        clear_config_instance()
        mock_run.return_value = Mock(returncode=0)

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "test"])

        assert exc_info.value.code == 0

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert env["OPENAI_API_BASE"] == "http://192.168.1.1:8888"
        assert env["ANTHROPIC_BASE_URL"] == "http://192.168.1.1:8888"

    @patch("subprocess.run")
    def test_run_with_env_override(self, mock_run: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test run with environment variable overrides."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("""
ccproxy:
  host: 192.168.1.1
  port: 8888
""")

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("CCPROXY_HOST", "10.0.0.1")
        monkeypatch.setenv("CCPROXY_PORT", "9999")
        clear_config_instance()  # env vars already set above, clear stale singleton
        mock_run.return_value = Mock(returncode=0)

        with pytest.raises(SystemExit):
            run_with_proxy(tmp_path, ["echo", "test"])

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert env["OPENAI_API_BASE"] == "http://10.0.0.1:9999"

    @patch("subprocess.run")
    def test_run_with_inspect_running(self, mock_run: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test run with inspect - client still connects to main port (transparent proxy)."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("""
ccproxy:
  host: 127.0.0.1
  port: 4000
  inspector:
    port: 8081
""")

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("CCPROXY_PORT", raising=False)
        monkeypatch.delenv("CCPROXY_HOST", raising=False)
        clear_config_instance()
        mock_run.return_value = Mock(returncode=0)

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "test"])

        assert exc_info.value.code == 0

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert "HTTPS_PROXY" not in env or env.get("HTTPS_PROXY") == os.environ.get("HTTPS_PROXY")
        assert env["OPENAI_API_BASE"] == "http://127.0.0.1:4000"
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"

    @patch("subprocess.run")
    def test_run_with_inspect_not_running(self, mock_run: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test run without inspect routes directly to LiteLLM."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("""
ccproxy:
  host: 127.0.0.1
  port: 4000
  inspector:
    port: 8081
""")

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("CCPROXY_PORT", raising=False)
        monkeypatch.delenv("CCPROXY_HOST", raising=False)
        clear_config_instance()
        mock_run.return_value = Mock(returncode=0)

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "test"])

        assert exc_info.value.code == 0

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert env["OPENAI_API_BASE"] == "http://127.0.0.1:4000"
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
        # HTTP_PROXY should not be set when inspect is not requested
        assert "HTTPS_PROXY" not in env or env.get("HTTPS_PROXY") == os.environ.get("HTTPS_PROXY")
        assert "HTTP_PROXY" not in env or env.get("HTTP_PROXY") == os.environ.get("HTTP_PROXY")

    @patch("subprocess.run")
    def test_run_command_not_found(self, mock_run: Mock, tmp_path: Path, capsys, monkeypatch) -> None:
        """Test run with non-existent command."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("ccproxy: {}")

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()
        mock_run.side_effect = FileNotFoundError()

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["nonexistent", "command"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Command not found: nonexistent" in captured.err

    @patch("subprocess.run")
    def test_run_command_keyboard_interrupt(self, mock_run: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test run with keyboard interrupt."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("ccproxy: {}")

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()
        mock_run.side_effect = KeyboardInterrupt()

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "test"])

        assert exc_info.value.code == 130  # Standard exit code for Ctrl+C


class TestViewLogs:
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_logs_journalctl_when_service_active(self, mock_run: Mock, mock_which: Mock) -> None:
        """Test that logs delegates to journalctl when systemd service is active."""
        mock_which.return_value = "/usr/bin/systemctl"
        mock_run.side_effect = [
            Mock(stdout="active\n", returncode=0),
            Mock(returncode=0),
        ]

        with pytest.raises(SystemExit) as exc_info:
            view_logs()

        assert exc_info.value.code == 0
        journalctl_call = mock_run.call_args_list[1]
        assert "journalctl" in journalctl_call[0][0]
        assert "-u" in journalctl_call[0][0]
        assert "ccproxy.service" in journalctl_call[0][0]

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_logs_follow_passes_flag(self, mock_run: Mock, mock_which: Mock) -> None:
        """Test that follow flag is passed to journalctl."""
        mock_which.return_value = "/usr/bin/systemctl"
        mock_run.side_effect = [
            Mock(stdout="active\n", returncode=0),
            Mock(returncode=0),
        ]

        with pytest.raises(SystemExit):
            view_logs(follow=True)

        journalctl_call = mock_run.call_args_list[1]
        assert "-f" in journalctl_call[0][0]

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_logs_lines_passed_to_journalctl(self, mock_run: Mock, mock_which: Mock) -> None:
        """Test that lines count is passed to journalctl."""
        mock_which.return_value = "/usr/bin/systemctl"
        mock_run.side_effect = [
            Mock(stdout="active\n", returncode=0),
            Mock(returncode=0),
        ]

        with pytest.raises(SystemExit):
            view_logs(lines=50)

        journalctl_call = mock_run.call_args_list[1]
        cmd = journalctl_call[0][0]
        n_idx = cmd.index("-n")
        assert cmd[n_idx + 1] == "50"

    @patch("ccproxy.cli.Path")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_logs_process_compose_when_socket_present(self, mock_run: Mock, mock_which: Mock, mock_path: Mock) -> None:
        """Test that logs delegates to process-compose when socket exists."""
        mock_which.side_effect = lambda cmd: "/usr/bin/systemctl" if cmd == "systemctl" else "/usr/bin/process-compose"
        mock_run.side_effect = [
            Mock(stdout="inactive\n", returncode=3),
            Mock(returncode=0),
        ]
        mock_socket = Mock()
        mock_socket.exists.return_value = True
        mock_path.return_value = mock_socket

        with pytest.raises(SystemExit) as exc_info:
            view_logs()

        assert exc_info.value.code == 0
        pc_call = mock_run.call_args_list[1]
        assert "process-compose" in pc_call[0][0]

    @patch("shutil.which", return_value=None)
    def test_logs_exits_1_when_no_supervisor(self, mock_which: Mock, capsys) -> None:
        """Test that logs exits 1 when no supervisor is found."""
        with pytest.raises(SystemExit) as exc_info:
            view_logs()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No active ccproxy service found" in captured.err


class TestShowStatus:
    @patch("socket.create_connection")
    def test_status_json_proxy_running(self, mock_conn: Mock, tmp_path: Path, capsys, monkeypatch) -> None:
        """Test status JSON output with proxy running."""
        ccproxy_config = tmp_path / "ccproxy.yaml"
        ccproxy_config.write_text("""
ccproxy:
  host: 127.0.0.1
  port: 4000
""")

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()

        user_hooks = tmp_path / "ccproxy.py"
        user_hooks.write_text("# hooks")

        # Mock TCP probe: proxy is reachable
        mock_conn.return_value.__enter__ = Mock(return_value=Mock())
        mock_conn.return_value.__exit__ = Mock(return_value=False)

        show_status(tmp_path, json_output=True)

        captured = capsys.readouterr()
        status = json.loads(captured.out)
        assert status["proxy"] is True
        assert status["config"]["ccproxy.yaml"] == str(ccproxy_config)
        assert status["log"] is None

    @patch("socket.create_connection", side_effect=OSError)
    def test_status_json_proxy_stopped(self, mock_conn: Mock, tmp_path: Path, capsys, monkeypatch) -> None:
        """Test status JSON output with proxy stopped."""
        ccproxy_config = tmp_path / "ccproxy.yaml"
        ccproxy_config.write_text("""
ccproxy:
  host: 127.0.0.1
  port: 4000
""")

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()

        show_status(tmp_path, json_output=True)

        captured = capsys.readouterr()
        status = json.loads(captured.out)
        assert status["proxy"] is False
        assert status["config"]["ccproxy.yaml"] == str(ccproxy_config)

    @patch("socket.create_connection", side_effect=OSError)
    def test_status_json_no_config(self, mock_conn: Mock, tmp_path: Path, capsys, monkeypatch) -> None:
        """Test status JSON output with no config files."""
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()

        show_status(tmp_path, json_output=True)

        captured = capsys.readouterr()
        status = json.loads(captured.out)
        assert status["proxy"] is False
        assert status["config"] == {}
        assert status["log"] is None

    @patch("socket.create_connection", side_effect=OSError)
    def test_status_json_proxy_not_reachable(self, mock_conn: Mock, tmp_path: Path, capsys, monkeypatch) -> None:
        """Test status JSON output when proxy port is not reachable."""
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()

        show_status(tmp_path, json_output=True)

        captured = capsys.readouterr()
        status = json.loads(captured.out)
        assert status["proxy"] is False

    @patch("socket.create_connection")
    def test_status_rich_output_proxy_running(self, mock_conn: Mock, tmp_path: Path, capsys, monkeypatch) -> None:
        """Test status rich output with proxy running."""
        ccproxy_config = tmp_path / "ccproxy.yaml"
        ccproxy_config.write_text("""
ccproxy:
  host: 127.0.0.1
  port: 4000
  hooks:
    inbound:
      - ccproxy.hooks.forward_oauth
""")

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()

        log_file = tmp_path / "ccproxy.log"
        log_file.write_text("log content")

        # Mock TCP probe: proxy is reachable
        mock_conn.return_value.__enter__ = Mock(return_value=Mock())
        mock_conn.return_value.__exit__ = Mock(return_value=False)

        show_status(tmp_path, json_output=False)

        captured = capsys.readouterr()
        assert "ccproxy Status" in captured.out
        assert "proxy" in captured.out
        assert "true" in captured.out
        assert "config" in captured.out
        assert "ccproxy.yaml" in captured.out

    def test_status_rich_output_no_config(self, tmp_path: Path, capsys, monkeypatch) -> None:
        """Test status rich output with no config files."""
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()

        show_status(tmp_path, json_output=False)

        captured = capsys.readouterr()
        assert "No config files found" in captured.out


class TestMainFunction:
    @patch("ccproxy.cli.start_server")
    def test_main_start_command(self, mock_start: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test main with start command."""
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()
        cmd = Start()
        main(cmd, config_dir=tmp_path)

        mock_start.assert_called_once_with(tmp_path)

    @patch("ccproxy.cli.init_config")
    def test_main_init_command(self, mock_init: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test main with init command."""
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()
        cmd = Init(force=True)
        main(cmd, config_dir=tmp_path)

        mock_init.assert_called_once_with(tmp_path, force=True)

    @patch("ccproxy.cli.run_with_proxy")
    def test_main_run_command(self, mock_run: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test main with run command."""
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()
        cmd = Run(command=["echo", "hello", "world"])
        main(cmd, config_dir=tmp_path)

        mock_run.assert_called_once_with(tmp_path, ["echo", "hello", "world"], inspect=False)

    def test_main_run_no_args(self, tmp_path: Path, capsys, monkeypatch) -> None:
        """Test main run command without arguments shows help."""
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()
        cmd = Run(command=[])

        with pytest.raises(SystemExit) as exc_info:
            main(cmd, config_dir=tmp_path)

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "usage: ccproxy run" in captured.out

    def test_main_default_config_dir(self, tmp_path: Path) -> None:
        """Test main uses default config directory when not specified."""
        default_dir = tmp_path / ".ccproxy"
        default_dir.mkdir()
        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(Path, "home", return_value=tmp_path),
            patch("ccproxy.cli.start_server") as mock_start,
        ):
            os.environ.pop("CCPROXY_CONFIG_DIR", None)
            cmd = Start()
            main(cmd)

            mock_start.assert_called_once_with(default_dir)

    @patch("ccproxy.cli.view_logs")
    def test_main_logs_command(self, mock_logs: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test main with logs command."""
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()
        cmd = Logs(follow=True, lines=50)
        main(cmd, config_dir=tmp_path)

        mock_logs.assert_called_once_with(follow=True, lines=50, config_dir=tmp_path)

    @patch("ccproxy.cli.show_status")
    def test_main_status_command(self, mock_status: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test main with status command."""
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()
        cmd = Status(json_output=False)
        main(cmd, config_dir=tmp_path)

        mock_status.assert_called_once_with(tmp_path, json_output=False, check_proxy=False, check_inspect=False)

    @patch("ccproxy.cli.show_status")
    def test_main_status_command_json(self, mock_status: Mock, tmp_path: Path, monkeypatch) -> None:
        """Test main with status command with JSON output."""
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()
        cmd = Status(json_output=True)
        main(cmd, config_dir=tmp_path)

        mock_status.assert_called_once_with(tmp_path, json_output=True, check_proxy=False, check_inspect=False)


class TestSetupLogging:
    """Tests for setup_logging — stderr vs systemd journal handler routing."""

    def _root(self) -> logging.Logger:
        return logging.getLogger()

    def _reset_root(self) -> None:
        self._root().handlers.clear()
        self._root().setLevel(logging.WARNING)

    def test_stderr_handler_when_use_journal_false(self, tmp_path: Path) -> None:
        """Default path: StreamHandler pointed at sys.stderr."""
        try:
            setup_logging(tmp_path, log_level="INFO", log_file=None, use_journal=False)
            handlers = self._root().handlers
            assert len(handlers) == 1
            assert isinstance(handlers[0], logging.StreamHandler)
            assert handlers[0].stream is sys.stderr
        finally:
            self._reset_root()

    def test_file_handler_added_when_log_file_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """log_file=<path> adds a FileHandler alongside the stream handler."""
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        target = tmp_path / "ccproxy.log"
        try:
            log_path = setup_logging(
                tmp_path,
                log_level="INFO",
                log_file=target,
                use_journal=False,
            )
            assert log_path == target
            handler_types = {type(h).__name__ for h in self._root().handlers}
            assert "FileHandler" in handler_types
            assert "StreamHandler" in handler_types
        finally:
            self._reset_root()
            target.unlink(missing_ok=True)

    def test_journal_fallback_when_systemd_missing(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """use_journal=True falls back to stderr when systemd-python is unavailable.

        The test environment does not have systemd-python installed, so the
        import naturally raises ImportError and exercises the fallback branch.
        The warning is emitted via the logger (whose StreamHandler writes to
        sys.stderr), so capsys captures it.
        """
        try:
            setup_logging(tmp_path, log_level="INFO", log_file=None, use_journal=True)

            handlers = self._root().handlers
            assert len(handlers) == 1
            assert isinstance(handlers[0], logging.StreamHandler)
            assert handlers[0].stream is sys.stderr

            captured = capsys.readouterr()
            assert "use_journal requested but JournalHandler unavailable" in captured.err
            # Python raises ModuleNotFoundError (subclass of ImportError) for
            # missing top-level packages; the fallback message formats
            # `type(exc).__name__` so either name may appear.
            assert "ModuleNotFoundError" in captured.err or "ImportError" in captured.err
        finally:
            self._reset_root()

    def test_journal_handler_installed_when_systemd_available(self, tmp_path: Path) -> None:
        """use_journal=True installs JournalHandler when systemd.journal imports cleanly."""
        mock_handler = Mock(spec=logging.Handler)
        mock_handler.level = logging.NOTSET
        fake_journal_module = Mock()
        fake_journal_module.JournalHandler = Mock(return_value=mock_handler)
        fake_systemd_module = Mock()
        fake_systemd_module.journal = fake_journal_module

        try:
            with patch.dict(
                sys.modules,
                {"systemd": fake_systemd_module, "systemd.journal": fake_journal_module},
            ):
                setup_logging(tmp_path, log_level="INFO", log_file=None, use_journal=True)

            fake_journal_module.JournalHandler.assert_called_once_with(SYSLOG_IDENTIFIER="ccproxy")
            assert mock_handler in self._root().handlers
        finally:
            self._reset_root()

    def test_journal_fallback_when_journal_handler_raises(self, tmp_path: Path) -> None:
        """Runtime JournalHandler construction failures also fall back to stderr."""
        fake_journal_module = Mock()
        fake_journal_module.JournalHandler = Mock(side_effect=OSError("No /run/systemd/journal/socket"))
        fake_systemd_module = Mock()
        fake_systemd_module.journal = fake_journal_module

        try:
            with patch.dict(
                sys.modules,
                {"systemd": fake_systemd_module, "systemd.journal": fake_journal_module},
            ):
                setup_logging(tmp_path, log_level="INFO", log_file=None, use_journal=True)

            handlers = self._root().handlers
            assert len(handlers) == 1
            assert isinstance(handlers[0], logging.StreamHandler)
            assert handlers[0].stream is sys.stderr
        finally:
            self._reset_root()

    def test_verbose_false_floors_level_at_warning(self, tmp_path: Path) -> None:
        """verbose=False floors effective level at WARNING even if log_level=DEBUG."""
        try:
            setup_logging(
                tmp_path,
                log_level="DEBUG",
                log_file=None,
                use_journal=False,
                verbose=False,
            )
            assert self._root().level == logging.WARNING
        finally:
            self._reset_root()

    def test_verbose_false_preserves_higher_level(self, tmp_path: Path) -> None:
        """verbose=False doesn't lower a level that's already above WARNING."""
        try:
            setup_logging(
                tmp_path,
                log_level="ERROR",
                log_file=None,
                use_journal=False,
                verbose=False,
            )
            assert self._root().level == logging.ERROR
        finally:
            self._reset_root()

    def test_verbose_true_applies_log_level_directly(self, tmp_path: Path) -> None:
        """verbose=True applies log_level without flooring."""
        try:
            setup_logging(
                tmp_path,
                log_level="DEBUG",
                log_file=None,
                use_journal=False,
                verbose=True,
            )
            assert self._root().level == logging.DEBUG
        finally:
            self._reset_root()


class TestStatusPipeline:
    def test_status_renders_pipeline_panel_with_all_5_hooks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pipeline panel in show_status renders all 5 production hooks.

        Regression guard: the deleted dag-viz command had a hardcoded import list
        that omitted verbose_mode and apply_compliance. This test verifies that
        show_status via load_hooks + render_pipeline produces output containing
        every hook declared in the config.
        """
        import socket as _socket

        from ccproxy.config import clear_config_instance

        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text("""
ccproxy:
  host: 127.0.0.1
  port: 4001
  inspector:
    port: 8084
  hooks:
    inbound:
      - ccproxy.hooks.forward_oauth
      - ccproxy.hooks.extract_session_id
    outbound:
      - ccproxy.hooks.inject_mcp_notifications
      - ccproxy.hooks.verbose_mode
      - ccproxy.hooks.apply_compliance
""")

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        clear_config_instance()

        # Proxy and inspector are not running — socket probes must fail cleanly.
        monkeypatch.setattr(_socket, "create_connection", Mock(side_effect=OSError))

        show_status(tmp_path, json_output=False, check_proxy=False, check_inspect=False)

        captured = capsys.readouterr()
        out = captured.out

        assert "Pipeline" in out
        for hook_name in (
            "forward_oauth",
            "extract_session_id",
            "inject_mcp_notifications",
            "verbose_mode",
            "apply_compliance",
        ):
            assert hook_name in out, f"Expected hook '{hook_name}' in status output"
        assert "lightllm transform" in out
        assert "provider API" in out
