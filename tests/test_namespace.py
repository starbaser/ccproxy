"""Tests for ccproxy.inspector.namespace — network namespace confinement."""

import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, mock_open, patch

import pytest

from ccproxy.inspector.namespace import (
    NamespaceContext,
    _rewrite_wg_endpoint,
    _safe_close,
    _safe_kill,
    check_namespace_capabilities,
    cleanup_namespace,
    create_namespace,
    run_in_namespace,
)

# --- Fixtures ---

SAMPLE_WG_CLIENT_CONF = """\
[Interface]
PrivateKey = kHs2qYLCZkKnfuHxfCxPiKFBRqBBPgFBPQMOaTbBnWs=
Address = 10.0.0.1/32
DNS = 10.0.0.53

[Peer]
PublicKey = 7ZFGqZrmMvBD3tE6a0l3iILmZ2kkM1AGWP+KnpSXUQ0=
AllowedIPs = 0.0.0.0/0
Endpoint = 192.168.1.100:51820
"""


@pytest.fixture
def mock_ctx(tmp_path: Path) -> NamespaceContext:
    """A NamespaceContext with mock resources for cleanup tests."""
    conf_path = tmp_path / "wg-client.conf"
    conf_path.write_text("test")
    return NamespaceContext(
        ns_pid=99999,
        slirp_proc=MagicMock(spec=subprocess.Popen),
        exit_w=999,
        wg_conf_path=conf_path,
        api_socket=None,
    )


# =============================================================================
# check_namespace_capabilities — prerequisite validation
# =============================================================================


