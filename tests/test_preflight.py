"""Tests for pre-flight startup checks."""

import os
import signal
import socket
from unittest.mock import MagicMock, mock_open, patch

import pytest

from ccproxy.preflight import (
    _cleanup_stale_wireguard_confs,
    _find_inode_pids,
    _is_ccproxy_process,
    _is_udp_port_in_use,
    _read_proc_cmdline,
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
        """_CCPROXY_PATTERNS is empty — no cmdline matches."""
        cmdline = "/usr/bin/python /usr/bin/litellm --config /home/user/.ccproxy/config.yaml --port 4000"
        assert _is_ccproxy_process(cmdline) is False

    def test_mitmweb_not_detected(self):
        """mitmweb is an in-process addon, not a detectable subprocess."""
        cmdline = "/usr/bin/mitmweb --listen-port 4000 -s /home/user/ccproxy/inspector/script.py"
        assert _is_ccproxy_process(cmdline) is False

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
        """_CCPROXY_PATTERNS is empty — no process matches regardless of cmdline."""
        proc_dir = MagicMock()
        proc_dir.name = "9999"
        proc_dir.is_dir.return_value = True
        mock_iterdir.return_value = [proc_dir]
        mock_cmdline.return_value = "/usr/bin/python /usr/bin/litellm --config /home/user/.ccproxy/config.yaml"

        results = find_ccproxy_processes(exclude_pid=os.getpid())
        assert results == []

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

        run_preflight_checks(ports=[free_port])

    def test_port_occupied_by_foreign_process(self, tmp_path):
        """Port held by non-ccproxy process → SystemExit."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        try:
            with pytest.raises(SystemExit):
                run_preflight_checks(ports=[port])
        finally:
            srv.close()

    def test_orphan_killed_then_port_freed(self, tmp_path):
        """Port held by any process → SystemExit (no pattern matches, so no auto-kill)."""
        fake_cmdline = "/usr/bin/litellm --config /home/user/.ccproxy/config.yaml"

        with (
            patch("ccproxy.preflight.get_port_pid", return_value=(42, fake_cmdline[:80])),
            patch("ccproxy.preflight._read_proc_cmdline", return_value=fake_cmdline),
            pytest.raises(SystemExit),
        ):
            run_preflight_checks(ports=[4000])

    def test_mitm_checks_both_ports(self, tmp_path):
        """When inspect=True the caller passes both main_port and forward_port."""
        with patch("ccproxy.preflight.get_port_pid", return_value=(None, None)) as mock_gpp:
            run_preflight_checks(ports=[4000, 8081])
            assert mock_gpp.call_count == 2
            mock_gpp.assert_any_call(4000)
            mock_gpp.assert_any_call(8081)

    def test_no_mitm_checks_main_port_only(self, tmp_path):
        """When inspect=False the caller passes only main_port."""
        with patch("ccproxy.preflight.get_port_pid", return_value=(None, None)) as mock_gpp:
            run_preflight_checks(ports=[4000])
            assert mock_gpp.call_count == 1
            mock_gpp.assert_called_with(4000)

    def test_does_not_kill_other_instance_processes(self, tmp_path):
        """Processes on ports NOT in our config are left alone."""
        other_cmdline = "/usr/bin/litellm --config /home/user/project/.ccproxy/config.yaml"

        with (
            patch("ccproxy.preflight.get_port_pid", return_value=(None, None)),
            patch("ccproxy.preflight.find_ccproxy_processes", return_value=[(999, other_cmdline)]) as mock_find,
            patch("ccproxy.preflight.kill_stale_processes") as mock_kill,
        ):
            run_preflight_checks(ports=[4000])
            # find_ccproxy_processes should NOT be called during preflight
            mock_find.assert_not_called()
            mock_kill.assert_not_called()

    def test_port_occupied_unknown_pid(self):
        """Port returns pid=-1 (can't identify) → SystemExit."""
        with patch("ccproxy.preflight.get_port_pid", return_value=(-1, "unknown")), pytest.raises(SystemExit):
            run_preflight_checks(ports=[4000])

    def test_orphan_killed_but_port_still_occupied(self):
        """Port held by any process → SystemExit (no pattern matches, so no auto-kill)."""
        fake_cmdline = "/usr/bin/litellm --config /home/user/.ccproxy/config.yaml"
        with (
            patch("ccproxy.preflight.get_port_pid", return_value=(42, fake_cmdline)),
            patch("ccproxy.preflight._read_proc_cmdline", return_value=fake_cmdline),
            pytest.raises(SystemExit),
        ):
            run_preflight_checks(ports=[4000])

    def test_udp_port_free(self):
        with patch("ccproxy.preflight._is_udp_port_in_use", return_value=None):
            run_preflight_checks(udp_ports=[51820])

    def test_udp_port_occupied_unknown(self):
        with patch("ccproxy.preflight._is_udp_port_in_use", return_value=-1), pytest.raises(SystemExit):
            run_preflight_checks(udp_ports=[51820])

    def test_udp_port_occupied_by_process(self):
        with (
            patch("ccproxy.preflight._is_udp_port_in_use", return_value=1234),
            patch("ccproxy.preflight._read_proc_cmdline", return_value="wg"),
            pytest.raises(SystemExit),
        ):
            run_preflight_checks(udp_ports=[51820])

    def test_config_dir_triggers_wg_cleanup(self, tmp_path):
        with patch("ccproxy.preflight._cleanup_stale_wireguard_confs") as mock_cleanup:
            run_preflight_checks(config_dir=tmp_path)
            mock_cleanup.assert_called_once_with(tmp_path)


# ---------------------------------------------------------------------------
# _read_proc_cmdline
# ---------------------------------------------------------------------------


class TestGetPortPidExtra:
    def test_host_0000_sets_exclusive_listen_addrs(self):
        """host='0.0.0.0' path executes."""
        _pid, _ = get_port_pid(59998, host="0.0.0.0")
        # Just verify it runs without error — port is likely free

    def test_inode_found_but_no_pid_resolution(self):
        """When inode resolves but PID not found → returns -1, 'unknown'."""
        tcp_line = (
            "0:  00000000:EA5E 00000000:0000 0A 00000000:00000000"
            " 00:00000000 00000000   999        0 99999999 1 0000000000000000 100 0 0 10 0\n"
        )
        with (
            patch("pathlib.Path.open", mock_open(read_data=tcp_line)),
            patch("ccproxy.preflight._find_inode_pids", return_value={}),
        ):
            pid, _ = get_port_pid(59998)
            assert pid == -1

    def test_tcp_oserror_continues(self):
        """OSError on /proc/net/tcp is handled gracefully."""
        with (
            patch("pathlib.Path.open", side_effect=OSError("no file")),
            patch("socket.socket") as mock_sock_cls,
        ):
            mock_sock = MagicMock()
            mock_sock.__enter__ = lambda s: s
            mock_sock.__exit__ = MagicMock(return_value=False)
            mock_sock.bind.return_value = None
            mock_sock_cls.return_value = mock_sock
            pid, _ = get_port_pid(59998)
            assert pid is None

    def test_tcp6_v4mapped_address_match(self):
        """TCP6 with v4-mapped loopback address is detected."""
        # Port EA5E = 59998 decimal
        tcp6_line = (
            "0:  00000000000000000000FFFF0100007F:EA5E 00000000000000000000000000000000:0000"
            " 0A 00000000:00000000 00:00000000 00000000   999        0 11111111 1 0000000000000000 100 0 0 10 0\n"
        )
        header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"

        def fake_open(self, *args, **kwargs):
            if "tcp6" in str(self):
                from io import StringIO

                return StringIO(header + tcp6_line)
            raise OSError("no tcp")

        with (
            patch("pathlib.Path.open", fake_open),
            patch("ccproxy.preflight._find_inode_pids", return_value={11111111: 12345}),
            patch("ccproxy.preflight._read_proc_cmdline", return_value="some process"),
        ):
            pid, _ = get_port_pid(59998)
            assert pid == 12345

    def test_short_tcp_line_skipped(self):
        """Short lines in /proc/net/tcp are skipped."""
        short_line = "too short\n"
        header = "  sl  local_address\n"

        def fake_open(self, *args, **kwargs):
            if "tcp6" in str(self):
                raise OSError("no tcp6")
            from io import StringIO

            return StringIO(header + short_line)

        with (
            patch("pathlib.Path.open", fake_open),
            patch("socket.socket") as mock_sock_cls,
        ):
            mock_sock = MagicMock()
            mock_sock.__enter__ = lambda s: s
            mock_sock.__exit__ = MagicMock(return_value=False)
            mock_sock.bind.return_value = None
            mock_sock_cls.return_value = mock_sock
            pid, _ = get_port_pid(59998)
            assert pid is None

    def test_socket_bind_fails_returns_neg1(self):
        """When /proc not available and socket bind fails → -1, 'unknown'."""
        with (
            patch("pathlib.Path.open", side_effect=OSError("no file")),
            patch("socket.socket") as mock_sock_cls,
        ):
            mock_sock = MagicMock()
            mock_sock.__enter__ = lambda s: s
            mock_sock.__exit__ = MagicMock(return_value=False)
            mock_sock.bind.side_effect = OSError("in use")
            mock_sock_cls.return_value = mock_sock
            pid, _ = get_port_pid(59998)
            assert pid == -1


class TestFindCcproxyProcessesExtra:
    def test_oserror_on_proc_scan(self):
        """OSError during /proc scan is handled gracefully."""
        with patch("pathlib.Path.iterdir", side_effect=OSError("no /proc")):
            result = find_ccproxy_processes()
            assert result == []

    def test_skips_non_digit_entries(self):
        """Non-digit entries in /proc are ignored."""
        non_digit = MagicMock()
        non_digit.name = "net"
        with patch("pathlib.Path.iterdir", return_value=[non_digit]):
            result = find_ccproxy_processes()
            assert result == []


class TestReadProcCmdline:
    def test_reads_real_self(self):
        """Should successfully read our own cmdline."""
        result = _read_proc_cmdline(os.getpid())
        assert result is not None
        assert len(result) > 0

    def test_nonexistent_pid_returns_none(self):
        result = _read_proc_cmdline(9999999)
        assert result is None


# ---------------------------------------------------------------------------
# _find_inode_pids
# ---------------------------------------------------------------------------


class TestFindInodePids:
    def test_returns_dict(self):
        result = _find_inode_pids()
        assert isinstance(result, dict)

    def test_handles_oserror_on_iterdir(self):
        with patch("pathlib.Path.iterdir", side_effect=OSError("no /proc")):
            result = _find_inode_pids()
            assert result == {}


# ---------------------------------------------------------------------------
# _is_udp_port_in_use
# ---------------------------------------------------------------------------


class TestIsUdpPortInUse:
    def test_free_port_returns_none(self):
        # A port that is definitely not bound
        result = _is_udp_port_in_use(59999)
        assert result is None

    def test_returns_none_on_oserror(self):
        with patch("pathlib.Path.open", side_effect=OSError("no file")):
            result = _is_udp_port_in_use(51820)
            assert result is None

    def test_detects_bound_udp_port(self):
        """Bind a UDP socket and verify detection."""
        import socket as sock_mod

        with sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            result = _is_udp_port_in_use(port)
            # May return a pid or -1 depending on /proc resolution
            assert result is not None

    def test_udp_short_line_skipped(self):
        """Short lines in /proc/net/udp are skipped."""

        def fake_open(self, *args, **kwargs):
            from io import StringIO

            return StringIO("too short\n")

        with patch("pathlib.Path.open", fake_open):
            result = _is_udp_port_in_use(59997)
            assert result is None

    def test_udp_inode_no_pid_returns_neg1(self):
        """Inode found in UDP but no PID mapping → -1."""
        # Port EA5D = 59997 decimal
        udp_line = (
            "0:  0100007F:EA5D 00000000:0000 07 00000000:00000000"
            " 00:00000000 00000000   999        0 88888888 2 0000000000000000\n"
        )

        def fake_open(self, *args, **kwargs):
            from io import StringIO

            return StringIO(udp_line)

        with (
            patch("pathlib.Path.open", fake_open),
            patch("ccproxy.preflight._find_inode_pids", return_value={}),
        ):
            result = _is_udp_port_in_use(59997)
            assert result == -1


# ---------------------------------------------------------------------------
# _cleanup_stale_wireguard_confs
# ---------------------------------------------------------------------------


class TestCleanupStaleWireguardConfs:
    def test_removes_dead_pid_conf(self, tmp_path):
        # PID 9999999 should not exist
        wg_file = tmp_path / "wireguard.9999999.conf"
        wg_file.write_text('{"private_key": "fake"}')
        _cleanup_stale_wireguard_confs(tmp_path)
        assert not wg_file.exists()

    def test_keeps_live_pid_conf(self, tmp_path):
        wg_file = tmp_path / f"wireguard.{os.getpid()}.conf"
        wg_file.write_text('{"private_key": "fake"}')
        _cleanup_stale_wireguard_confs(tmp_path)
        assert wg_file.exists()

    def test_ignores_non_wg_files(self, tmp_path):
        other = tmp_path / "config.yaml"
        other.write_text("key: value")
        _cleanup_stale_wireguard_confs(tmp_path)
        assert other.exists()

    def test_empty_dir_is_noop(self, tmp_path):
        _cleanup_stale_wireguard_confs(tmp_path)


# ---------------------------------------------------------------------------
# kill_stale_processes extra paths
# ---------------------------------------------------------------------------


class TestKillStaleProcessesExtra:
    @patch("os.kill")
    @patch("time.sleep")
    def test_sends_sigkill_when_still_alive(self, mock_sleep, mock_kill):
        """If process is still alive after SIGTERM, sends SIGKILL."""
        # First kill (SIGTERM) succeeds, second (check with 0) succeeds (still alive),
        # third (SIGKILL) succeeds
        mock_kill.side_effect = [None, None, None]
        count = kill_stale_processes([(1234, "litellm .ccproxy/config.yaml")])
        assert count == 1
        calls = [c[0][1] for c in mock_kill.call_args_list]
        assert signal.SIGTERM in calls
        assert signal.SIGKILL in calls

    @patch("os.kill")
    @patch("time.sleep")
    def test_oserror_logs_error(self, mock_sleep, mock_kill):
        mock_kill.side_effect = OSError("unexpected")
        count = kill_stale_processes([(1234, "litellm .ccproxy/config.yaml")])
        assert count == 0

    @patch("os.kill")
    @patch("time.sleep")
    def test_long_cmdline_snippet(self, mock_sleep, mock_kill):
        mock_kill.side_effect = ProcessLookupError
        long_cmd = "x" * 200
        count = kill_stale_processes([(1234, long_cmd)])
        assert count == 1
