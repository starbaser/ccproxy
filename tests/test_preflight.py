"""Tests for pre-flight startup checks."""

import os
import signal
import socket
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.preflight import (
    _is_ccproxy_process,
    find_ccproxy_processes,
    get_port_pid,
    kill_stale_processes,
    run_preflight_checks,
)

# ---------------------------------------------------------------------------
# _is_ccproxy_process
# ---------------------------------------------------------------------------


class TestIsCcproxyProcess:
    def test_litellm_with_config(self):
        cmdline = "/usr/bin/python /usr/bin/litellm --config /home/user/.ccproxy/config.yaml --port 4000"
        assert _is_ccproxy_process(cmdline) is True

    def test_mitmdump_with_script(self):
        cmdline = "/usr/bin/mitmdump --listen-port 4000 -s /home/user/ccproxy/mitm/script.py"
        assert _is_ccproxy_process(cmdline) is True

    def test_unrelated_litellm(self):
        cmdline = "/usr/bin/python /usr/bin/litellm --config /etc/litellm/config.yaml"
        assert _is_ccproxy_process(cmdline) is False

    def test_unrelated_process(self):
        cmdline = "/usr/bin/nginx -g daemon off;"
        assert _is_ccproxy_process(cmdline) is False

    def test_empty(self):
        assert _is_ccproxy_process("") is False


# ---------------------------------------------------------------------------
# get_port_pid
# ---------------------------------------------------------------------------


class TestGetPortPid:
    def test_free_port(self):
        """A truly free port should return (None, None)."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        # Port is now unbound
        pid, name = get_port_pid(free_port)
        assert pid is None
        assert name is None

    def test_occupied_port(self):
        """A bound+listening port should be detected as occupied."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            pid, _ = get_port_pid(port)
            assert pid is not None
            # Should resolve to our own PID
            if pid != -1:
                assert pid == os.getpid()
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# find_ccproxy_processes
# ---------------------------------------------------------------------------


class TestFindCcproxyProcesses:
    @patch("ccproxy.preflight._read_proc_cmdline")
    @patch("pathlib.Path.iterdir")
    def test_finds_litellm(self, mock_iterdir, mock_cmdline):
        proc_dir = MagicMock()
        proc_dir.name = "9999"
        proc_dir.is_dir.return_value = True
        mock_iterdir.return_value = [proc_dir]
        mock_cmdline.return_value = "/usr/bin/python /usr/bin/litellm --config /home/user/.ccproxy/config.yaml"

        results = find_ccproxy_processes(exclude_pid=os.getpid())
        assert len(results) == 1
        assert results[0][0] == 9999

    @patch("ccproxy.preflight._read_proc_cmdline")
    @patch("pathlib.Path.iterdir")
    def test_excludes_own_pid(self, mock_iterdir, mock_cmdline):
        own = MagicMock()
        own.name = str(os.getpid())
        own.is_dir.return_value = True
        mock_iterdir.return_value = [own]
        mock_cmdline.return_value = "/usr/bin/litellm --config /home/user/.ccproxy/config.yaml"

        results = find_ccproxy_processes(exclude_pid=os.getpid())
        assert results == []

    @patch("ccproxy.preflight._read_proc_cmdline")
    @patch("pathlib.Path.iterdir")
    def test_skips_non_ccproxy(self, mock_iterdir, mock_cmdline):
        proc_dir = MagicMock()
        proc_dir.name = "5555"
        proc_dir.is_dir.return_value = True
        mock_iterdir.return_value = [proc_dir]
        mock_cmdline.return_value = "/usr/bin/nginx"

        results = find_ccproxy_processes(exclude_pid=os.getpid())
        assert results == []


# ---------------------------------------------------------------------------
# kill_stale_processes
# ---------------------------------------------------------------------------