class TestCheckNamespaceCapabilities:
    """Verify that all jail prerequisites are validated before allowing execution."""

    @patch("shutil.which")
    def test_all_tools_present(self, mock_which: Mock, tmp_path: Path) -> None:
        """All tools found and userns enabled → empty problem list."""
        mock_which.return_value = "/usr/bin/tool"
        with patch.object(Path, "exists", return_value=False):
            # /proc/sys/kernel/unprivileged_userns_clone doesn't exist (some kernels)
            problems = check_namespace_capabilities()
        assert problems == []

    @patch("shutil.which")
    def test_userns_disabled(self, mock_which: Mock) -> None:
        """Unprivileged user namespaces disabled → reported as problem."""
        mock_which.return_value = "/usr/bin/tool"

        with patch("ccproxy.inspector.namespace.Path") as mock_path_cls:
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.read_text.return_value = "0\n"
            mock_path_cls.return_value = mock_path_instance

            problems = check_namespace_capabilities()

        assert len(problems) == 1
        assert "unprivileged_userns_clone=0" in problems[0].lower()

    @patch("shutil.which")
    def test_userns_enabled(self, mock_which: Mock) -> None:
        """Unprivileged user namespaces enabled → no problem for userns."""
        mock_which.return_value = "/usr/bin/tool"

        with patch("ccproxy.inspector.namespace.Path") as mock_path_cls:
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.read_text.return_value = "1\n"
            mock_path_cls.return_value = mock_path_instance

            problems = check_namespace_capabilities()

        assert problems == []

    @patch("shutil.which")
    def test_missing_single_tool(self, mock_which: Mock) -> None:
        """One missing tool → exactly one problem reported."""

        def which_side_effect(name: str) -> str | None:
            if name == "slirp4netns":
                return None
            return f"/usr/bin/{name}"

        mock_which.side_effect = which_side_effect

        with patch("ccproxy.inspector.namespace.Path") as mock_path_cls:
            mock_path_cls.return_value.exists.return_value = False
            problems = check_namespace_capabilities()

        assert len(problems) == 1
        assert "slirp4netns" in problems[0]

    @patch("shutil.which", return_value=None)
    def test_all_tools_missing(self, mock_which: Mock) -> None:
        """All tools missing → one problem per tool."""
        with patch("ccproxy.inspector.namespace.Path") as mock_path_cls:
            mock_path_cls.return_value.exists.return_value = False
            problems = check_namespace_capabilities()

        # 5 tools: slirp4netns, unshare, nsenter, ip, wg
        assert len(problems) == 5
        tool_names = {"slirp4netns", "unshare", "nsenter", "ip", "wg"}
        for problem in problems:
            assert any(tool in problem for tool in tool_names)

    @patch("shutil.which", return_value=None)
    def test_userns_disabled_plus_missing_tools(self, mock_which: Mock) -> None:
        """Both userns disabled AND tools missing → all problems reported."""
        with patch("ccproxy.inspector.namespace.Path") as mock_path_cls:
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.read_text.return_value = "0\n"
            mock_path_cls.return_value = mock_path_instance

            problems = check_namespace_capabilities()

        # 1 userns + 5 tools = 6 problems
        assert len(problems) == 6

    @patch("shutil.which", return_value="/usr/bin/tool")
    def test_userns_file_unreadable(self, mock_which: Mock) -> None:
        """OSError reading userns sysctl → silently ignored (not a problem)."""
        with patch("ccproxy.inspector.namespace.Path") as mock_path_cls:
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.read_text.side_effect = OSError("permission denied")
            mock_path_cls.return_value = mock_path_instance

            problems = check_namespace_capabilities()

        assert problems == []

    @patch("shutil.which")
    def test_each_tool_checked_independently(self, mock_which: Mock) -> None:
        """Missing ip and wg but others present → exactly 2 problems."""
        missing = {"ip", "wg"}

        def which_side_effect(name: str) -> str | None:
            return None if name in missing else f"/usr/bin/{name}"

        mock_which.side_effect = which_side_effect

        with patch("ccproxy.inspector.namespace.Path") as mock_path_cls:
            mock_path_cls.return_value.exists.return_value = False
            problems = check_namespace_capabilities()

        assert len(problems) == 2
        assert any("ip" in p for p in problems)
        assert any("wg" in p for p in problems)

    @patch("shutil.which")
    def test_install_hints_included(self, mock_which: Mock) -> None:
        """Each problem includes a nix install hint."""

        def which_side_effect(name: str) -> str | None:
            return None if name == "wg" else f"/usr/bin/{name}"

        mock_which.side_effect = which_side_effect

        with patch("ccproxy.inspector.namespace.Path") as mock_path_cls:
            mock_path_cls.return_value.exists.return_value = False
            problems = check_namespace_capabilities()

        assert len(problems) == 1
        assert "nix profile install" in problems[0]
        assert "wireguard-tools" in problems[0]


# =============================================================================
# _rewrite_wg_endpoint — WireGuard config rewriting
# =============================================================================


class TestRewriteWgEndpoint:
    """Verify WireGuard client config endpoint rewriting for namespace routing."""

    def test_rewrites_endpoint(self) -> None:
        """Standard endpoint is replaced with the slirp4netns gateway, port preserved from config."""
        result = _rewrite_wg_endpoint(SAMPLE_WG_CLIENT_CONF, "10.0.2.2")
        assert "Endpoint = 10.0.2.2:51820" in result
        assert "192.168.1.100" not in result

    def test_preserves_other_fields(self) -> None:
        """Non-Endpoint, non-wg-quick fields are preserved exactly."""
        result = _rewrite_wg_endpoint(SAMPLE_WG_CLIENT_CONF, "10.0.2.2")
        assert "PrivateKey = kHs2qYLCZkKnfuHxfCxPiKFBRqBBPgFBPQMOaTbBnWs=" in result
        assert "AllowedIPs = 0.0.0.0/0" in result
        # Address and DNS are wg-quick-only fields, stripped for `wg setconf`
        assert "Address" not in result
        assert "DNS" not in result

    def test_custom_port(self) -> None:
        """Port from the config Endpoint line is preserved in the rewritten endpoint."""
        conf = "Endpoint = 192.168.1.100:9999\n"
        result = _rewrite_wg_endpoint(conf, "10.0.2.2")
        assert "Endpoint = 10.0.2.2:9999" in result

    def test_endpoint_with_extra_whitespace(self) -> None:
        """Endpoint with irregular spacing is still matched and replaced, port preserved."""
        conf = "Endpoint  =  10.20.30.40:12345\n"
        result = _rewrite_wg_endpoint(conf, "10.0.2.2")
        assert "Endpoint = 10.0.2.2:12345" in result
        assert "10.20.30.40" not in result

    def test_no_endpoint_line(self) -> None:
        """Config without Endpoint line → no change, no error."""
        conf = "[Interface]\nPrivateKey = abc\n"
        result = _rewrite_wg_endpoint(conf, "10.0.2.2")
        assert result == conf

    def test_ipv6_endpoint_replaced(self) -> None:
        """IPv6 endpoint host is replaced with the IPv4 gateway, port preserved."""
        conf = "Endpoint = [::1]:51820\n"
        result = _rewrite_wg_endpoint(conf, "10.0.2.2")
        assert "Endpoint = 10.0.2.2:51820" in result
        assert "::1" not in result


