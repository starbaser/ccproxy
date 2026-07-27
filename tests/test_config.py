"""Tests for configuration management."""

import concurrent.futures
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

from ccproxy.auth.sources import (
    CommandAuthSource,
    FileAuthSource,
    _read_credential_file,
    _run_credential_command,
)
from ccproxy.config import (
    CCProxyConfig,
    GeminiCapacityFallbackConfig,
    LightllmConfig,
    OpenAIConversationsConfig,
    Provider,
    TransformOverride,
    clear_config_instance,
    get_config,
    get_config_dir,
)


def _make_provider(
    *,
    command: str = "echo tok",
    header: str | None = None,
    host: str = "api.example.com",
    path: str = "/v1/messages",
    type: str = "anthropic",
) -> Provider:
    """Build a Provider with a CommandAuthSource for tests."""
    return Provider(
        auth=CommandAuthSource(command=command, header=header) if command else None,
        base_url=f"https://{host}",
        path=path,
        type=type,
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
        assert config.provider_max_connections == 256
        assert config.ccproxy_config_path == Path("./ccproxy.yaml")

    def test_legacy_provider_host_migrates_to_https_base_url(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "ccproxy.yaml"
        yaml_path.write_text(
            """
ccproxy:
  providers:
    anthropic:
      host: api.anthropic.com
      path: /v1/messages
      type: anthropic
"""
        )

        config = CCProxyConfig.from_yaml(yaml_path, litellm_path=tmp_path / "absent.yaml")

        assert config.providers["anthropic"].base_url == "https://api.anthropic.com"
        assert config.providers["anthropic"].host == "api.anthropic.com"

    def test_from_yaml_no_project_section(self) -> None:
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
    inbound:
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

            inbound_hooks = config.hooks["inbound"]
            assert len(inbound_hooks) == 2
            assert inbound_hooks[0] == "ccproxy.hooks.rule_evaluator"
            assert inbound_hooks[1] == {
                "hook": "ccproxy.hooks.capture_headers",
                "params": {"headers": ["user-agent", "x-request-id"]},
            }

        finally:
            yaml_path.unlink()

    def test_lightllm_transforms_from_yaml(self) -> None:
        """Test that transform overrides load from the lightllm section."""
        yaml_content = """
ccproxy:
  lightllm:
    transforms:
      - match_path: ^/v1/chat/completions$
        action: transform
        dest_provider: anthropic
        dest_model: claude-haiku-4-5-20251001
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            assert len(config.lightllm.transforms) == 1
            transform = config.lightllm.transforms[0]
            assert transform.match_path == "^/v1/chat/completions$"
            assert transform.action == "transform"
            assert transform.dest_provider == "anthropic"
            assert transform.dest_model == "claude-haiku-4-5-20251001"

        finally:
            yaml_path.unlink()

    def test_legacy_transform_destination_host_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="dest_host"):
            TransformOverride.model_validate({"dest_host": "api.anthropic.com"})

    def test_inspector_transforms_rejected(self) -> None:
        """Test that the old inspector.transforms location fails clearly."""
        yaml_content = """
ccproxy:
  inspector:
    transforms:
      - action: passthrough
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="inspector\\.transforms has moved to lightllm\\.transforms"):
                CCProxyConfig.from_yaml(yaml_path)

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

    def test_get_config_uses_config_yaml(self) -> None:
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

    def test_env_var_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path / "explicit"))
        assert get_config_dir() == tmp_path / "explicit"

    def test_xdg_config_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CCPROXY_CONFIG_DIR", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert get_config_dir() == tmp_path / "xdg" / "ccproxy"

    def test_default_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CCPROXY_CONFIG_DIR", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        with mock.patch.object(Path, "home", return_value=tmp_path):
            assert get_config_dir() == tmp_path / ".config" / "ccproxy"


class TestThreadSafety:
    """Tests for thread-safe configuration access."""

    def test_concurrent_get_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that concurrent access to get_config is thread-safe."""
        import concurrent.futures
        import threading

        clear_config_instance()

        yaml_content = """
