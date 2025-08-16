"""End-to-end integration tests for Claude Code with ccproxy.

This test suite validates that the `claude` command works correctly when routed through ccproxy.
"""

import tempfile
from pathlib import Path
import subprocess
import os
import pytest
import yaml
import time
from typing import Generator
import socket
from contextlib import closing
from unittest.mock import patch, MagicMock


def find_free_port() -> int:
    """Find a free port to use for testing."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


@pytest.mark.skipif(
    subprocess.run(["which", "claude"], capture_output=True).returncode != 0,
    reason="claude command not available"
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
                            "model": "claude-3-5-sonnet-20241022",
                            "api_base": "https://api.anthropic.com"
                        }
                    }
                ]
            }
            
            # Create minimal ccproxy config
            ccproxy_config = {
                "litellm": {
                    "host": "127.0.0.1",
                    "port": find_free_port(),
                    "num_workers": 1,
                    "telemetry": False
                },
                "ccproxy": {
                    "debug": False,
                    "hooks": [
                        "ccproxy.hooks.model_router",
                        "ccproxy.hooks.forward_oauth"
                    ],
                    "rules": []
                }
            }
            
            # Write config files
            (config_dir / "config.yaml").write_text(yaml.dump(litellm_config))
            (config_dir / "ccproxy.yaml").write_text(yaml.dump(ccproxy_config))
            
            yield config_dir
    
    def test_claude_simple_query_with_mock(self, test_config_dir):
        """Test that claude command environment is set up correctly by ccproxy run."""
        port = yaml.safe_load((test_config_dir / "ccproxy.yaml").read_text())["litellm"]["port"]
        
        # Create a mock claude script that just verifies environment
        mock_claude = test_config_dir / "claude"
        mock_claude.write_text("""#!/bin/bash
if [ "$ANTHROPIC_BASE_URL" = "http://127.0.0.1:PORT" ]; then
    echo "SUCCESS: Environment configured correctly"
    exit 0
else
    echo "FAIL: ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"
    exit 1
fi
""".replace("PORT", str(port)))
        mock_claude.chmod(0o755)
        
        # Add mock claude to PATH
        env = os.environ.copy()
        env["PATH"] = f"{test_config_dir}:{env['PATH']}"
        env["CCPROXY_CONFIG_DIR"] = str(test_config_dir)
        
        # Run ccproxy run command
        result = subprocess.run(
            ["uv", "run", "ccproxy", "run", "claude", "-p", "Hello"],
            env=env,
            cwd=test_config_dir,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        assert result.returncode == 0, f"Command failed: {result.stderr}"
        assert "SUCCESS" in result.stdout


