"""Tests for configuration management."""

import tempfile
from pathlib import Path
from unittest import mock

from ccproxy.config import (
    CCProxyConfig,
    RuleConfig,
    clear_config_instance,
    get_config,
)


class TestCCProxyConfig:
    """Tests for main config class."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = CCProxyConfig()
        assert config.debug is False
        assert config.metrics_enabled is True
        assert config.litellm_config_path == Path("./config.yaml")
        assert config.ccproxy_config_path == Path("./ccproxy.yaml")
        assert config.rules == []

    def test_config_attributes(self) -> None:
        """Test config attributes can be set directly."""
        config = CCProxyConfig()
        config.debug = True
        config.metrics_enabled = False
        assert config.debug is True
        assert config.metrics_enabled is False

    def test_rule_config(self) -> None:
        """Test rule configuration."""
        # Create a rule config
        rule = RuleConfig("test_name", "ccproxy.rules.TokenCountRule", [{"threshold": 5000}])
        assert rule.model_name == "test_name"
        assert rule.rule_path == "ccproxy.rules.TokenCountRule"
        assert rule.params == [{"threshold": 5000}]

        # Create instance
        instance = rule.create_instance()
        from ccproxy.rules import TokenCountRule

        assert isinstance(instance, TokenCountRule)

    def test_from_yaml_files(self) -> None:
        """Test loading configuration from ccproxy.yaml."""
        ccproxy_yaml_content = """
ccproxy:
  debug: true
  metrics_enabled: false
  rules:
    - name: token_count
      rule: ccproxy.rules.TokenCountRule
      params:
        - threshold: 80000
    - name: background
      rule: ccproxy.rules.MatchModelRule
      params:
        - model_name: claude-haiku-4-5-20251001
"""
        litellm_yaml_content = """