class TestKillStaleProcesses:
    @patch("os.kill")
    def test_kills_process(self, mock_kill):
        # SIGTERM succeeds, then process is gone on check
        mock_kill.side_effect = [None, ProcessLookupError]
        count = kill_stale_processes([(1234, "litellm .ccproxy/config.yaml")])
        assert count == 1
        mock_kill.assert_any_call(1234, signal.SIGTERM)

    @patch("os.kill")
    def test_already_dead(self, mock_kill):
        mock_kill.side_effect = ProcessLookupError
        count = kill_stale_processes([(1234, "litellm .ccproxy/config.yaml")])
        assert count == 1

    @patch("os.kill")
    def test_permission_denied(self, mock_kill):
        mock_kill.side_effect = PermissionError
        count = kill_stale_processes([(1234, "litellm .ccproxy/config.yaml")])
        assert count == 0


# ---------------------------------------------------------------------------
# run_preflight_checks
# ---------------------------------------------------------------------------


class TestRunPreflightChecks:
    def test_clean_system(self, tmp_path):
        """No conflicts — should pass without error."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        with patch("ccproxy.preflight.find_ccproxy_processes", return_value=[]):
            run_preflight_checks(tmp_path, ports=[free_port])

    def test_already_running_via_pidfile(self, tmp_path):
        """PID file with alive process → SystemExit."""
        from ccproxy.process import write_pid

        pid_file = tmp_path / "litellm.lock"
        write_pid(pid_file, os.getpid())

        with pytest.raises(SystemExit):
            run_preflight_checks(tmp_path, ports=[])

    def test_stale_pidfile_cleaned(self, tmp_path):
        """PID file with dead process should be cleaned, not block start."""
        pid_file = tmp_path / "litellm.lock"
        pid_file.write_text("999999999")  # Unlikely to be alive

        with patch("ccproxy.preflight.find_ccproxy_processes", return_value=[]):
            # Should NOT raise — stale PID file gets cleaned by is_process_running
            run_preflight_checks(tmp_path, ports=[])

    def test_port_occupied_by_foreign_process(self, tmp_path):
        """Port held by non-ccproxy process → SystemExit."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        try:
            with (
                patch("ccproxy.preflight.find_ccproxy_processes", return_value=[]),
                pytest.raises(SystemExit),
            ):
                run_preflight_checks(tmp_path, ports=[port])
        finally:
            srv.close()

    def test_orphan_killed_then_port_freed(self, tmp_path):
        """Orphaned ccproxy process on port → killed, startup proceeds."""
        fake_cmdline = "/usr/bin/litellm --config /home/user/.ccproxy/config.yaml"

        with (
            patch("ccproxy.preflight.find_ccproxy_processes", return_value=[]),
            patch(
                "ccproxy.preflight.get_port_pid",
                side_effect=[(42, fake_cmdline[:80]), (None, None)],
            ),
            patch("ccproxy.preflight._read_proc_cmdline", return_value=fake_cmdline),
            patch("ccproxy.preflight.kill_stale_processes", return_value=1),
        ):
            run_preflight_checks(tmp_path, ports=[4000])

    def test_mitm_checks_both_ports(self, tmp_path):
        """When mitm=True the caller passes both main_port and forward_port."""
        with (
            patch("ccproxy.preflight.find_ccproxy_processes", return_value=[]),
            patch("ccproxy.preflight.get_port_pid", return_value=(None, None)) as mock_gpp,
        ):
            run_preflight_checks(tmp_path, ports=[4000, 8081])
            # Should check both ports
            assert mock_gpp.call_count == 2
            mock_gpp.assert_any_call(4000)
            mock_gpp.assert_any_call(8081)

    def test_no_mitm_checks_main_port_only(self, tmp_path):
        """When mitm=False the caller passes only main_port."""
        with (
            patch("ccproxy.preflight.find_ccproxy_processes", return_value=[]),
            patch("ccproxy.preflight.get_port_pid", return_value=(None, None)) as mock_gpp,
        ):
            run_preflight_checks(tmp_path, ports=[4000])
            assert mock_gpp.call_count == 1
            mock_gpp.assert_called_with(4000)