# =============================================================================
# create_namespace — orchestration
# =============================================================================


class TestCreateNamespace:
    """Test the namespace creation orchestration."""

    @patch("ccproxy.inspector.namespace.subprocess.run")
    @patch("ccproxy.inspector.namespace.subprocess.Popen")
    @patch("ccproxy.inspector.namespace.os.pipe")
    @patch("ccproxy.inspector.namespace.os.fdopen")
    @patch("ccproxy.inspector.namespace.os.close")
    @patch("ccproxy.inspector.namespace.tempfile.mkstemp")
    def test_successful_creation(
        self,
        mock_mkstemp: Mock,
        mock_close: Mock,
        mock_fdopen: Mock,
        mock_pipe: Mock,
        mock_popen: Mock,
        mock_run: Mock,
        tmp_path: Path,
    ) -> None:
        """Happy path: all steps succeed → returns NamespaceContext."""
        conf_path = tmp_path / "wg.conf"
        mock_mkstemp.return_value = (10, str(conf_path))

        # Write conf file
        mock_fdopen_ctx = MagicMock()
        mock_fdopen.return_value.__enter__ = Mock(return_value=mock_fdopen_ctx)
        mock_fdopen.return_value.__exit__ = Mock(return_value=False)

        # Pipes: (ready_r, ready_w), (exit_r, exit_w)
        mock_pipe.side_effect = [(100, 101), (200, 201)]

        # Popen calls: sentinel, then slirp4netns
        sentinel_proc = MagicMock(pid=42)
        slirp_proc = MagicMock(pid=43)
        mock_popen.side_effect = [sentinel_proc, slirp_proc]

        # Ready-fd read: return "1" to signal readiness
        ready_file = MagicMock()
        ready_file.read.return_value = "1"
        ready_fdopen_ctx = MagicMock()
        ready_fdopen_ctx.__enter__ = Mock(return_value=ready_file)
        ready_fdopen_ctx.__exit__ = Mock(return_value=False)
        # First fdopen is for writing conf (fd=10), second for reading ready (fd=100)
        mock_fdopen.side_effect = [
            MagicMock(__enter__=Mock(return_value=mock_fdopen_ctx), __exit__=Mock(return_value=False)),
            ready_fdopen_ctx,
        ]

        # WG setup nsenter succeeds
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        ctx = create_namespace(SAMPLE_WG_CLIENT_CONF)

        assert ctx.ns_pid == 42
        assert ctx.slirp_proc == slirp_proc
        assert ctx.exit_w == 201  # write end of exit pipe

        # Verify unshare was called to create namespace
        unshare_call = mock_popen.call_args_list[0]
        assert "unshare" in unshare_call[0][0][0]
        assert "--net" in unshare_call[0][0]

        # Verify slirp4netns was called with correct args
        slirp_call = mock_popen.call_args_list[1]
        slirp_cmd = slirp_call[0][0]
        assert "slirp4netns" in slirp_cmd[0]
        assert "--configure" in slirp_cmd
        assert "--mtu=65520" in slirp_cmd

        # Verify nsenter WireGuard setup was called
        mock_run.assert_called_once()
        nsenter_call = mock_run.call_args[0][0]
        assert "nsenter" in nsenter_call[0]
        assert "-t" in nsenter_call
        assert "42" in nsenter_call  # ns_pid

    @patch("ccproxy.inspector.namespace.subprocess.Popen")
    @patch("ccproxy.inspector.namespace.tempfile.mkstemp")
    @patch("ccproxy.inspector.namespace.os.fdopen")
    @patch("ccproxy.inspector.namespace._safe_kill")
    def test_unshare_failure_cleans_up(
        self,
        mock_kill: Mock,
        mock_fdopen: Mock,
        mock_mkstemp: Mock,
        mock_popen: Mock,
        tmp_path: Path,
    ) -> None:
        """unshare fails → RuntimeError raised, temp conf file cleaned up."""
        conf_path = tmp_path / "wg.conf"
        conf_path.write_text("placeholder")
        mock_mkstemp.return_value = (10, str(conf_path))
        mock_fdopen.return_value.__enter__ = Mock(return_value=MagicMock())
        mock_fdopen.return_value.__exit__ = Mock(return_value=False)

        mock_popen.side_effect = FileNotFoundError("unshare not found")

        with pytest.raises(RuntimeError, match="Failed to create network namespace"):
            create_namespace(SAMPLE_WG_CLIENT_CONF)

        # Temp conf file should be cleaned up
        assert not conf_path.exists()

    @patch("ccproxy.inspector.namespace.subprocess.run")
    @patch("ccproxy.inspector.namespace.subprocess.Popen")
    @patch("ccproxy.inspector.namespace.os.pipe")
    @patch("ccproxy.inspector.namespace.os.fdopen")
    @patch("ccproxy.inspector.namespace.os.close")
    @patch("ccproxy.inspector.namespace.tempfile.mkstemp")
    @patch("ccproxy.inspector.namespace._safe_kill")
    @patch("ccproxy.inspector.namespace._safe_close")
    def test_slirp_not_ready_cleans_up(
        self,
        mock_safe_close: Mock,
        mock_safe_kill: Mock,
        mock_mkstemp: Mock,
        mock_close: Mock,
        mock_fdopen: Mock,
        mock_pipe: Mock,
        mock_popen: Mock,
        mock_run: Mock,
        tmp_path: Path,
    ) -> None:
        """slirp4netns writes empty to ready-fd → RuntimeError, resources cleaned."""
        conf_path = tmp_path / "wg.conf"
        mock_mkstemp.return_value = (10, str(conf_path))
        mock_pipe.side_effect = [(100, 101), (200, 201)]

        sentinel_proc = MagicMock(pid=42)
        slirp_proc = MagicMock(pid=43)
        mock_popen.side_effect = [sentinel_proc, slirp_proc]

        # First fdopen: write conf, second: read ready (returns empty = not ready)
        write_ctx = MagicMock()
        write_ctx.__enter__ = Mock(return_value=MagicMock())
        write_ctx.__exit__ = Mock(return_value=False)

        ready_file = MagicMock()
        ready_file.read.return_value = ""  # empty = not ready
        ready_ctx = MagicMock()
        ready_ctx.__enter__ = Mock(return_value=ready_file)
        ready_ctx.__exit__ = Mock(return_value=False)

        mock_fdopen.side_effect = [write_ctx, ready_ctx]

        with pytest.raises(RuntimeError, match="slirp4netns failed to become ready"):
            create_namespace(SAMPLE_WG_CLIENT_CONF)

        # Sentinel should be killed on failure
        mock_safe_kill.assert_called_with(42)

    @patch("ccproxy.inspector.namespace.subprocess.run")
    @patch("ccproxy.inspector.namespace.subprocess.Popen")
    @patch("ccproxy.inspector.namespace.os.pipe")
    @patch("ccproxy.inspector.namespace.os.fdopen")
    @patch("ccproxy.inspector.namespace.os.close")
    @patch("ccproxy.inspector.namespace.tempfile.mkstemp")
    @patch("ccproxy.inspector.namespace._safe_kill")
    @patch("ccproxy.inspector.namespace._safe_close")
    def test_wg_setup_failure_cleans_up(
        self,
        mock_safe_close: Mock,
        mock_safe_kill: Mock,
        mock_mkstemp: Mock,
        mock_close: Mock,
        mock_fdopen: Mock,
        mock_pipe: Mock,
        mock_popen: Mock,
        mock_run: Mock,
        tmp_path: Path,
    ) -> None:
        """nsenter WireGuard setup fails → RuntimeError, everything cleaned."""
        conf_path = tmp_path / "wg.conf"
        mock_mkstemp.return_value = (10, str(conf_path))
        mock_pipe.side_effect = [(100, 101), (200, 201)]

        sentinel_proc = MagicMock(pid=42)
        slirp_proc = MagicMock(pid=43)
        mock_popen.side_effect = [sentinel_proc, slirp_proc]

        write_ctx = MagicMock()
        write_ctx.__enter__ = Mock(return_value=MagicMock())
        write_ctx.__exit__ = Mock(return_value=False)

        ready_file = MagicMock()
        ready_file.read.return_value = "1"
        ready_ctx = MagicMock()
        ready_ctx.__enter__ = Mock(return_value=ready_file)
        ready_ctx.__exit__ = Mock(return_value=False)

        mock_fdopen.side_effect = [write_ctx, ready_ctx]

        # WG setup fails
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="RTNETLINK answers: Operation not permitted",
        )

        with pytest.raises(RuntimeError, match="WireGuard setup failed"):
            create_namespace(SAMPLE_WG_CLIENT_CONF)

        mock_safe_kill.assert_called_with(42)


