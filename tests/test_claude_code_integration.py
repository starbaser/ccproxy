"""End-to-end integration tests for Claude Code with ccproxy.

This test suite validates that the `claude` command works correctly when routed through ccproxy.
"""

import json
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Generator
from contextlib import closing, suppress
from pathlib import Path

import httpx
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
        mock_claude.write_text(r"""#!/usr/bin/env bash
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

        env = os.environ.copy()
        env["CCPROXY_CONFIG_DIR"] = str(test_config_dir)

        # Use the absolute path to the mock so PATH lookup is bypassed.
        # This avoids picking up system wrappers (e.g. NixOS claude shims) that
        # would intercept a bare "claude" argument before the mock is reached.
        result = subprocess.run(
            ["uv", "run", "ccproxy", "run", "--", str(mock_claude), "-p", "Hello"],
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

            # Create minimal settings.json for claude wrapper
            (claude_dir / "settings.json").write_text(json.dumps({"custom": {}}))

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
        env.pop("CLAUDECODE", None)  # Allow nested launch in test context

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
                        "uv",
                        "run",
                        "ccproxy",
                        "--config-dir",
                        config_dir_str,
                        "run",
                        "--",
                        "claude",
                        "-p",
                        "What is 2+2?",
                        "--model",
                        "claude-opus-4-5-20251101",
                        "--no-session-persistence",
                        "--strict-mcp-config",
                        "--disable-slash-commands",
                        "--allowedTools",
                        "",  # No tools allowed
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

    @pytest.fixture
    def oauth_config_dir(self) -> Generator[tuple[Path, int, str], None, None]:
        """Create config directory for OAuth E2E test.

        Resolves the OAuth token from known credential locations and
        writes a ccproxy config that uses the token directly via file source.

        Yields:
            Tuple of (config_dir, port, oauth_token).
        """
        # Find OAuth token from known locations
        oauth_token = self._resolve_oauth_token()
        if not oauth_token:
            pytest.fail(
                "No OAuth token found. Checked:\n"
                "  - ~/.ccproxy/.claude.credentials.json (claudeAiOauth.accessToken)\n"
                "  - ~/.claude/.credentials.json (claudeAiOauth.accessToken)\n"
                "  - CCPROXY_TEST_OAUTH_TOKEN env var"
            )

        port = find_free_port()

        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)

            # Write the token to a file for the oat_sources file: source
            token_file = config_dir / "oauth-token"
            token_file.write_text(oauth_token)
            token_file.chmod(0o600)

            litellm_config = {
                "model_list": [
                    {
                        "model_name": "claude-haiku-4-5-20251001",
                        "litellm_params": {
                            "model": "anthropic/claude-haiku-4-5-20251001",
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
                        "ccproxy.hooks.rule_evaluator",
                        "ccproxy.hooks.model_router",
                        "ccproxy.hooks.forward_oauth",
                        "ccproxy.hooks.add_beta_headers",
                        "ccproxy.hooks.inject_claude_code_identity",
                    ],
                    "oat_sources": {
                        "anthropic": {
                            "file": str(token_file),
                            "destinations": ["api.anthropic.com"],
                        },
                    },
                    "rules": [],
                },
            }

            (config_dir / "config.yaml").write_text(yaml.dump(litellm_config))
            (config_dir / "ccproxy.yaml").write_text(yaml.dump(ccproxy_config))

            try:
                yield config_dir, port, oauth_token
            finally:
                self._kill_processes_on_port(port)

    def _resolve_oauth_token(self) -> str | None:
        """Find an OAuth token from known credential locations."""
        # 1. Explicit test override
        env_token = os.environ.get("CCPROXY_TEST_OAUTH_TOKEN")
        if env_token:
            return env_token

        # 2. Active Claude Code session token
        session_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if session_token:
            return session_token

        # 3. Credentials files
        for cred_path in [
            Path.home() / ".ccproxy" / ".claude.credentials.json",
            Path.home() / ".claude" / ".credentials.json",
        ]:
            if cred_path.exists():
                try:
                    creds = json.loads(cred_path.read_text())
                    token = creds.get("claudeAiOauth", {}).get("accessToken")
                    if token:
                        return token
                except (json.JSONDecodeError, KeyError):
                    continue

        return None

    @pytest.mark.e2e
    def test_oauth_forwarding_e2e(self, oauth_config_dir: tuple[Path, int, str]) -> None:
        """Test OAuth token forwarding through ccproxy to Anthropic API.

        Sends a direct HTTP request to the proxy with a Bearer OAuth token
        and verifies the full pipeline: token forwarding, beta headers,
        identity injection, and a successful API response.

        Uses haiku with max_tokens=1 to minimize cost.
        """
        config_dir, port, oauth_token = oauth_config_dir
        config_dir_str = str(config_dir)

        env = os.environ.copy()
        env["CCPROXY_TEST_MODE"] = "1"

        # Start ccproxy
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
            base_url = f"http://127.0.0.1:{port}"
            self._wait_for_proxy(base_url, timeout=15)

            # Send a minimal request with OAuth Bearer token
            response = httpx.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {oauth_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                timeout=30,
            )

            print(f"\n=== OAuth E2E Response ===")
            print(f"Status: {response.status_code}")
            print(f"Body: {response.text[:2000]}")
            print(f"==========================\n")

            # Print proxy logs
            log_file = config_dir / "litellm.log"
            if log_file.exists():
                print(f"\n=== Proxy Logs (last 5KB) ===")
                print(log_file.read_text()[-5000:])
                print(f"=============================\n")

            # These non-200 statuses prove the pipeline worked (request reached Anthropic)
            if response.status_code == 429:
                pytest.skip("Rate limited by Anthropic — OAuth pipeline connectivity verified")
            if response.status_code == 401 and "expired" in response.text.lower():
                pytest.skip("OAuth token expired — OAuth pipeline connectivity verified (refresh token)")

            assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text[:500]}"

            body = response.json()
            assert "choices" in body, f"Missing 'choices' in response: {body}"
            assert len(body["choices"]) > 0, f"Empty choices in response: {body}"

        finally:
            subprocess.run(
                ["uv", "run", "ccproxy", "--config-dir", config_dir_str, "stop"],
                env=env,
                capture_output=True,
                timeout=10,
            )

    def _wait_for_proxy(self, base_url: str, timeout: int = 15) -> None:
        """Poll the proxy health endpoint until it responds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = httpx.get(f"{base_url}/health", timeout=2)
                if r.status_code in (200, 503):
                    # 503 = healthy but no models yet; proxy is up
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.5)
        pytest.fail(f"Proxy at {base_url} did not become ready within {timeout}s")
