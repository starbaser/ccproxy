"""Configuration management for ccproxy.

Configuration Discovery Precedence (Highest to Lowest Priority):
===============================================================

1. **CCPROXY_CONFIG_DIR Environment Variable** (Highest Priority)
   - Set by CLI or manually: `export CCPROXY_CONFIG_DIR=/path/to/config`
   - Looks for: `${CCPROXY_CONFIG_DIR}/ccproxy.yaml`
   - Use case: Development, testing, custom deployments

2. **LiteLLM Proxy Server Runtime Directory**
   - Automatically detected from proxy_server.config_path
   - Looks for: `{proxy_runtime_dir}/ccproxy.yaml`
   - Use case: Production deployments with LiteLLM proxy

3. **~/.ccproxy Directory** (Fallback)
   - User's home directory default location
   - Looks for: `~/.ccproxy/ccproxy.yaml`
   - Use case: Default user installations

The first existing `ccproxy.yaml` found in this order is used.
If no `ccproxy.yaml` is found, default configuration is applied.

Examples:
--------
# Override with environment variable (highest priority)
export CCPROXY_CONFIG_DIR=/custom/path
litellm --config /custom/path/config.yaml

# Use proxy runtime directory (automatic detection)
litellm --config /etc/litellm/config.yaml
# Will look for /etc/litellm/ccproxy.yaml

# Fallback to user directory
# Will look for ~/.ccproxy/ccproxy.yaml
"""

import importlib
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class StatuslineConfig(BaseModel):
    """Statusline widget configuration (Starship-style)."""

    format: str = "⸢$status⸥"
    """Format string with $status placeholder"""

    symbol: str = ""
    """Symbol/icon prefix (available as $symbol in format)"""

    on: str = "ccproxy: ON"
    """Status text when proxy is active"""

    off: str = "ccproxy: OFF"
    """Status text when proxy is inactive"""

    disabled: bool = False
    """Disable statusline output entirely"""


class OAuthSource(BaseModel):
    """OAuth token source configuration.

    Can be specified as either a simple string (shell command) or
    an object with command and optional user_agent.
    """

    command: str
    """Shell command to retrieve the OAuth token"""

    user_agent: str | None = None
    """Optional custom User-Agent header to send with requests using this token"""


# Import proxy_server to access runtime configuration
try:
    from litellm.proxy import proxy_server
except ImportError:
    # Handle case where proxy_server is not available (e.g., during testing)
    proxy_server = None


class HookConfig:
    """Configuration for a single hook with optional parameters."""

    def __init__(self, hook_path: str, params: dict[str, Any] | None = None) -> None:
        """Initialize a hook configuration.

        Args:
            hook_path: Python import path to the hook function
            params: Optional parameters to pass to the hook via kwargs
        """
        self.hook_path = hook_path
        self.params = params or {}


class RuleConfig:
    """Configuration for a single classification rule."""

    def __init__(self, name: str, rule_path: str, params: list[Any] | None = None) -> None:
        """Initialize a rule configuration.

        Args:
            name: The name for this rule (maps to model_name in LiteLLM config)
            rule_path: Python import path to the rule class
            params: Optional parameters to pass to the rule constructor
        """
        self.model_name = name
        self.rule_path = rule_path
        self.params = params or []

    def create_instance(self) -> Any:
        """Create an instance of the rule class.

        Returns:
            An instance of the ClassificationRule

        Raises:
            ImportError: If the rule class cannot be imported
            TypeError: If the rule class cannot be instantiated with provided params
        """
        # Import the rule class
        module_path, class_name = self.rule_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        rule_class = getattr(module, class_name)

        # Create instance with parameters
        if not self.params:
            # No parameters
            return rule_class()

        if isinstance(self.params, list):
            # If all params are dicts, assume they're kwargs
            if all(isinstance(p, dict) for p in self.params):
                # Merge all dicts into one kwargs dict
                kwargs = {}
                for p in self.params:
                    kwargs.update(p)
                return rule_class(**kwargs)
            # Otherwise treat as positional args
            return rule_class(*self.params)
        if isinstance(self.params, dict):  # type: ignore[unreachable]
            # Single dict of kwargs
            return rule_class(**self.params)
        # Single positional arg
        return rule_class(self.params)