# =============================================================================
# run_in_namespace — subprocess execution
# =============================================================================


class TestRunInNamespace:
    """Test running commands inside a confined namespace."""

    def test_returns_exit_code(self, mock_ctx: NamespaceContext) -> None:
        """Subprocess exit code is propagated."""
        with patch("ccproxy.inspector.namespace.subprocess.Popen") as mock_popen:
            proc = MagicMock()
            proc.wait.return_value = 42
            mock_popen.return_value = proc

            result = run_in_namespace(mock_ctx, ["echo", "hello"], {})

        assert result == 42

    def test_nsenter_command_structure(self, mock_ctx: NamespaceContext) -> None:
        """nsenter is called with correct namespace PID and command."""
        with patch("ccproxy.inspector.namespace.subprocess.Popen") as mock_popen:
            proc = MagicMock()
            proc.wait.return_value = 0
            mock_popen.return_value = proc

            run_in_namespace(mock_ctx, ["curl", "https://example.com"], {"PATH": "/bin"})

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "nsenter"
        assert "-t" in cmd
        assert str(mock_ctx.ns_pid) in cmd
        assert "--net" in cmd
        assert "--user" in cmd
        assert "--" in cmd
        assert cmd[-2:] == ["curl", "https://example.com"]

        # env is passed through
        assert mock_popen.call_args[1]["env"] == {"PATH": "/bin"}

    def test_keyboard_interrupt_terminates_process(self, mock_ctx: NamespaceContext) -> None:
        """KeyboardInterrupt → process is terminated, returns 130."""
        with patch("ccproxy.inspector.namespace.subprocess.Popen") as mock_popen:
            proc = MagicMock()
            proc.wait.side_effect = [KeyboardInterrupt, 130]
            mock_popen.return_value = proc

            result = run_in_namespace(mock_ctx, ["sleep", "100"], {})

        proc.terminate.assert_called_once()
        assert result == 130

    def test_keyboard_interrupt_force_kill_on_timeout(self, mock_ctx: NamespaceContext) -> None:
        """Process doesn't terminate after SIGTERM → gets killed, returns 130."""
        with patch("ccproxy.inspector.namespace.subprocess.Popen") as mock_popen:
            proc = MagicMock()
            proc.wait.side_effect = [
                KeyboardInterrupt,  # initial wait
                subprocess.TimeoutExpired("nsenter", 5),  # wait after terminate
            ]
            mock_popen.return_value = proc

            result = run_in_namespace(mock_ctx, ["sleep", "100"], {})

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert result == 130

    def test_zero_exit_code_on_success(self, mock_ctx: NamespaceContext) -> None:
        """Successful command returns 0."""
        with patch("ccproxy.inspector.namespace.subprocess.Popen") as mock_popen:
            proc = MagicMock()
            proc.wait.return_value = 0
            mock_popen.return_value = proc

            result = run_in_namespace(mock_ctx, ["true"], {})

        assert result == 0

    def test_nonzero_exit_code_propagated(self, mock_ctx: NamespaceContext) -> None:
        """Failed command exit code is returned as-is."""
        with patch("ccproxy.inspector.namespace.subprocess.Popen") as mock_popen:
            proc = MagicMock()
            proc.wait.return_value = 127
            mock_popen.return_value = proc

            result = run_in_namespace(mock_ctx, ["nonexistent"], {})

        assert result == 127


