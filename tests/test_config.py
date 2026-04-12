"""Tests for configuration management."""

import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from ccproxy.config import (
    CCProxyConfig,
    CredentialSource,
    OAuthSource,
    _read_credential_file,
    _run_credential_command,
    clear_config_instance,
    get_config,
)


class TestCCProxyConfig:
    """Tests for main config class."""

    def test_default_config(self, monkeypatch: mock.MagicMock) -> None:
        """Test default configuration values."""
        monkeypatch.delenv("CCPROXY_HOST", raising=False)
        monkeypatch.delenv("CCPROXY_PORT", raising=False)
        config = CCProxyConfig()
        assert config.debug is False
        assert config.host == "127.0.0.1"
        assert config.port == 4000
        assert config.ccproxy_config_path == Path("./ccproxy.yaml")

    def test_config_attributes(self) -> None:
        """Test config attributes can be set directly."""
        config = CCProxyConfig()
        config.debug = True
        assert config.debug is True

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

            assert config.debug is False

        finally:
            yaml_path.unlink()

    def test_hook_parameters_from_yaml(self) -> None:
        """Test that hooks with parameters are loaded correctly."""
        yaml_content = """
ccproxy:
  debug: false
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


class TestConfigSingleton:
    """Tests for configuration singleton functions."""

    def test_get_config_singleton(self) -> None:
        """Test that get_config returns the same instance."""
        clear_config_instance()

        # Create a custom config instance and set it directly
        custom_config = CCProxyConfig(debug=True)
        from ccproxy.config import set_config_instance

        set_config_instance(custom_config)

        try:
            config1 = get_config()
            config2 = get_config()

            assert config1 is config2
            assert config1.debug is True

        finally:
            clear_config_instance()

    def test_get_config_uses_ccproxy_yaml(self) -> None:
        """Test that get_config reads settings from ccproxy.yaml."""
        clear_config_instance()

        ccproxy_yaml_content = """
ccproxy:
  debug: true
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
                    assert config.debug is True
            finally:
                os.chdir(original_cwd)

        clear_config_instance()


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
  debug: true
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

    def test_exception_returns_none(self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_non_zero_exit_returns_none(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        mock_result = mock.MagicMock(returncode=127, stderr=" command not found \n")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))
        assert _run_credential_command("badcmd", "TestCmd") is None
        assert "TestCmd command failed (exit 127)" in caplog.text

    def test_empty_stdout_returns_none(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="\n   \n")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))
        assert _run_credential_command("echo", "TestCmd") is None
        assert "TestCmd command returned empty output" in caplog.text

    def test_timeout_expired_returns_none(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        def mock_run_timeout(*args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="sleep", timeout=5)

        monkeypatch.setattr(subprocess, "run", mock_run_timeout)
        assert _run_credential_command("sleep 10", "TestCmd") is None
        assert "TestCmd command timed out after 5 seconds" in caplog.text

    def test_other_exception_returns_none(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
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
        config = CCProxyConfig(oat_sources={"provider1": "echo new-token"})
        config._oat_values["provider1"] = "old-token"
        mock_result = mock.MagicMock(returncode=0, stdout="new-token")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))

        token, changed = config.refresh_oauth_token("provider1")

        assert token == "new-token"
        assert changed is True
        assert config._oat_values["provider1"] == "new-token"

    def test_token_unchanged_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = CCProxyConfig(oat_sources={"provider1": "echo current-token"})
        config._oat_values["provider1"] = "current-token"
        mock_result = mock.MagicMock(returncode=0, stdout="current-token")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))

        token, changed = config.refresh_oauth_token("provider1")

        assert token == "current-token"
        assert changed is False

    def test_provider_not_configured_returns_none(self) -> None:
        config = CCProxyConfig()
        token, changed = config.refresh_oauth_token("missing-provider")
        assert token is None
        assert changed is False

    def test_user_agent_stored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = CCProxyConfig(oat_sources={
            "provider1": OAuthSource(command="echo tok", user_agent="CustomAgent/1.0")
        })
        mock_result = mock.MagicMock(returncode=0, stdout="tok")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))

        config.refresh_oauth_token("provider1")

        assert config._oat_user_agents.get("provider1") == "CustomAgent/1.0"


