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


class TestFindAvailablePort:
    """Tests for find_available_port function."""

    def test_returns_a_port_in_range(self) -> None:
        from ccproxy.utils import find_available_port

        port = find_available_port(49200, 49300)
        assert 49200 <= port <= 49300

    def test_returned_port_is_bindable(self) -> None:
        import socket

        from ccproxy.utils import find_available_port

        port = find_available_port(49200, 49300)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))

    def test_raises_when_all_ports_occupied(self) -> None:
        import socket

        from ccproxy.utils import find_available_port

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

            with (
                patch("socket.socket") as mock_sock_cls,
                pytest.raises(RuntimeError, match="Could not find available port"),
            ):
                mock_sock = mock_sock_cls.return_value.__enter__.return_value
                mock_sock.bind.side_effect = OSError("in use")
                find_available_port(port, port)


class TestDebugTable:
    """Tests for debug_table and helper functions."""

    def test_debug_dict(self) -> None:
        from ccproxy.utils import debug_table

        debug_table({"key": "value", "num": 42})

    def test_debug_list(self) -> None:
        from ccproxy.utils import debug_table

        debug_table([1, 2, 3])

    def test_debug_tuple(self) -> None:
        from ccproxy.utils import debug_table

        debug_table((1, "two", 3.0))

    def test_debug_object(self) -> None:
        from ccproxy.utils import debug_table

        class Obj:
            def __init__(self) -> None:
                self.x = 1
                self.y = "hello"

            def my_method(self) -> None:
                pass

        debug_table(Obj())

    def test_debug_scalar(self) -> None:
        from ccproxy.utils import debug_table

        debug_table(42)

    def test_debug_dict_with_title(self) -> None:
        from ccproxy.utils import debug_table

        debug_table({"a": 1}, title="My Dict")

    def test_debug_dict_non_compact(self) -> None:
        from ccproxy.utils import debug_table

        debug_table({"a": 1}, compact=False)

    def test_debug_list_non_compact(self) -> None:
        from ccproxy.utils import debug_table

        debug_table([1, 2], compact=False)

    def test_debug_object_show_methods(self) -> None:
        from ccproxy.utils import debug_table

        class Obj:
            def method(self) -> str:
                return "hi"

            @property
            def bad_prop(self) -> str:
                raise RuntimeError("cannot access")

        debug_table(Obj(), show_methods=True)

    def test_debug_dict_max_width(self) -> None:
        from ccproxy.utils import debug_table

        debug_table({"k": "x" * 200}, max_width=10)


class TestFormatValue:
    """Tests for _format_value helper."""

    def test_none(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value(None)
        assert "None" in result

    def test_bool_true(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value(True)
        assert "True" in result

    def test_bool_false(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value(False)
        assert "False" in result

    def test_int(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value(42)
        assert "42" in result

    def test_float(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value(3.14)
        assert "3.14" in result

    def test_string_truncation(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value("x" * 100, max_width=10)
        assert "..." in result

    def test_string_no_truncation(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value("short")
        assert "short" in result

    def test_list(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value([1, 2, 3])
        assert "list" in result

    def test_tuple(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value((1, 2))
        assert "tuple" in result

    def test_dict(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value({"a": 1})
        assert "dict" in result

    def test_callable(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value(lambda: None)
        assert "()" in result

    def test_object_truncation(self) -> None:
        from ccproxy.utils import _format_value

        class Big:
            def __str__(self) -> str:
                return "x" * 100

        result = _format_value(Big(), max_width=10)
        assert "..." in result

    def test_string_escapes_markup(self) -> None:
        from ccproxy.utils import _format_value

        result = _format_value("[bold]text[/bold]")
        assert r"\[" in result


class TestDvFunction:
    """Tests for dv() debug variables function."""

    def test_dv_basic(self) -> None:
        from ccproxy.utils import dv

        dv(1, "hello", [1, 2])

    def test_dv_with_kwargs(self) -> None:
        from ccproxy.utils import dv

        dv(x=1, y="test")

    def test_dv_no_frame(self) -> None:
        import inspect
        from unittest.mock import patch

        from ccproxy.utils import dv

        with patch.object(inspect, "currentframe", return_value=None):
            dv(1, 2, 3)


class TestAliasedFunctions:
    """Tests for dt(), d(), p() aliases."""

    def test_dt(self) -> None:
        from ccproxy.utils import dt

        dt({"key": "val"})

    def test_d(self) -> None:
        from ccproxy.utils import d

        d({"key": "val"})

    def test_p_dict(self) -> None:
        from ccproxy.utils import p

        p({"key": "val"})

    def test_p_list(self) -> None:
        from ccproxy.utils import p

        p([1, 2, 3])

    def test_p_tuple(self) -> None:
        from ccproxy.utils import p

        p((1, 2))

    def test_p_object(self) -> None:
        from ccproxy.utils import p

        class Obj:
            def __init__(self) -> None:
                self.x = 1
                self.y = "hello"

        p(Obj())

    def test_p_scalar(self) -> None:
        from ccproxy.utils import p

        p(42)

    def test_p_scalar_string(self) -> None:
        from ccproxy.utils import p

        p("plain string")


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
