"""End-to-end integration tests for Claude Code with ccproxy.

This test suite validates that the `claude` command works correctly when routed through ccproxy.
"""

import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Generator
from contextlib import closing, suppress
from pathlib import Path

import psutil
import pytest
import yaml


def find_free_port() -> int:
    """Find a free port to use for testing."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


@pytest.mark.skipif(
    subprocess.run(["which", "claude"], capture_output=True).returncode != 0, reason="claude command not available"
)
class TestClaudeCodeE2E:
    """End-to-end test that validates claude command works through ccproxy."""

    @pytest.fixture
    def test_config_dir(self) -> Generator[Path, None, None]:
        """Create a test configuration directory with minimal ccproxy config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)

            # Create minimal litellm proxy config with Anthropic models
            litellm_config = {
                "model_list": [
                    {
                        "model_name": "default",
                        "litellm_params": {
                            "model": "claude-sonnet-4-5-20250929",
                            "api_base": "https://api.anthropic.com",
                        },
                    }
                ]
            }

            # Create minimal ccproxy config with OAuth support for real API calls
            ccproxy_config = {
                "litellm": {"host": "127.0.0.1", "port": find_free_port(), "num_workers": 1, "telemetry": False},
                "ccproxy": {
                    "debug": False,
                    "hooks": [
                        "ccproxy.hooks.model_router",
                        "ccproxy.hooks.forward_oauth",
                        "ccproxy.hooks.add_beta_headers",
                        "ccproxy.hooks.inject_claude_code_identity",
                    ],
                    "oat_sources": {
                        "anthropic": "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json",
                    },
                    "rules": [],
                },
            }

            # Write config files
            (config_dir / "config.yaml").write_text(yaml.dump(litellm_config))
            (config_dir / "ccproxy.yaml").write_text(yaml.dump(ccproxy_config))

            yield config_dir

    def test_claude_simple_query_with_mock(self, test_config_dir):
        """Test that claude command environment is set up correctly by ccproxy run."""
        # Create a mock claude script that just verifies environment is set
        mock_claude = test_config_dir / "claude"
        mock_claude.write_text(r"""#!/bin/bash
# Check if ANTHROPIC_BASE_URL is set to something that looks like a proxy
if [[ "$ANTHROPIC_BASE_URL" =~ ^http://127\.0\.0\.1:[0-9]+$ ]]; then
    echo "SUCCESS: Environment configured correctly"
    echo "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"
    echo "Args: $@"
    exit 0
else
    echo "FAIL: ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL (should match http://127.0.0.1:PORT)"
    exit 1
fi
""")
        mock_claude.chmod(0o755)

        # Add mock claude to PATH
        env = os.environ.copy()
        env["PATH"] = f"{test_config_dir}:{env['PATH']}"
        env["CCPROXY_CONFIG_DIR"] = str(test_config_dir)

        # Run ccproxy run command with proper argument separation
        result = subprocess.run(
            ["uv", "run", "ccproxy", "run", "--", "claude", "-p", "Hello"],
            env=env,
            cwd=test_config_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0, f"Command failed. stdout: {result.stdout}, stderr: {result.stderr}"
        assert "SUCCESS" in result.stdout

    @pytest.fixture
    def e2e_config_dir(self) -> Generator[tuple[Path, int], None, None]:
        """Create config directory for E2E test and ensure process cleanup.

        Yields:
            Tuple of (config_dir, port) for the test to use.
        """
        port = find_free_port()
        real_home = Path.home()

        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)

            # Create isolated .claude directory with just credentials (no hooks)
            claude_dir = config_dir / ".claude"
            claude_dir.mkdir()

            # Create .ccproxy directory (HOME is overridden, so ccproxy looks here)
            ccproxy_dir = config_dir / ".ccproxy"
            ccproxy_dir.mkdir()

            # Copy credentials from real home if they exist
            real_creds = real_home / ".claude" / ".credentials.json"
            if real_creds.exists():
                import shutil
                shutil.copy(real_creds, claude_dir / ".credentials.json")

            litellm_config = {
                "model_list": [
                    {
                        "model_name": "default",
                        "litellm_params": {
                            "model": "claude-sonnet-4-5-20250929",
                            "api_base": "https://api.anthropic.com",
                        },
                    },
                    {
                        "model_name": "claude-opus-4-5-20251101",
                        "litellm_params": {
                            "model": "anthropic/claude-opus-4-5-20251101",
                            "api_base": "https://api.anthropic.com",
                        },
                    },
                ],
                "litellm_settings": {
                    "callbacks": ["ccproxy.handler"],
                },
                "general_settings": {
                    "max_parallel_requests": 1000000,
                    "global_max_parallel_requests": 1000000,
                    "forward_client_headers_to_llm_api": True,
                },
            }

            ccproxy_config = {
                "litellm": {"host": "127.0.0.1", "port": port, "num_workers": 1, "telemetry": False},
                "ccproxy": {
                    "debug": True,
                    "default_model_passthrough": True,
                    "hooks": [
                        "ccproxy.hooks.model_router",
                        "ccproxy.hooks.forward_oauth",
                        "ccproxy.hooks.add_beta_headers",
                        "ccproxy.hooks.inject_claude_code_identity",
                    ],
                    "oat_sources": {
                        "anthropic": "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json",
                    },
                    "rules": [],
                },
            }

            (config_dir / "config.yaml").write_text(yaml.dump(litellm_config))
            (config_dir / "ccproxy.yaml").write_text(yaml.dump(ccproxy_config))

            try:
                yield config_dir, port
            finally:
                # Aggressive cleanup: kill any process listening on our port
                self._kill_processes_on_port(port)
                # Also kill by PID file if it exists
                pid_file = config_dir / "litellm.pid"
                if pid_file.exists():
                    try:
                        pid = int(pid_file.read_text().strip())
                        self._kill_process_tree(pid)
                    except (ValueError, OSError):
                        pass

    def _kill_processes_on_port(self, port: int) -> None:
        """Kill any processes listening on the given port."""
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                for conn in proc.net_connections():
                    if hasattr(conn, "laddr") and conn.laddr and conn.laddr.port == port:
                        self._kill_process_tree(proc.pid)
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

    def _kill_process_tree(self, pid: int) -> None:
        """Kill a process and all its children."""
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                with suppress(psutil.NoSuchProcess):
                    child.kill()
            parent.kill()
            parent.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired:
            pass

    @pytest.mark.e2e
    def test_claude_real_cli_e2e(self, e2e_config_dir: tuple[Path, int]) -> None:
        """Run real claude CLI with a simple prompt through ccproxy.

        This test:
        1. Starts ccproxy proxy server in background
        2. Runs `claude -p` with a simple prompt through ccproxy
        3. Validates the response
        4. Cleans up all processes aggressively
        """
        config_dir, _port = e2e_config_dir
        config_dir_str = str(config_dir)

        # Create isolated environment - use temp dir as HOME to avoid user's hooks
        env = os.environ.copy()
        env["CCPROXY_TEST_MODE"] = "1"  # Signal we're in test mode
        env["HOME"] = config_dir_str  # Redirect HOME so Claude uses isolated .claude dir

        # Start ccproxy in background with explicit config dir
        start_result = subprocess.run(
            ["uv", "run", "ccproxy", "--config-dir", config_dir_str, "start", "--detach"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert start_result.returncode == 0, f"Failed to start ccproxy: {start_result.stderr}"

        try:
            # Wait for proxy to be ready
            time.sleep(3)

            # Run claude with a simple prompt - locked down config for testing
            try:
                result = subprocess.run(
                    [
                        "uv", "run", "ccproxy", "--config-dir", config_dir_str, "run", "--",
                        "claude", "-p", "What is 2+2?",
                        "--model", "claude-opus-4-5-20251101",
                        "--no-session-persistence",
                        "--strict-mcp-config",
                        "--disable-slash-commands",
                        "--allowedTools", "",  # No tools allowed
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired as e:
                # Print logs even on timeout
                log_file = config_dir / "litellm.log"
                if log_file.exists():
                    print(f"\n=== Proxy Logs on Timeout ===")
                    print(log_file.read_text()[-15000:])
                raise AssertionError(f"Claude command timed out after 60s. stdout={e.stdout}, stderr={e.stderr}")

            # Always print Claude output for debugging
            print(f"\n=== Claude CLI Output ===")
            print(f"Return code: {result.returncode}")
            print(f"STDOUT:\n{result.stdout}")
            print(f"STDERR:\n{result.stderr}")
            print(f"=========================\n")

            # Print proxy logs if available
            log_file = config_dir / "litellm.log"
            if log_file.exists():
                print(f"\n=== Proxy Logs (last 50 lines) ===")
                print(log_file.read_text()[-10000:])  # Last ~10KB
                print(f"==================================\n")

            # Check for success or acceptable API errors (rate limit proves connectivity)
            if result.returncode != 0:
                # Rate limit error means proxy is working - request reached Anthropic
                if "rate limit" in result.stdout.lower() or "rate limit" in result.stderr.lower():
                    pytest.skip("Rate limited by Anthropic API - proxy connectivity verified")
                # Subscription tier error - proxy working but account limitation
                if "not available with" in result.stdout.lower():
                    pytest.skip("Model not available on account tier - proxy connectivity verified")
                raise AssertionError(f"Claude command failed: {result.stderr}\nstdout: {result.stdout}")

            # Response should contain "4"
            assert "4" in result.stdout, f"Expected '4' in response, got: {result.stdout}"

        finally:
            # Always attempt graceful stop first
            subprocess.run(
                ["uv", "run", "ccproxy", "--config-dir", config_dir_str, "stop"],
                env=env,
                capture_output=True,
                timeout=10,
            )
            # Fixture cleanup will kill any remaining processes