ccproxy:
  log_level: DEBUG
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            ccproxy_path = Path(temp_dir) / "ccproxy.yaml"
            ccproxy_path.write_text(yaml_content)

            monkeypatch.setenv("CCPROXY_CONFIG_DIR", temp_dir)
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

        def mock_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
            if str(self).endswith("error.txt"):
                raise PermissionError("Access Denied")
            return cast(Path, cast(Any, original_resolve)(self, *args, **kwargs))

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


class TestResolveAuthToken:
    def test_resolves_via_provider_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = CCProxyConfig(providers={"prov": _make_provider(command="echo fresh-tok")})
        mock_result = mock.MagicMock(returncode=0, stdout="fresh-tok")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))

        assert config.resolve_auth_token("prov") == "fresh-tok"

    def test_provider_not_configured_returns_none(self) -> None:
        config = CCProxyConfig()
        assert config.resolve_auth_token("missing-provider") is None

    def test_provider_without_auth_returns_none(self) -> None:
        config = CCProxyConfig(providers={"prov": _make_provider(command="")})
        assert config.resolve_auth_token("prov") is None

    def test_resolves_through_file_source(self, tmp_path: Path) -> None:
        f = tmp_path / "tok.txt"
        f.write_text("file-tok")
        config = CCProxyConfig(
            providers={
                "prov": Provider(
                    auth=FileAuthSource(file=str(f)),
                    base_url="https://api.example.com",
                    path="/v1/messages",
                    type="anthropic",
                ),
            }
        )
        assert config.resolve_auth_token("prov") == "file-tok"


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