# =============================================================================
# cleanup_namespace — resource teardown
# =============================================================================


class TestCleanupNamespace:
    """Test namespace resource cleanup."""

    @patch("ccproxy.inspector.namespace._safe_kill")
    @patch("ccproxy.inspector.namespace._safe_close")
    def test_clean_shutdown(self, mock_close: Mock, mock_kill: Mock, mock_ctx: NamespaceContext) -> None:
        """Normal cleanup: close exit-fd, wait for slirp, kill sentinel, remove files."""
        mock_ctx.slirp_proc.wait.return_value = 0

        cleanup_namespace(mock_ctx)

        # exit-fd closed to trigger clean slirp4netns exit
        mock_close.assert_called_with(999)
        # slirp waited on
        mock_ctx.slirp_proc.wait.assert_called_once_with(timeout=2)
        # sentinel killed
        mock_kill.assert_called_once_with(mock_ctx.ns_pid)
        # temp conf file removed
        assert not mock_ctx.wg_conf_path.exists()

    @patch("ccproxy.inspector.namespace._safe_kill")
    @patch("ccproxy.inspector.namespace._safe_close")
    def test_slirp_timeout_force_kills(self, mock_close: Mock, mock_kill: Mock, mock_ctx: NamespaceContext) -> None:
        """slirp4netns doesn't exit after exit-fd close → force killed."""
        mock_ctx.slirp_proc.wait.side_effect = [
            subprocess.TimeoutExpired("slirp4netns", 2),  # first wait
            None,  # wait after kill
        ]

        cleanup_namespace(mock_ctx)

        mock_ctx.slirp_proc.kill.assert_called_once()

    @patch("ccproxy.inspector.namespace._safe_kill")
    @patch("ccproxy.inspector.namespace._safe_close")
    def test_api_socket_cleaned(self, mock_close: Mock, mock_kill: Mock, tmp_path: Path) -> None:
        """API socket file is removed if present."""
        conf_path = tmp_path / "wg.conf"
        conf_path.write_text("test")
        socket_path = tmp_path / "slirp.sock"
        socket_path.write_text("socket")

        ctx = NamespaceContext(
            ns_pid=99999,
            slirp_proc=MagicMock(spec=subprocess.Popen),
            exit_w=999,
            wg_conf_path=conf_path,
            api_socket=socket_path,
        )
        ctx.slirp_proc.wait.return_value = 0

        cleanup_namespace(ctx)

        assert not socket_path.exists()
        assert not conf_path.exists()

    @patch("ccproxy.inspector.namespace._safe_kill")
    @patch("ccproxy.inspector.namespace._safe_close")
    def test_exit_w_set_to_negative_after_close(
        self, mock_close: Mock, mock_kill: Mock, mock_ctx: NamespaceContext
    ) -> None:
        """exit_w is set to -1 after closing to prevent double-close."""
        mock_ctx.slirp_proc.wait.return_value = 0

        cleanup_namespace(mock_ctx)

        assert mock_ctx.exit_w == -1


