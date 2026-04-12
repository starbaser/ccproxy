"""Tests for ccproxy __main__ module."""

import runpy
import sys
from unittest.mock import patch


class TestMain:
    @patch("tyro.cli")
    def test_main_entry_point(self, mock_tyro_cli) -> None:
        """Test that __main__ calls tyro.cli with main function."""
        from ccproxy.cli import main

        # Run the module as __main__
        with patch.object(sys, "argv", ["ccproxy"]):
            runpy.run_module("ccproxy", run_name="__main__")

        mock_tyro_cli.assert_called_once_with(main)
