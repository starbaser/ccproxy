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