# =============================================================================
# _safe_close / _safe_kill — low-level helpers
# =============================================================================


class TestSafeClose:
    """Test FD close helper."""

    @patch("os.close")
    def test_closes_valid_fd(self, mock_close: Mock) -> None:
        _safe_close(42)
        mock_close.assert_called_once_with(42)

    @patch("os.close")
    def test_ignores_negative_fd(self, mock_close: Mock) -> None:
        _safe_close(-1)
        mock_close.assert_not_called()

    @patch("os.close", side_effect=OSError("bad fd"))
    def test_ignores_os_error(self, mock_close: Mock) -> None:
        _safe_close(42)  # should not raise


class TestSafeKill:
    """Test process kill helper."""

    @patch("os.waitpid")
    @patch("os.kill")
    def test_kills_and_waits(self, mock_kill: Mock, mock_waitpid: Mock) -> None:
        _safe_kill(1234)
        mock_kill.assert_called_once_with(1234, signal.SIGKILL)
        mock_waitpid.assert_called_once_with(1234, 0)

    @patch("os.kill", side_effect=ProcessLookupError)
    def test_ignores_already_dead(self, mock_kill: Mock) -> None:
        _safe_kill(1234)  # should not raise

    @patch("os.kill", side_effect=OSError("unexpected"))
    def test_ignores_os_error(self, mock_kill: Mock) -> None:
        _safe_kill(1234)  # should not raise