class TestResolveAuthTokenConcurrency:
    """Per-provider lock isolates concurrent resolves across providers."""

    def test_cross_provider_resolves_do_not_block_each_other(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A slow resolve on provider-A must NOT delay a concurrent resolve
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
                slow_release.wait(timeout=5.0)
                return mock.MagicMock(returncode=0, stdout="slow-tok")
            return mock.MagicMock(returncode=0, stdout="fast-tok")

        monkeypatch.setattr(subprocess, "run", routed_run)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            slow_future = pool.submit(config.resolve_auth_token, slow_provider)

            assert slow_started.wait(timeout=2.0), "slow provider resolve did not start in time"

            fast_start = time.monotonic()
            fast_future = pool.submit(config.resolve_auth_token, fast_provider)

            fast_token = fast_future.result(timeout=2.0)
            fast_elapsed = time.monotonic() - fast_start

            slow_release.set()
            slow_token = slow_future.result(timeout=5.0)

        assert fast_token == "fast-tok"  # noqa: S105
        assert slow_token == "slow-tok"  # noqa: S105
        assert fast_elapsed < 1.0, (
            f"fast provider resolve took {fast_elapsed:.3f}s — per-provider locks are not isolating providers"
        )


class TestGeminiCapacityConfig:
    """Tests for the gemini_capacity config block."""

    def test_default_is_enabled_with_empty_chain(self) -> None:
        config = CCProxyConfig()
        assert config.gemini_capacity.enabled is True
        assert config.gemini_capacity.fallback_models == []
        assert config.gemini_capacity.sticky_retry_attempts == 3
        assert config.gemini_capacity.sticky_retry_max_delay_seconds == 60.0
        assert config.gemini_capacity.terminal_delay_threshold_seconds == 300.0
        assert config.gemini_capacity.total_retry_budget_seconds == 120.0

    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "ccproxy.yaml"
        yaml_path.write_text(
            "ccproxy:\n"
            "  gemini_capacity:\n"
            "    enabled: true\n"
            "    fallback_models: [gemini-3-flash-preview, gemini-2.5-pro]\n"
            "    sticky_retry_attempts: 5\n"
            "    sticky_retry_max_delay_seconds: 30\n"
            "    terminal_delay_threshold_seconds: 600\n"
            "    total_retry_budget_seconds: 240\n"
        )
        config = CCProxyConfig.from_yaml(yaml_path)
        assert config.gemini_capacity.enabled is True
        assert config.gemini_capacity.fallback_models == ["gemini-3-flash-preview", "gemini-2.5-pro"]
        assert config.gemini_capacity.sticky_retry_attempts == 5
        assert config.gemini_capacity.sticky_retry_max_delay_seconds == 30.0
        assert config.gemini_capacity.terminal_delay_threshold_seconds == 600.0
        assert config.gemini_capacity.total_retry_budget_seconds == 240.0

    def test_partial_block_keeps_defaults(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "ccproxy.yaml"
        yaml_path.write_text(
            "ccproxy:\n  gemini_capacity:\n    enabled: true\n    fallback_models: [gemini-2.5-flash]\n"
        )
        config = CCProxyConfig.from_yaml(yaml_path)
        assert config.gemini_capacity.enabled is True
        assert config.gemini_capacity.fallback_models == ["gemini-2.5-flash"]
        assert config.gemini_capacity.sticky_retry_attempts == 3

    def test_validation_rejects_negative_attempts(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            GeminiCapacityFallbackConfig(sticky_retry_attempts=-1)

    def test_validation_rejects_zero_max_delay(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            GeminiCapacityFallbackConfig(sticky_retry_max_delay_seconds=0)


class TestOpenAIConversationsTurnIdleTimeout:
    """Tests for ``lightllm.openai_conversations.turn_idle_timeout_seconds`` —
    the named config surface for the session_ws / ws_handoff idle-timeout
    backstop (P-6)."""

    def test_default_is_120_seconds(self) -> None:
        config = CCProxyConfig()
        assert config.lightllm.openai_conversations.turn_idle_timeout_seconds == 120.0

    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "ccproxy.yaml"
        yaml_path.write_text("ccproxy:\n  lightllm:\n    openai_conversations:\n      turn_idle_timeout_seconds: 45\n")
        config = CCProxyConfig.from_yaml(yaml_path)
        assert config.lightllm.openai_conversations.turn_idle_timeout_seconds == 45.0

    def test_validation_rejects_zero(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            OpenAIConversationsConfig(turn_idle_timeout_seconds=0)

    def test_validation_rejects_negative(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            OpenAIConversationsConfig(turn_idle_timeout_seconds=-1)

    def test_lightllm_config_carries_the_block(self) -> None:
        lightllm = LightllmConfig(openai_conversations=OpenAIConversationsConfig(turn_idle_timeout_seconds=30.0))
        assert lightllm.openai_conversations.turn_idle_timeout_seconds == 30.0


class TestOpenAIConversationsProviderDefault:
    """The shipped default template includes a parseable openai_conversations provider."""

    def _load_packaged_config(self) -> CCProxyConfig:
        from importlib.resources import as_file, files

        with as_file(files("ccproxy.templates").joinpath("ccproxy.yaml")) as template_path:
            return CCProxyConfig.from_yaml(Path(template_path))

    def test_packaged_template_routes_openai_conversations(self) -> None:
        """The generated default template carries the openai_conversations provider."""
        config = self._load_packaged_config()
        provider = config.providers.get("openai_conversations")
        assert provider is not None
        assert provider.type == "openai_conversations"
        assert provider.host == "chatgpt.com"
        assert provider.path == "/backend-api/f/conversation"
        assert provider.fingerprint_profile == "chrome136"

    def test_packaged_template_auth_source(self) -> None:
        """The provider's auth parses as an OpenAIConversationsAuthSource with default paths."""
        from ccproxy.auth.sources import OpenAIConversationsAuthSource

        provider = self._load_packaged_config().providers["openai_conversations"]
        assert isinstance(provider.auth, OpenAIConversationsAuthSource)
        assert provider.auth.type == "openai_conversations"
        assert provider.auth.file_path == "~/.config/ccproxy/openai-conversations-credentials.json"
        assert provider.auth.cookie_file == "~/.config/ccproxy/openai-conversations-cookies.txt"

    def test_fingerprint_profile_is_a_valid_curl_cffi_profile(self) -> None:
        """``chrome136`` must be a real curl-cffi impersonate profile, or egress fails fast."""
        from ccproxy.transport import VALID_PROFILES

        assert "chrome136" in VALID_PROFILES