class TestGetAuthProviderUA:
    def test_returns_stored_user_agent(self) -> None:
        config = CCProxyConfig()
        config._oat_user_agents["prov"] = "TestAgent/1.0"
        assert config.get_auth_provider_ua("prov") == "TestAgent/1.0"

    def test_returns_none_for_unknown_provider(self) -> None:
        config = CCProxyConfig()
        assert config.get_auth_provider_ua("unknown") is None


class TestGetAuthHeader:
    def test_oauth_source_with_auth_header(self) -> None:
        config = CCProxyConfig(oat_sources={
            "prov": OAuthSource(command="echo t", auth_header="x-api-key")
        })
        assert config.get_auth_header("prov") == "x-api-key"

    def test_string_source_returns_none(self) -> None:
        config = CCProxyConfig(oat_sources={"prov": "echo token"})
        assert config.get_auth_header("prov") is None

    def test_missing_provider_returns_none(self) -> None:
        config = CCProxyConfig()
        assert config.get_auth_header("unknown") is None


class TestGetProviderForDestination:
    def test_none_api_base_returns_none(self) -> None:
        config = CCProxyConfig()
        assert config.get_provider_for_destination(None) is None

    def test_empty_api_base_returns_none(self) -> None:
        config = CCProxyConfig()
        assert config.get_provider_for_destination("") is None

    def test_matching_destination_case_insensitive(self) -> None:
        config = CCProxyConfig(oat_sources={
            "anthropic": OAuthSource(command="cmd", destinations=["api.anthropic.com"])
        })
        assert config.get_provider_for_destination("https://API.ANTHROPIC.COM/v1") == "anthropic"

    def test_no_matching_destination_returns_none(self) -> None:
        config = CCProxyConfig(oat_sources={
            "anthropic": OAuthSource(command="cmd", destinations=["api.anthropic.com"])
        })
        assert config.get_provider_for_destination("api.openai.com") is None

    def test_string_source_skipped(self) -> None:
        config = CCProxyConfig(oat_sources={"prov": "echo tok"})
        assert config.get_provider_for_destination("api.test.com") is None

    def test_dict_source_matching(self) -> None:
        config = CCProxyConfig(oat_sources={
            "prov": {"command": "echo t", "destinations": ["api.z.ai"]}
        })
        assert config.get_provider_for_destination("https://api.z.ai/v1") == "prov"


class TestLoadCredentials:
    def test_empty_oat_sources_clears_values(self) -> None:
        config = CCProxyConfig()
        config._oat_values = {"stale": "data"}
        config._load_credentials()
        assert config._oat_values == {}
        assert config._oat_user_agents == {}

    def test_single_provider_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = CCProxyConfig(oat_sources={"prov1": "echo tok1"})
        mock_result = mock.MagicMock(returncode=0, stdout="tok1")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))

        config._load_credentials()

        assert config._oat_values["prov1"] == "tok1"

    def test_partial_failure_logs_warning(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        config = CCProxyConfig(oat_sources={"prov1": "echo tok1", "prov2": "fail"})

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

        assert config._oat_values == {"prov1": "tok1"}
        assert "but 1 provider(s) failed to load" in caplog.text

    def test_all_providers_fail_logs_error(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        config = CCProxyConfig(oat_sources={"prov1": "fail1", "prov2": "fail2"})
        mock_result = mock.MagicMock(returncode=1, stderr="err")
        monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=mock_result))

        config._load_credentials()

        assert config._oat_values == {}
        assert "Failed to load OAuth tokens for all 2 provider(s)" in caplog.text