# =============================================================================
# CLI integration — hard failure on missing prerequisites
# =============================================================================


class TestCliInspectHardFailure:
    """Verify that ccproxy run --inspect refuses to run without the jail."""

    @patch("ccproxy.cli.run_with_proxy")
    def test_inspect_flag_passed_through(self, mock_run: Mock, tmp_path: Path) -> None:
        """--inspect flag is extracted from args and passed to run_with_proxy."""
        from ccproxy.cli import Run, main

        cmd = Run(command=["--inspect", "--", "echo", "hello"])
        main(cmd, config_dir=tmp_path)

        mock_run.assert_called_once_with(
            tmp_path, ["echo", "hello"], inspect=True
        )

    @patch("ccproxy.inspector.namespace.check_namespace_capabilities")
    def test_missing_prerequisites_exits_1(self, mock_check: Mock, tmp_path: Path, capsys) -> None:
        """Missing prerequisites → exit(1), not fallback to unconfined execution."""
        from ccproxy.cli import run_with_proxy

        (tmp_path / "ccproxy.yaml").write_text("ccproxy: {}")

        mock_check.return_value = ["slirp4netns not found. Install with: nix profile install nixpkgs#slirp4netns"]

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "hello"], inspect=True)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "slirp4netns" in captured.err
        assert "Cannot create network namespace" in captured.err

    @patch("ccproxy.inspector.namespace.check_namespace_capabilities")
    def test_multiple_missing_prerequisites_all_reported(
        self, mock_check: Mock, tmp_path: Path, capsys
    ) -> None:
        """All missing prerequisites are listed before exiting."""
        from ccproxy.cli import run_with_proxy

        (tmp_path / "ccproxy.yaml").write_text("ccproxy: {}")

        mock_check.return_value = [
            "slirp4netns not found",
            "wg not found",
            "Unprivileged user namespaces disabled",
        ]

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "hello"], inspect=True)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "slirp4netns" in captured.err
        assert "wg" in captured.err
        assert "namespaces" in captured.err.lower()

    @patch("ccproxy.inspector.namespace.check_namespace_capabilities", return_value=[])
    def test_missing_wg_state_file_exits_1(self, mock_check: Mock, tmp_path: Path, capsys) -> None:
        """Prerequisites present but no WG state file → clear error about starting --inspect."""
        from ccproxy.cli import run_with_proxy

        (tmp_path / "ccproxy.yaml").write_text("ccproxy: {}")
        # No .inspector-wireguard-client.conf

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "hello"], inspect=True)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "ccproxy start --inspect" in captured.err

    @patch("ccproxy.inspector.namespace.check_namespace_capabilities", return_value=[])
    @patch("ccproxy.inspector.namespace.create_namespace")
    def test_namespace_runtime_error_exits_1(
        self, mock_create: Mock, mock_check: Mock, tmp_path: Path, capsys
    ) -> None:
        """Namespace creation fails at runtime → exit(1) with error message."""
        from ccproxy.cli import run_with_proxy

        (tmp_path / "ccproxy.yaml").write_text("ccproxy: {}")
        (tmp_path / ".inspector-wireguard-client.conf").write_text(SAMPLE_WG_CLIENT_CONF)

        mock_create.side_effect = RuntimeError("ip link add failed: Operation not permitted")

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "hello"], inspect=True)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Namespace setup failed" in captured.err

    @patch("ccproxy.inspector.namespace.check_namespace_capabilities", return_value=[])
    @patch("ccproxy.inspector.namespace.cleanup_namespace")
    @patch("ccproxy.inspector.namespace.run_in_namespace", return_value=0)
    @patch("ccproxy.inspector.namespace.create_namespace")
    def test_cleanup_always_called(
        self,
        mock_create: Mock,
        mock_run_ns: Mock,
        mock_cleanup: Mock,
        mock_check: Mock,
        tmp_path: Path,
    ) -> None:
        """cleanup_namespace is called even when run_in_namespace succeeds."""
        from ccproxy.cli import run_with_proxy

        (tmp_path / "ccproxy.yaml").write_text("ccproxy: {}")
        (tmp_path / ".inspector-wireguard-client.conf").write_text(SAMPLE_WG_CLIENT_CONF)

        ctx = MagicMock()
        mock_create.return_value = ctx

        with pytest.raises(SystemExit) as exc_info:
            run_with_proxy(tmp_path, ["echo", "hello"], inspect=True)

        assert exc_info.value.code == 0
        mock_cleanup.assert_called_once_with(ctx)

    @patch("ccproxy.inspector.namespace.check_namespace_capabilities", return_value=[])
    @patch("ccproxy.inspector.namespace.cleanup_namespace")
    @patch("ccproxy.inspector.namespace.create_namespace")
    def test_cleanup_called_on_error(
        self,
        mock_create: Mock,
        mock_cleanup: Mock,
        mock_check: Mock,
        tmp_path: Path,
    ) -> None:
        """cleanup_namespace is called even when create_namespace raises."""
        from ccproxy.cli import run_with_proxy

        (tmp_path / "ccproxy.yaml").write_text("ccproxy: {}")
        (tmp_path / ".inspector-wireguard-client.conf").write_text(SAMPLE_WG_CLIENT_CONF)

        mock_create.side_effect = RuntimeError("boom")

        with pytest.raises(SystemExit):
            run_with_proxy(tmp_path, ["echo", "hello"], inspect=True)

        # cleanup not called because ctx was None (create_namespace raised before returning)
        mock_cleanup.assert_not_called()

    def test_inspect_false_does_not_import_namespace(self, tmp_path: Path) -> None:
        """Non-inspect run doesn't touch namespace module at all."""
        from ccproxy.cli import run_with_proxy

        (tmp_path / "ccproxy.yaml").write_text("ccproxy: {}")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with pytest.raises(SystemExit) as exc_info:
                run_with_proxy(tmp_path, ["echo", "hello"], inspect=False)
            assert exc_info.value.code == 0