class CCProxyConfig(BaseSettings):
    """Main configuration for ccproxy that reads from ccproxy.yaml."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
    )

    # Core settings
    debug: bool = False
    metrics_enabled: bool = True
    default_model_passthrough: bool = True

    # Handler import path (e.g., "ccproxy.handler:CCProxyHandler")
    handler: str = "ccproxy.handler:CCProxyHandler"

    # Statusline configuration
    statusline: StatuslineConfig = Field(default_factory=StatuslineConfig)

    # OAuth token sources - dict mapping provider name to shell command or OAuthSource
    # Example: {"anthropic": "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"}
    # Extended: {"gemini": {"command": "jq -r '.token' ~/.gemini/creds.json", "user_agent": "MyApp/1.0"}}
    oat_sources: dict[str, str | OAuthSource] = Field(default_factory=dict)

    # Cached OAuth tokens (loaded at startup) - dict mapping provider name to token
    _oat_values: dict[str, str] = PrivateAttr(default_factory=dict)

    # Cached OAuth user agents (loaded at startup) - dict mapping provider name to user-agent
    _oat_user_agents: dict[str, str] = PrivateAttr(default_factory=dict)

    # Hook configurations (function import paths or dict with params)
    hooks: list[str | dict[str, Any]] = Field(default_factory=list)

    # Rule configurations
    rules: list[RuleConfig] = Field(default_factory=list)

    # Path to ccproxy config
    ccproxy_config_path: Path = Field(default_factory=lambda: Path("./ccproxy.yaml"))

    # Path to LiteLLM config (for model lookups)
    litellm_config_path: Path = Field(default_factory=lambda: Path("./config.yaml"))

    @property
    def oat_values(self) -> dict[str, str]:
        """Get the cached OAuth token values.

        Returns:
            Dict mapping provider name to OAuth token
        """
        return self._oat_values

    def get_oauth_token(self, provider: str) -> str | None:
        """Get OAuth token for a specific provider.

        Args:
            provider: Provider name (e.g., "anthropic", "gemini")

        Returns:
            OAuth token string or None if not configured for this provider
        """
        return self._oat_values.get(provider)

    def get_oauth_user_agent(self, provider: str) -> str | None:
        """Get custom User-Agent for a specific provider.

        Args:
            provider: Provider name (e.g., "anthropic", "gemini")

        Returns:
            Custom User-Agent string or None if not configured for this provider
        """
        return self._oat_user_agents.get(provider)

    def _load_credentials(self) -> None:
        """Execute shell commands to load OAuth tokens for all configured providers at startup.

        Raises:
            RuntimeError: If any shell command fails to execute or returns empty token
        """
        if not self.oat_sources:
            # No OAuth sources configured
            self._oat_values = {}
            self._oat_user_agents = {}
            return

        loaded_tokens = {}
        loaded_user_agents = {}
        errors = []

        for provider, source in self.oat_sources.items():
            # Normalize to OAuthSource for consistent handling
            if isinstance(source, str):
                oauth_source = OAuthSource(command=source)
            elif isinstance(source, OAuthSource):
                oauth_source = source
            elif isinstance(source, dict):
                # Handle dict from YAML
                oauth_source = OAuthSource(**source)
            else:
                error_msg = f"Invalid OAuth source type for provider '{provider}': {type(source)}"
                logger.error(error_msg)
                errors.append(error_msg)
                continue

            try:
                # Execute shell command
                result = subprocess.run(  # noqa: S602
                    oauth_source.command,
                    shell=True,  # Intentional: command is user-configured
                    capture_output=True,
                    text=True,
                    timeout=5,  # 5 second timeout
                )

                if result.returncode != 0:
                    error_msg = (
                        f"OAuth command for provider '{provider}' failed with exit code "
                        f"{result.returncode}: {result.stderr.strip()}"
                    )
                    logger.error(error_msg)
                    errors.append(error_msg)
                    continue

                token = result.stdout.strip()
                if not token:
                    error_msg = f"OAuth command for provider '{provider}' returned empty output"
                    logger.error(error_msg)
                    errors.append(error_msg)
                    continue

                loaded_tokens[provider] = token
                logger.debug(f"Successfully loaded OAuth token for provider '{provider}'")

                # Store user-agent if specified
                if oauth_source.user_agent:
                    loaded_user_agents[provider] = oauth_source.user_agent
                    logger.debug(f"Loaded custom User-Agent for provider '{provider}': {oauth_source.user_agent}")

            except subprocess.TimeoutExpired:
                error_msg = f"OAuth command for provider '{provider}' timed out after 5 seconds"
                logger.error(error_msg)
                errors.append(error_msg)
            except Exception as e:
                error_msg = f"Failed to execute OAuth command for provider '{provider}': {e}"
                logger.error(error_msg)
                errors.append(error_msg)

        # Store successfully loaded tokens and user-agents
        self._oat_values = loaded_tokens
        self._oat_user_agents = loaded_user_agents

        # If we had errors but successfully loaded some tokens, log warning
        if errors and loaded_tokens:
            logger.warning(
                f"Loaded OAuth tokens for {len(loaded_tokens)} provider(s), "
                f"but {len(errors)} provider(s) failed to load"
            )

        # If all providers failed, raise error
        if errors and not loaded_tokens:
            raise RuntimeError(
                f"Failed to load OAuth tokens for all {len(self.oat_sources)} provider(s):\n"
                + "\n".join(f"  - {err}" for err in errors)
            )

    def load_hooks(self) -> list[tuple[Any, dict[str, Any]]]:
        """Load hook functions from their import paths.

        Returns:
            List of (hook_function, params) tuples

        Raises:
            ImportError: If a hook cannot be imported
        """
        loaded_hooks = []
        for hook_entry in self.hooks:
            # Parse hook entry (string or dict format)
            if isinstance(hook_entry, str):
                hook_path = hook_entry
                params: dict[str, Any] = {}
            elif isinstance(hook_entry, dict):
                hook_path = hook_entry.get("hook", "")
                params = hook_entry.get("params", {})
                if not hook_path:
                    logger.error(f"Hook entry missing 'hook' key: {hook_entry}")
                    continue
            else:
                logger.error(f"Invalid hook entry type: {type(hook_entry)}")
                continue

            try:
                # Import the hook function
                module_path, func_name = hook_path.rsplit(".", 1)
                module = importlib.import_module(module_path)
                hook_func = getattr(module, func_name)
                loaded_hooks.append((hook_func, params))
                logger.debug(f"Loaded hook: {hook_path}" + (f" with params: {params}" if params else ""))
            except (ImportError, AttributeError) as e:
                logger.error(f"Failed to load hook {hook_path}: {e}")
                # Continue loading other hooks even if one fails
        return loaded_hooks

    @classmethod
    def from_proxy_runtime(cls, **kwargs: Any) -> "CCProxyConfig":
        """Load configuration from ccproxy.yaml file in the same directory as config.yaml.

        This method looks for ccproxy.yaml in the same directory as the LiteLLM config.
        """
        # Create instance with defaults
        instance = cls(**kwargs)

        # Try to find ccproxy.yaml in the same directory as config.yaml
        config_dir = instance.litellm_config_path.parent
        ccproxy_yaml_path = config_dir / "ccproxy.yaml"

        if ccproxy_yaml_path.exists():
            instance = cls.from_yaml(ccproxy_yaml_path, **kwargs)

        return instance

    @classmethod
    def from_yaml(cls, yaml_path: Path, **kwargs: Any) -> "CCProxyConfig":
        """Load configuration from ccproxy.yaml file.

        Args:
            yaml_path: Path to the ccproxy.yaml file
            **kwargs: Additional keyword arguments

        Returns:
            CCProxyConfig instance

        Raises:
            RuntimeError: If credentials shell command fails during startup
        """
        instance = cls(ccproxy_config_path=yaml_path, **kwargs)

        # Load YAML if it exists
        if yaml_path.exists():
            with yaml_path.open() as f:
                data = yaml.safe_load(f) or {}

                # Get ccproxy section
                ccproxy_data = data.get("ccproxy", {})

                # Apply basic settings
                if "debug" in ccproxy_data:
                    instance.debug = ccproxy_data["debug"]
                if "metrics_enabled" in ccproxy_data:
                    instance.metrics_enabled = ccproxy_data["metrics_enabled"]
                if "default_model_passthrough" in ccproxy_data:
                    instance.default_model_passthrough = ccproxy_data["default_model_passthrough"]
                if "oat_sources" in ccproxy_data:
                    instance.oat_sources = ccproxy_data["oat_sources"]

                # Load statusline configuration
                if "statusline" in ccproxy_data:
                    statusline_data = ccproxy_data["statusline"]
                    if isinstance(statusline_data, dict):
                        instance.statusline = StatuslineConfig(**statusline_data)
                    else:
                        logger.warning(f"Invalid statusline config format: {type(statusline_data)}")

                # Backwards compatibility: migrate deprecated 'credentials' field
                if "credentials" in ccproxy_data:
                    logger.error(
                        "DEPRECATED: The 'credentials' field is deprecated and will be removed in a future version. "
                        "Please migrate to 'oat_sources' in your ccproxy.yaml configuration. "
                        "Example:\n"
                        "  oat_sources:\n"
                        "    anthropic: \"jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json\"\n"
                        "The deprecated 'credentials' field has been automatically migrated to "
                        "oat_sources['anthropic'] for this session."
                    )
                    # Migrate credentials to oat_sources for anthropic provider
                    if "anthropic" not in instance.oat_sources:
                        instance.oat_sources["anthropic"] = ccproxy_data["credentials"]
                    else:
                        logger.warning(
                            "Both 'credentials' and 'oat_sources[\"anthropic\"]' are configured. "
                            "Using 'oat_sources[\"anthropic\"]' and ignoring deprecated 'credentials' field."
                        )

                # Load hooks
                hooks_data = ccproxy_data.get("hooks", [])
                if hooks_data:
                    instance.hooks = hooks_data

                # Load rules
                rules_data = ccproxy_data.get("rules", [])
                instance.rules = []
                for rule_data in rules_data:
                    if isinstance(rule_data, dict):
                        name = rule_data.get("name", "")
                        rule_path = rule_data.get("rule", "")
                        params = rule_data.get("params", [])
                        if name and rule_path:
                            rule_config = RuleConfig(name, rule_path, params)
                            instance.rules.append(rule_config)

        # Load credentials at startup (raises RuntimeError if fails)
        instance._load_credentials()

        return instance


# Global configuration instance
_config_instance: CCProxyConfig | None = None
_config_lock = threading.Lock()


def get_config() -> CCProxyConfig:
    """Get the configuration instance."""
    global _config_instance

    if _config_instance is None:
        with _config_lock:
            # Double-check locking pattern
            if _config_instance is None:
                # Configuration discovery precedence:
                # 1. CCPROXY_CONFIG_DIR environment variable (highest priority)
                # 2. LiteLLM proxy server runtime directory
                # 3. ~/.ccproxy directory (fallback)

                import os

                config_path = None
                config_source = None

                # Priority 1: Environment variable
                env_config_dir = os.environ.get("CCPROXY_CONFIG_DIR")
                if env_config_dir:
                    config_path = Path(env_config_dir)
                    config_source = f"ENV:CCPROXY_CONFIG_DIR={env_config_dir}"
                    logger.info(f"Using config directory from environment: {config_path}")
                else:
                    # Priority 2: LiteLLM proxy server runtime directory
                    try:
                        from litellm.proxy import proxy_server

                        if proxy_server and hasattr(proxy_server, "config_path") and proxy_server.config_path:
                            config_path = Path(proxy_server.config_path).parent
                            config_source = f"PROXY_RUNTIME:{config_path}"
                            logger.info(f"Using config directory from proxy runtime: {config_path}")
                    except ImportError:
                        logger.debug("LiteLLM proxy server not available for config discovery")

                if config_path:
                    # Try to load ccproxy.yaml from discovered path
                    ccproxy_yaml_path = config_path / "ccproxy.yaml"
                    if ccproxy_yaml_path.exists():
                        logger.info(f"Loading ccproxy config from: {ccproxy_yaml_path} (source: {config_source})")
                        _config_instance = CCProxyConfig.from_yaml(ccproxy_yaml_path)
                        _config_instance.litellm_config_path = config_path / "config.yaml"
                    else:
                        logger.info(
                            f"ccproxy.yaml not found at {ccproxy_yaml_path}, using default config "
                            f"(source: {config_source})"
                        )
                        # Create default config with proper paths
                        _config_instance = CCProxyConfig(
                            litellm_config_path=config_path / "config.yaml", ccproxy_config_path=ccproxy_yaml_path
                        )
                else:
                    # Priority 3: Fallback to ~/.ccproxy directory
                    fallback_config_dir = Path.home() / ".ccproxy"
                    ccproxy_path = fallback_config_dir / "ccproxy.yaml"
                    if ccproxy_path.exists():
                        logger.info(f"Using fallback config directory: {fallback_config_dir}")
                        _config_instance = CCProxyConfig.from_yaml(ccproxy_path)
                        _config_instance.litellm_config_path = fallback_config_dir / "config.yaml"
                    else:
                        logger.info("No ccproxy.yaml found in any location, using proxy runtime defaults")
                        # Use from_proxy_runtime which will look for ccproxy.yaml
                        # in the same directory as config.yaml
                        _config_instance = CCProxyConfig.from_proxy_runtime()

    return _config_instance


def set_config_instance(config: CCProxyConfig) -> None:
    """Set the global configuration instance (for testing)."""
    global _config_instance
    _config_instance = config


def clear_config_instance() -> None:
    """Clear the global configuration instance (for testing)."""
    global _config_instance
    _config_instance = None
