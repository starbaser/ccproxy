"""Tests for num_workers configuration passthrough."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from ccproxy.cli import start_litellm


class TestNumWorkers:
    """Test suite for num_workers in ccproxy.yaml."""

    @patch("subprocess.run")
    def test_num_workers_passed_to_litellm(self, mock_run: Mock, tmp_path: Path) -> None:
        """Test num_workers from ccproxy.yaml is passed as --num_workers to litellm."""
        (tmp_path / "config.yaml").write_text("model_list: []")
        (tmp_path / "ccproxy.yaml").write_text(
            "ccproxy:\n  handler: 'ccproxy.handler:CCProxyHandler'\nlitellm:\n  num_workers: 8\n"
        )
        mock_run.return_value = Mock(returncode=0)

        with pytest.raises(SystemExit):
            start_litellm(tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "--num_workers" in cmd, f"--num_workers missing from command: {cmd}"
        assert cmd[cmd.index("--num_workers") + 1] == "8"
