"""Tests for configuration management."""

import concurrent.futures
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from ccproxy.config import (
    CCProxyConfig,
    CredentialSource,
    Provider,
    clear_config_instance,
    get_config,
    get_config_dir,
)
from ccproxy.oauth.sources import (
    CommandAuthSource,
    _read_credential_file,
    _run_credential_command,
)


def _make_provider(
    *,
    command: str = "echo tok",
    header: str | None = None,
    host: str = "api.example.com",
    path: str = "/v1/messages",
    provider: str = "anthropic",
) -> Provider:
    """Build a Provider with a CommandAuthSource for tests."""
    return Provider(
        auth=CommandAuthSource(command=command, header=header) if command else None,
        host=host,
        path=path,
        provider=provider,
    )


class TestCCProxyConfig:
    """Tests for main config class."""

    def test_default_config(self, monkeypatch: mock.MagicMock) -> None:
        """Test default configuration values."""
        monkeypatch.delenv("CCPROXY_HOST", raising=False)
        monkeypatch.delenv("CCPROXY_PORT", raising=False)
        config = CCProxyConfig()
        assert config.log_level == "INFO"
        assert config.host == "127.0.0.1"
        assert config.port == 4000
        assert config.ccproxy_config_path == Path("./ccproxy.yaml")

    def test_config_attributes(self) -> None:
        """Test config attributes can be set directly."""
        config = CCProxyConfig()
        config.log_level = "DEBUG"
        assert config.log_level == "DEBUG"

    def test_from_yaml_no_ccproxy_section(self) -> None:
        """Test loading ccproxy.yaml without ccproxy section."""
        yaml_content = """
# Empty YAML or missing ccproxy section
other_settings:
  key: value
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            assert config.log_level == "INFO"

        finally:
            yaml_path.unlink()

    def test_hook_parameters_from_yaml(self) -> None:
        """Test that hooks with parameters are loaded correctly."""
        yaml_content = """
ccproxy:
  hooks:
    - ccproxy.hooks.rule_evaluator
    - hook: ccproxy.hooks.capture_headers
      params:
        headers: [user-agent, x-request-id]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            assert len(config.hooks) == 2
            assert config.hooks[0] == "ccproxy.hooks.rule_evaluator"
            assert config.hooks[1] == {
                "hook": "ccproxy.hooks.capture_headers",
                "params": {"headers": ["user-agent", "x-request-id"]},
            }

        finally:
            yaml_path.unlink()

    def test_host_port_from_yaml(self, monkeypatch: mock.MagicMock) -> None:
        """Test that host and port are loaded from the ccproxy section of YAML."""
        monkeypatch.delenv("CCPROXY_HOST", raising=False)
        monkeypatch.delenv("CCPROXY_PORT", raising=False)

        yaml_content = """
ccproxy:
  host: "0.0.0.0"
  port: 9999
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            assert config.host == "0.0.0.0"
            assert config.port == 9999

        finally:
            yaml_path.unlink()

    def test_host_port_env_override(self, monkeypatch: mock.MagicMock) -> None:
        """Test that CCPROXY_PORT env var takes precedence over YAML value."""
        monkeypatch.setenv("CCPROXY_PORT", "5555")

        yaml_content = """
ccproxy:
  port: 9999
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            assert config.port == 5555

        finally:
            yaml_path.unlink()


class TestResolvedLogFile:
    """Tests for the ``resolved_log_file`` property."""

    def test_resolved_log_file_relative(self, tmp_path: Path) -> None:
        """Relative log_file resolves against ccproxy_config_path.parent."""
        config = CCProxyConfig()
        config.ccproxy_config_path = tmp_path / "ccproxy.yaml"
        config.log_file = Path("ccproxy.log")
        assert config.resolved_log_file == tmp_path / "ccproxy.log"

    def test_resolved_log_file_absolute(self, tmp_path: Path) -> None:
        """Absolute log_file passes through unchanged."""
        config = CCProxyConfig()
        config.ccproxy_config_path = tmp_path / "ccproxy.yaml"
        absolute_path = tmp_path / "elsewhere" / "ccproxy.log"
        config.log_file = absolute_path
        assert config.resolved_log_file == absolute_path

    def test_resolved_log_file_none(self) -> None:
        """log_file=None resolves to None."""
        config = CCProxyConfig()
        config.log_file = None
        assert config.resolved_log_file is None

    def test_log_file_from_yaml(self, tmp_path: Path) -> None:
        """YAML log_file value is parsed into the field."""
        yaml_path = tmp_path / "ccproxy.yaml"
        absolute_log = tmp_path / "foo.log"
        yaml_path.write_text(f"ccproxy:\n  log_file: {absolute_log}\n")
        config = CCProxyConfig.from_yaml(yaml_path)
        assert config.log_file == absolute_log
        assert config.resolved_log_file == absolute_log

    def test_log_file_yaml_null_disables(self, tmp_path: Path) -> None:
        """YAML log_file: null sets the field to None."""
        yaml_path = tmp_path / "ccproxy.yaml"
        yaml_path.write_text("ccproxy:\n  log_file: null\n")
        config = CCProxyConfig.from_yaml(yaml_path)
        assert config.log_file is None
        assert config.resolved_log_file is None


