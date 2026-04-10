"""Tests for configuration management."""

import tempfile
from pathlib import Path
from unittest import mock

from ccproxy.config import (
    CCProxyConfig,
    clear_config_instance,
    get_config,
)


class TestCCProxyConfig:
    """Tests for main config class."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = CCProxyConfig()
        assert config.debug is False
        assert config.litellm_config_path == Path("./config.yaml")
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

            # Should use defaults
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

            # Both hook formats should be in hooks list
            assert len(config.hooks) == 2
            assert config.hooks[0] == "ccproxy.hooks.rule_evaluator"
            assert config.hooks[1] == {
                "hook": "ccproxy.hooks.capture_headers",
                "params": {"headers": ["user-agent", "x-request-id"]},
            }

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


class TestProxyRuntimeConfig:
    """Tests for loading configuration from proxy_server runtime."""

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

    def test_config_from_runtime(self) -> None:
        """Test loading configuration from proxy_server runtime."""
        config = CCProxyConfig.from_proxy_runtime()

        # Config should be created successfully
        assert config is not None
        # Model lookup functionality has been moved to router.py

    def test_get_config_uses_runtime_when_available(self) -> None:
        """Test that get_config prefers runtime config when available."""
        clear_config_instance()

        ccproxy_yaml_content = """
ccproxy:
  debug: true
"""

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            config_yaml = temp_path / "config.yaml"
            config_yaml.write_text("model_list: []")

            ccproxy_yaml = temp_path / "ccproxy.yaml"
            ccproxy_yaml.write_text(ccproxy_yaml_content)

            import os

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

                # All threads should get the same instance
                assert len(config_ids) == 1
            finally:
                os.chdir(original_cwd)
                clear_config_instance()
