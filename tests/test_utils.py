"""Tests for ccproxy utilities."""

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from ccproxy.utils import calculate_duration_ms, get_template_file, get_templates_dir, parse_session_id


class TestGetTemplatesDir:
    """Test suite for get_templates_dir function."""

    def test_templates_dir_development_mode(self, tmp_path: Path) -> None:
        """Test finding templates in development mode."""
        # Create a fake development structure
        src_dir = tmp_path / "src" / "ccproxy"
        src_dir.mkdir(parents=True)
        utils_file = src_dir / "utils.py"
        utils_file.touch()

        # Create templates directory two levels up
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "ccproxy.yaml").touch()

        # Mock __file__ to point to our fake utils.py
        with patch("ccproxy.utils.__file__", str(utils_file)):
            result = get_templates_dir()
            assert result == templates_dir

    def test_templates_dir_installed_mode(self, tmp_path: Path) -> None:
        """Test finding templates in installed package mode."""
        # Create a fake module location
        fake_module = tmp_path / "fake" / "location" / "ccproxy"
        fake_module.mkdir(parents=True)
        fake_utils = fake_module / "utils.py"
        fake_utils.touch()

        # Create templates inside the package
        templates_dir = fake_module / "templates"
        templates_dir.mkdir()
        (templates_dir / "ccproxy.yaml").touch()

        # Mock __file__
        with patch("ccproxy.utils.__file__", str(fake_utils)):
            result = get_templates_dir()
            assert result == templates_dir

    def test_templates_dir_not_found(self) -> None:
        """Test error when templates directory not found."""
        # Mock __file__ to point to a location without templates
        with (
            patch("ccproxy.utils.__file__", "/nowhere/utils.py"),
            patch.object(Path, "exists", return_value=False),
            pytest.raises(RuntimeError) as exc_info,
        ):
            get_templates_dir()

        assert "Could not find templates directory" in str(exc_info.value)


class TestGetTemplateFile:
    """Test suite for get_template_file function."""

    @patch("ccproxy.utils.get_templates_dir")
    def test_get_existing_template(self, mock_get_templates: Mock, tmp_path: Path) -> None:
        """Test getting an existing template file."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        template_file = templates_dir / "test.yaml"
        template_file.write_text("test content")

        mock_get_templates.return_value = templates_dir

        result = get_template_file("test.yaml")
        assert result == template_file

    @patch("ccproxy.utils.get_templates_dir")
    def test_get_nonexistent_template(self, mock_get_templates: Mock, tmp_path: Path) -> None:
        """Test error when template file doesn't exist."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()

        mock_get_templates.return_value = templates_dir

        with pytest.raises(FileNotFoundError) as exc_info:
            get_template_file("missing.yaml")

        assert "Template file not found: missing.yaml" in str(exc_info.value)


class TestCalculateDurationMs:
    """Test suite for calculate_duration_ms function."""

    def test_calculate_duration_with_floats(self) -> None:
        """Test duration calculation with float timestamps."""
        start_time = 1000.0
        end_time = 1002.5

        result = calculate_duration_ms(start_time, end_time)

        assert result == 2500.0  # 2.5 seconds = 2500 ms

    def test_calculate_duration_with_timedelta(self) -> None:
        """Test duration calculation with timedelta objects."""
        start_time = timedelta(seconds=0)
        end_time = timedelta(seconds=1, milliseconds=500)

        result = calculate_duration_ms(start_time, end_time)

        assert result == 1500.0  # 1.5 seconds = 1500 ms

    def test_calculate_duration_with_mixed_types(self) -> None:
        """Test that mixed types are handled gracefully."""
        # Mixed types that don't support subtraction should return 0.0
        start_time = 0
        end_time = timedelta(seconds=2)

        # This will fail because int - timedelta is not supported
        result = calculate_duration_ms(start_time, end_time)

        # Should return 0.0 due to TypeError
        assert result == 0.0

    def test_calculate_duration_with_invalid_types(self) -> None:
        """Test that invalid types return 0.0."""
        # String types should cause TypeError
        result = calculate_duration_ms("start", "end")
        assert result == 0.0

        # None types should cause TypeError
        result = calculate_duration_ms(None, None)
        assert result == 0.0

        # Object without subtraction support
        result = calculate_duration_ms({"time": 1}, {"time": 2})
        assert result == 0.0

    def test_calculate_duration_rounding(self) -> None:
        """Test that results are rounded to 2 decimal places."""
        start_time = 1000.0
        end_time = 1000.0012345

        result = calculate_duration_ms(start_time, end_time)

        assert result == 1.23  # Should be rounded to 2 decimal places

    def test_calculate_duration_negative(self) -> None:
        """Test calculation when end time is before start time."""
        start_time = 2000.0
        end_time = 1000.0

        result = calculate_duration_ms(start_time, end_time)

        assert result == -1000000.0  # Negative duration is allowed


class TestParseSessionId:
    """Tests for parse_session_id."""

    def test_json_format(self) -> None:
        user_id = json.dumps({"device_id": "dev1", "account_uuid": "acc1", "session_id": "abc123"})
        assert parse_session_id(user_id) == "abc123"

    def test_json_format_minimal(self) -> None:
        user_id = json.dumps({"session_id": "xyz"})
        assert parse_session_id(user_id) == "xyz"

    def test_json_format_no_session_id(self) -> None:
        user_id = json.dumps({"device_id": "dev1"})
        assert parse_session_id(user_id) is None

    def test_json_format_empty_session_id(self) -> None:
        user_id = json.dumps({"session_id": ""})
        assert parse_session_id(user_id) is None

    def test_json_format_invalid_json(self) -> None:
        assert parse_session_id("{not valid json") is None

    def test_legacy_format(self) -> None:
        assert parse_session_id("user_hash_account_uuid_session_sid123") == "sid123"

    def test_legacy_format_multiple_session_separators(self) -> None:
        assert parse_session_id("a_session_b_session_c") is None

    def test_neither_format(self) -> None:
        assert parse_session_id("plain-user-id") is None

    def test_empty_string(self) -> None:
        assert parse_session_id("") is None