class TestJournalIdentifier:
    """Tests for the ``journal_identifier`` config field."""

    def test_journal_identifier_default_none(self, monkeypatch: mock.MagicMock) -> None:
        """Default value is None (derivation happens in cli._derive_journal_identifier)."""
        monkeypatch.delenv("CCPROXY_JOURNAL_IDENTIFIER", raising=False)
        config = CCProxyConfig()
        assert config.journal_identifier is None

    def test_journal_identifier_explicit_override(self, tmp_path: Path) -> None:
        """YAML journal_identifier value is parsed into the field."""
        yaml_path = tmp_path / "ccproxy.yaml"
        yaml_path.write_text("ccproxy:\n  journal_identifier: ccproxy-myproj\n")
        config = CCProxyConfig.from_yaml(yaml_path)
        assert config.journal_identifier == "ccproxy-myproj"

    def test_journal_identifier_env_override(self, monkeypatch: mock.MagicMock) -> None:
        """CCPROXY_JOURNAL_IDENTIFIER env var sets the field via pydantic-settings."""
        monkeypatch.setenv("CCPROXY_JOURNAL_IDENTIFIER", "ccproxy-fromenv")
        config = CCProxyConfig()
        assert config.journal_identifier == "ccproxy-fromenv"


class TestConfigSingleton:
    """Tests for configuration singleton functions."""

    def test_get_config_singleton(self) -> None:
        """Test that get_config returns the same instance."""
        clear_config_instance()

        # Create a custom config instance and set it directly
        custom_config = CCProxyConfig(log_level="DEBUG")
        from ccproxy.config import set_config_instance

        set_config_instance(custom_config)

        try:
            config1 = get_config()
            config2 = get_config()

            assert config1 is config2
            assert config1.log_level == "DEBUG"

        finally:
            clear_config_instance()

    def test_get_config_uses_ccproxy_yaml(self) -> None:
        """Test that get_config reads settings from ccproxy.yaml."""
        clear_config_instance()

        ccproxy_yaml_content = """
ccproxy:
  log_level: DEBUG
"""

        with tempfile.TemporaryDirectory() as temp_dir:
            import os

            ccproxy_yaml = Path(temp_dir) / "ccproxy.yaml"
            ccproxy_yaml.write_text(ccproxy_yaml_content)

            original_cwd = Path.cwd()
            os.chdir(temp_dir)

            try:
                with mock.patch.dict(os.environ, {"CCPROXY_CONFIG_DIR": temp_dir}):
                    config = get_config()
                    assert config.log_level == "DEBUG"
            finally:
                os.chdir(original_cwd)

        clear_config_instance()