model_list:
  - model_name: default
    litellm_params:
      model: claude-sonnet-4-5-20250929
  - model_name: background
    litellm_params:
      model: claude-haiku-4-5-20251001-20241022
  - model_name: think
    litellm_params:
      model: claude-opus-4-1-20250805
  - model_name: token_count
    litellm_params:
      model: gemini-2.5-pro
  - model_name: web_search
    litellm_params:
      model: perplexity/llama-3.1-sonar-large-128k-online
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as ccproxy_file:
            ccproxy_file.write(ccproxy_yaml_content)
            ccproxy_path = Path(ccproxy_file.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as litellm_file:
            litellm_file.write(litellm_yaml_content)
            litellm_path = Path(litellm_file.name)

        try:
            config = CCProxyConfig.from_yaml(ccproxy_path, litellm_config_path=litellm_path)

            # Check ccproxy settings
            assert config.debug is True
            assert config.metrics_enabled is False
            assert len(config.rules) == 2
            assert config.rules[0].model_name == "token_count"
            assert config.rules[1].model_name == "background"

            # Model lookup functionality has been moved to router.py

        finally:
            ccproxy_path.unlink()
            litellm_path.unlink()

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

            # Should use defaults
            assert config.debug is False
            assert config.metrics_enabled is True
            assert config.rules == []

        finally:
            yaml_path.unlink()

    def test_yaml_config_values(self) -> None:
        """Test that YAML config values are loaded correctly."""
        yaml_content = """
ccproxy:
  debug: true
  metrics_enabled: false
  rules:
    - name: custom_rule
      rule: ccproxy.rules.TokenCountRule
      params:
        - threshold: 70000
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)
            # YAML values should be loaded
            assert config.debug is True
            assert config.metrics_enabled is False
            assert len(config.rules) == 1
            assert config.rules[0].model_name == "custom_rule"
            assert config.rules[0].params == [{"threshold": 70000}]

        finally:
            yaml_path.unlink()

    def test_model_loading_from_yaml(self) -> None:
        """Test that model configuration can be loaded from YAML files."""
        litellm_yaml_content = """
model_list:
  - model_name: default
    litellm_params:
      model: gpt-4
  - model_name: background
    litellm_params:
      model: gpt-3.5-turbo
"""
        ccproxy_yaml_content = """
ccproxy:
  debug: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as litellm_file:
            litellm_file.write(litellm_yaml_content)
            litellm_path = Path(litellm_file.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as ccproxy_file:
            ccproxy_file.write(ccproxy_yaml_content)
            ccproxy_path = Path(ccproxy_file.name)

        try:
            config = CCProxyConfig.from_yaml(ccproxy_path, litellm_config_path=litellm_path)

            # Config should have the litellm_config_path set
            assert config.litellm_config_path == litellm_path
            # Model lookup functionality has been moved to router.py

        finally:
            litellm_path.unlink()
            ccproxy_path.unlink()


class TestConfigSingleton:
    """Tests for configuration singleton functions."""

    def test_get_config_singleton(self) -> None:
        """Test that get_config returns the same instance."""
        # Clear any existing instance
        clear_config_instance()

        # Create a custom config instance and set it directly
        custom_config = CCProxyConfig(debug=True, metrics_enabled=False)
        from ccproxy.config import set_config_instance

        set_config_instance(custom_config)

        try:
            config1 = get_config()
            config2 = get_config()

            assert config1 is config2
            assert config1.debug is True
            assert config1.metrics_enabled is False

        finally:
            clear_config_instance()


class TestProxyRuntimeConfig:
    """Tests for loading configuration from proxy_server runtime."""

    def test_from_proxy_runtime_with_ccproxy_yaml(self) -> None:
        """Test loading config from ccproxy.yaml in the same directory as config.yaml."""
        # Create a temp directory with config.yaml and ccproxy.yaml
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create config.yaml (LiteLLM config)
            config_yaml = temp_path / "config.yaml"
            config_yaml.write_text("""
model_list:
  - model_name: default
    litellm_params:
      model: gpt-4
""")

            # Create ccproxy.yaml in same directory
            ccproxy_yaml = temp_path / "ccproxy.yaml"
            ccproxy_yaml.write_text("""
ccproxy:
  debug: true
  metrics_enabled: false
  rules:
    - name: test
      rule: ccproxy.rules.TokenCountRule
      params:
        - threshold: 75000
""")

            # Mock Path("config.yaml") to return our temp config.yaml
            with mock.patch("ccproxy.config.Path") as mock_path:
                mock_path.return_value = config_yaml
                config = CCProxyConfig.from_proxy_runtime()

                assert config.debug is True
                assert config.metrics_enabled is False
                assert len(config.rules) == 1
                assert config.rules[0].model_name == "test"

    def test_from_proxy_runtime_without_ccproxy_yaml(self) -> None:
        """Test loading config when ccproxy.yaml doesn't exist."""
        # Create a temporary directory without ccproxy.yaml
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_yaml = temp_path / "config.yaml"
            config_yaml.write_text("model_list: []")

            # Mock Path("config.yaml") to return our temp config.yaml
            with mock.patch("ccproxy.config.Path") as mock_path:
                mock_path.return_value = config_yaml
                config = CCProxyConfig.from_proxy_runtime()

                # Should use defaults
                assert config.debug is False
                assert config.metrics_enabled is True
                assert config.rules == []

    def test_from_proxy_runtime_default_paths(self) -> None:
        """Test loading config with default paths."""
        # Create paths that don't exist
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_yaml = temp_path / "config.yaml"  # Don't create it

            # Mock Path to return our non-existent config.yaml
            with mock.patch("ccproxy.config.Path") as mock_path:
                mock_path.return_value = config_yaml
                config = CCProxyConfig.from_proxy_runtime()

                # Should use defaults
                assert config.debug is False
                assert config.metrics_enabled is True
                assert config.rules == []

    def test_config_from_runtime(self) -> None:
        """Test loading configuration from proxy_server runtime."""
        # Mock proxy_server
        mock_proxy_server = mock.MagicMock()
        mock_proxy_server.general_settings = {}
        mock_proxy_server.llm_router = mock.MagicMock()
        mock_proxy_server.llm_router.model_list = [
            {
                "model_name": "default",
                "litellm_params": {
                    "model": "anthropic/claude-sonnet-4-5-20250929",
                    "api_base": "https://api.anthropic.com",
                },
            },
            {
                "model_name": "background",
                "litellm_params": {
                    "model": "anthropic/claude-haiku-4-5-20251001-20241022",
                    "api_base": "https://api.anthropic.com",
                },
            },
        ]

        with mock.patch("ccproxy.config.proxy_server", mock_proxy_server):
            config = CCProxyConfig.from_proxy_runtime()

            # Config should be created successfully
            assert config is not None
            # Model lookup functionality has been moved to router.py

    def test_get_config_uses_runtime_when_available(self) -> None:
        """Test that get_config prefers runtime config when available."""
        # Clear any existing instance
        clear_config_instance()

        # Mock proxy_server
        mock_proxy_server = mock.MagicMock()
        mock_proxy_server.general_settings = {}

        # Create temporary ccproxy.yaml
        ccproxy_yaml_content = """
ccproxy:
  debug: true
  rules:
    - name: runtime_test
      rule: ccproxy.rules.TokenCountRule
      params:
        - threshold: 90000
"""

        # Create a temp directory for the config files
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create config.yaml
            config_yaml = temp_path / "config.yaml"
            config_yaml.write_text("model_list: []")

            # Create ccproxy.yaml
            ccproxy_yaml = temp_path / "ccproxy.yaml"
            ccproxy_yaml.write_text(ccproxy_yaml_content)

            # Change to the temp directory so ./ccproxy.yaml exists
            import os

            original_cwd = Path.cwd()
            os.chdir(temp_dir)

            try:
                # Set environment variable to point to test directory
                with (
                    mock.patch("ccproxy.config.proxy_server", mock_proxy_server),
                    mock.patch.dict(os.environ, {"CCPROXY_CONFIG_DIR": temp_dir}),
                ):
                    config = get_config()
                    assert config.debug is True
                    assert len(config.rules) == 1
                    assert config.rules[0].params == [{"threshold": 90000}]
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

        # Clear any existing instance
        clear_config_instance()

        yaml_content = """
ccproxy:
  debug: true
  rules:
    - name: concurrent_test
      rule: ccproxy.rules.TokenCountRule
      params:
        - threshold: 50000
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            ccproxy_path = Path(temp_dir) / "ccproxy.yaml"
            ccproxy_path.write_text(yaml_content)

            # Change to temp directory so ./ccproxy.yaml exists
            original_cwd = Path.cwd()
            os.chdir(temp_dir)

            try:
                # Track which thread created the config
                config_ids: set[int] = set()
                lock = threading.Lock()

                def get_and_track() -> None:
                    config = get_config()
                    with lock:
                        config_ids.add(id(config))

                # Run multiple threads
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = [executor.submit(get_and_track) for _ in range(50)]
                    concurrent.futures.wait(futures)

                # All threads should get the same instance
                assert len(config_ids) == 1
            finally:
                os.chdir(original_cwd)
                clear_config_instance()


class TestCredentialsLoading:
    """Tests for credentials loading at config startup."""

    def test_credentials_loaded_at_startup_success(self) -> None:
        """Test that credentials are loaded successfully during config initialization."""
        yaml_content = """
ccproxy:
  credentials: echo 'test-token-123'
  debug: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            # Credentials should be loaded and cached
            assert config.credentials_value == "test-token-123"
            assert config.credentials == "echo 'test-token-123'"

        finally:
            yaml_path.unlink()

    def test_credentials_loaded_with_whitespace_stripped(self) -> None:
        """Test that whitespace is stripped from credentials output."""
        yaml_content = """
ccproxy:
  credentials: echo '  token-with-spaces  '
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)
            assert config.credentials_value == "token-with-spaces"

        finally:
            yaml_path.unlink()

    def test_credentials_shell_command_failure(self) -> None:
        """Test that config loading fails when credentials shell command fails."""
        yaml_content = """
ccproxy:
  credentials: exit 1
  debug: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            # Should raise RuntimeError when shell command fails
            import pytest

            with pytest.raises(RuntimeError, match="Credentials shell command failed with exit code 1"):
                CCProxyConfig.from_yaml(yaml_path)

        finally:
            yaml_path.unlink()

    def test_credentials_shell_command_empty_output(self) -> None:
        """Test that config loading fails when credentials shell command returns empty output."""
        yaml_content = """
ccproxy:
  credentials: echo -n ''
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            # Should raise RuntimeError when output is empty
            import pytest

            with pytest.raises(RuntimeError, match="Credentials shell command returned empty output"):
                CCProxyConfig.from_yaml(yaml_path)

        finally:
            yaml_path.unlink()

    def test_credentials_shell_command_timeout(self) -> None:
        """Test that config loading fails when credentials shell command times out."""
        yaml_content = """
ccproxy:
  credentials: sleep 10
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            # Should raise RuntimeError when command times out
            import pytest

            with pytest.raises(RuntimeError, match="Credentials shell command timed out after 5 seconds"):
                CCProxyConfig.from_yaml(yaml_path)

        finally:
            yaml_path.unlink()

    def test_credentials_not_configured(self) -> None:
        """Test that config loads successfully when no credentials configured."""
        yaml_content = """
ccproxy:
  debug: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            # Should load successfully with no credentials
            assert config.credentials is None
            assert config.credentials_value is None

        finally:
            yaml_path.unlink()

    def test_credentials_value_property_readonly(self) -> None:
        """Test that credentials_value is accessible via property."""
        config = CCProxyConfig(credentials=None)
        config._credentials_value = "cached-token"

        # Should be accessible via property
        assert config.credentials_value == "cached-token"

    def test_credentials_cached_once(self) -> None:
        """Test that credentials are cached and not re-executed."""
        yaml_content = """
ccproxy:
  credentials: echo 'initial-token'
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            # Get the cached value
            first_value = config.credentials_value
            assert first_value == "initial-token"

            # Accessing again should return same cached value
            second_value = config.credentials_value
            assert second_value == first_value

        finally:
            yaml_path.unlink()