class TestGetConfigDir:
    """Tests for get_config_dir() resolution."""

    def test_env_var_wins(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path / "explicit"))
        assert get_config_dir() == tmp_path / "explicit"

    def test_xdg_config_home(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("CCPROXY_CONFIG_DIR", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert get_config_dir() == tmp_path / "xdg" / "ccproxy"

    def test_default_fallback(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("CCPROXY_CONFIG_DIR", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        with mock.patch.object(Path, "home", return_value=tmp_path):
            assert get_config_dir() == tmp_path / ".config" / "ccproxy"


class TestThreadSafety:
    """Tests for thread-safe configuration access."""

    def test_concurrent_get_config(self) -> None:
        """Test that concurrent access to get_config is thread-safe."""
        import concurrent.futures
        import os
        import threading

        clear_config_instance()

        yaml_content = """
ccproxy:
  log_level: DEBUG
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            ccproxy_path = Path(temp_dir) / "ccproxy.yaml"
            ccproxy_path.write_text(yaml_content)

            original_cwd = Path.cwd()
            os.chdir(temp_dir)

            try:
                config_ids: set[int] = set()
                lock = threading.Lock()

                def get_and_track() -> None:
                    config = get_config()
                    with lock:
                        config_ids.add(id(config))

                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = [executor.submit(get_and_track) for _ in range(50)]
                    concurrent.futures.wait(futures)

                assert len(config_ids) == 1
            finally:
                os.chdir(original_cwd)
                clear_config_instance()


class TestReadCredentialFile:
    def test_existing_file_returns_stripped_content(self, tmp_path: Path) -> None:
        f = tmp_path / "cred.txt"
        f.write_text("   secret-token   \n")
        assert _read_credential_file(str(f), "TestCred") == "secret-token"

    def test_non_existent_file_returns_none(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "missing.txt"
        assert _read_credential_file(str(f), "TestCred") is None
        assert "TestCred file not found" in caplog.text

    def test_empty_file_returns_none(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "empty.txt"
        f.write_text(" \n \t  ")
        assert _read_credential_file(str(f), "TestCred") is None
        assert "TestCred file is empty" in caplog.text

    def test_exception_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_resolve = Path.resolve

        def mock_resolve(self: Path, *args: object, **kwargs: object) -> Path:
            if str(self).endswith("error.txt"):
                raise PermissionError("Access Denied")
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", mock_resolve)
        f = tmp_path / "error.txt"
        assert _read_credential_file(str(f), "TestCred") is None
        assert "Failed to read TestCred file" in caplog.text


class TestRunCredentialCommand:
    def test_success_returns_stripped_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="   cmd-token   \n")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))
        assert _run_credential_command("echo cmd-token", "TestCmd") == "cmd-token"

    def test_non_zero_exit_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_result = mock.MagicMock(returncode=127, stderr=" command not found \n")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))
        assert _run_credential_command("badcmd", "TestCmd") is None
        assert "TestCmd command failed (exit 127)" in caplog.text

    def test_empty_stdout_returns_none(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="\n   \n")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))
        assert _run_credential_command("echo", "TestCmd") is None
        assert "TestCmd command returned empty output" in caplog.text

    def test_timeout_expired_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def mock_run_timeout(*args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="sleep", timeout=5)

        monkeypatch.setattr(subprocess, "run", mock_run_timeout)
        assert _run_credential_command("sleep 10", "TestCmd") is None
        assert "TestCmd command timed out after 5 seconds" in caplog.text

    def test_other_exception_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def mock_run_error(*args: object, **kwargs: object) -> None:
            raise OSError("No such file or directory")

        monkeypatch.setattr(subprocess, "run", mock_run_error)
        assert _run_credential_command("missing", "TestCmd") is None
        assert "Failed to execute TestCmd command" in caplog.text


class TestCredentialSource:
    def test_resolve_file(self, tmp_path: Path) -> None:
        f = tmp_path / "cred.txt"
        f.write_text("file-credential")
        source = CredentialSource(file=str(f))
        assert source.resolve() == "file-credential"

    def test_resolve_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="cmd-credential")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))
        source = CredentialSource(command="echo cmd")
        assert source.resolve() == "cmd-credential"

    def test_requires_exactly_one_source(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            CredentialSource()  # neither file nor command


class TestRefreshOAuthToken:
    def test_token_changes_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = CCProxyConfig(providers={"provider1": _make_provider(command="echo new-token")})
        config._cached_auth_tokens["provider1"] = "old-token"
        mock_result = mock.MagicMock(returncode=0, stdout="new-token")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))

        token, changed = config.refresh_oauth_token("provider1")

        assert token == "new-token"  # noqa: S105
        assert changed is True
        assert config._cached_auth_tokens["provider1"] == "new-token"

    def test_token_unchanged_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = CCProxyConfig(providers={"provider1": _make_provider(command="echo current-token")})
        config._cached_auth_tokens["provider1"] = "current-token"
        mock_result = mock.MagicMock(returncode=0, stdout="current-token")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))

        token, changed = config.refresh_oauth_token("provider1")

        assert token == "current-token"  # noqa: S105
        assert changed is False

    def test_provider_not_configured_returns_none(self) -> None:
        config = CCProxyConfig()
        token, changed = config.refresh_oauth_token("missing-provider")
        assert token is None
        assert changed is False


class TestGetAuthHeader:
    def test_provider_with_auth_header(self) -> None:
        config = CCProxyConfig(providers={"prov": _make_provider(header="x-api-key")})
        assert config.get_auth_header("prov") == "x-api-key"

    def test_provider_without_auth_header_returns_none(self) -> None:
        config = CCProxyConfig(providers={"prov": _make_provider(header=None)})
        assert config.get_auth_header("prov") is None

    def test_missing_provider_returns_none(self) -> None:
        config = CCProxyConfig()
        assert config.get_auth_header("unknown") is None


class TestLoadCredentials:
    def test_empty_providers_clears_cache(self) -> None:
        config = CCProxyConfig()
        config._cached_auth_tokens = {"stale": "data"}
        config._load_credentials()
        assert config._cached_auth_tokens == {}

    def test_single_provider_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = CCProxyConfig(providers={"prov1": _make_provider(command="echo tok1")})
        mock_result = mock.MagicMock(returncode=0, stdout="tok1")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))

        config._load_credentials()

        assert config._cached_auth_tokens["prov1"] == "tok1"

    def test_partial_failure_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = CCProxyConfig(
            providers={
                "prov1": _make_provider(command="echo tok1"),
                "prov2": _make_provider(command="fail"),
            }
        )

        def mock_run(cmd: str, **kwargs: object) -> mock.MagicMock:
            m = mock.MagicMock()
            if "tok1" in cmd:
                m.returncode = 0
                m.stdout = "tok1"
            else:
                m.returncode = 1
                m.stderr = "error"
            return m

        monkeypatch.setattr(subprocess, "run", mock_run)

        config._load_credentials()

        assert config._cached_auth_tokens == {"prov1": "tok1"}
        assert "but 1 provider(s) failed to load" in caplog.text

    def test_all_providers_fail_logs_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = CCProxyConfig(
            providers={
                "prov1": _make_provider(command="fail1"),
                "prov2": _make_provider(command="fail2"),
            }
        )
        mock_result = mock.MagicMock(returncode=1, stderr="err")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))

        config._load_credentials()

        assert config._cached_auth_tokens == {}
        assert "Failed to load auth tokens for all 2 provider(s)" in caplog.text


class TestRefreshOAuthTokenConcurrency:
    """Concurrent-refresh single-flight tests for the per-provider lock."""

    def test_concurrent_refresh_dedups_to_single_subprocess_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """20 threads simultaneously calling refresh_oauth_token must produce
        exactly ONE underlying credential resolution. Per-provider lock plus
        the in-lock cache re-check make the 19 followers a no-op once the
        first thread finishes."""
        provider_name = "anthropic"
        config = CCProxyConfig(providers={provider_name: _make_provider(command="echo tok-fresh")})

        call_count = 0
        call_count_lock = threading.Lock()
        # Barrier ensures all 20 threads reach refresh_oauth_token before any
        # of them is allowed to acquire the per-provider lock.
        barrier = threading.Barrier(20)

        def counting_run(*args: object, **kwargs: object) -> mock.MagicMock:
            nonlocal call_count
            with call_count_lock:
                call_count += 1
            # Simulate a slow upstream so the followers definitely queue on
            # the per-provider lock while this call is in flight.
            time.sleep(0.05)
            return mock.MagicMock(returncode=0, stdout="tok-fresh")

        monkeypatch.setattr(subprocess, "run", counting_run)

        results: list[tuple[str | None, bool]] = []
        results_lock = threading.Lock()

        def call_refresh() -> None:
            barrier.wait()
            result = config.refresh_oauth_token(provider_name)
            with results_lock:
                results.append(result)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(call_refresh) for _ in range(20)]
            concurrent.futures.wait(futures)

        assert call_count == 1, f"expected exactly one upstream credential call, got {call_count}"
        assert len(results) == 20
        for token, _changed in results:
            assert token == "tok-fresh"  # noqa: S105
        assert config._cached_auth_tokens[provider_name] == "tok-fresh"

    def test_cross_provider_refreshes_do_not_block_each_other(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A slow refresh on provider-A must NOT delay a concurrent refresh
        on provider-B. Per-provider locks gate independently."""
        slow_provider = "slow"
        fast_provider = "fast"
        config = CCProxyConfig(
            providers={
                slow_provider: _make_provider(command="echo slow-tok"),
                fast_provider: _make_provider(command="echo fast-tok"),
            }
        )

        slow_started = threading.Event()
        slow_release = threading.Event()

        def routed_run(cmd: str, **kwargs: object) -> mock.MagicMock:
            if "slow-tok" in cmd:
                slow_started.set()
                # Block here until the test signals release. Long enough that
                # if cross-provider serialization were happening the fast
                # call would clearly time out.
                slow_release.wait(timeout=5.0)
                return mock.MagicMock(returncode=0, stdout="slow-tok")
            return mock.MagicMock(returncode=0, stdout="fast-tok")

        monkeypatch.setattr(subprocess, "run", routed_run)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            slow_future = pool.submit(config.refresh_oauth_token, slow_provider)

            assert slow_started.wait(timeout=2.0), "slow provider refresh did not start in time"

            fast_start = time.monotonic()
            fast_future = pool.submit(config.refresh_oauth_token, fast_provider)

            fast_token, fast_changed = fast_future.result(timeout=2.0)
            fast_elapsed = time.monotonic() - fast_start

            slow_release.set()
            slow_token, slow_changed = slow_future.result(timeout=5.0)

        assert fast_token == "fast-tok"  # noqa: S105
        assert fast_changed is True
        assert slow_token == "slow-tok"  # noqa: S105
        assert slow_changed is True
        # Fast provider must complete promptly while slow provider is still
        # blocked; allow generous slack but require sub-second.
        assert fast_elapsed < 1.0, (
            f"fast provider refresh took {fast_elapsed:.3f}s — per-provider locks are not isolating providers"
        )
