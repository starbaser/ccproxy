"""Tests for the ccproxy db sql CLI command."""

import io
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ccproxy.cli import (
    DbSql,
    execute_sql,
    format_csv_output,
    format_json_output,
    format_table,
    get_database_url,
    handle_db_sql,
    main,
    resolve_sql_input,
)


class TestGetDatabaseUrl:
    """Test suite for get_database_url function."""

    def test_env_var_ccproxy_database_url(self, tmp_path: Path) -> None:
        """Test database URL from CCPROXY_DATABASE_URL env var."""
        with patch.dict(
            "os.environ", {"CCPROXY_DATABASE_URL": "postgresql://test:123@host/db"}
        ):
            result = get_database_url(tmp_path)
        assert result == "postgresql://test:123@host/db"

    def test_env_var_database_url(self, tmp_path: Path) -> None:
        """Test database URL from DATABASE_URL env var."""
        with patch.dict(
            "os.environ", {"DATABASE_URL": "postgresql://test:456@host/db"}, clear=True
        ):
            result = get_database_url(tmp_path)
        assert result == "postgresql://test:456@host/db"

    def test_ccproxy_database_url_takes_precedence(self, tmp_path: Path) -> None:
        """Test CCPROXY_DATABASE_URL takes precedence over DATABASE_URL."""
        with patch.dict(
            "os.environ",
            {
                "CCPROXY_DATABASE_URL": "postgresql://primary@host/db",
                "DATABASE_URL": "postgresql://fallback@host/db",
            },
        ):
            result = get_database_url(tmp_path)
        assert result == "postgresql://primary@host/db"

    def test_from_config_file(self, tmp_path: Path) -> None:
        """Test database URL from ccproxy.yaml config."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text(
            """
ccproxy:
  mitm:
    database_url: postgresql://config:789@host/db
"""
        )

        with patch.dict("os.environ", {}, clear=True):
            result = get_database_url(tmp_path)
        assert result == "postgresql://config:789@host/db"

    def test_from_config_with_env_expansion(self, tmp_path: Path) -> None:
        """Test database URL with environment variable expansion."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text(
            """
ccproxy:
  mitm:
    database_url: postgresql://${DB_USER}:${DB_PASS}@host/db
"""
        )

        with patch.dict(
            "os.environ", {"DB_USER": "myuser", "DB_PASS": "mypass"}, clear=True
        ):
            result = get_database_url(tmp_path)
        assert result == "postgresql://myuser:mypass@host/db"

    def test_from_config_with_env_default(self, tmp_path: Path) -> None:
        """Test database URL with environment variable default value."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text(
            """
ccproxy:
  mitm:
    database_url: postgresql://${DB_USER:-defaultuser}@host/db
"""
        )

        with patch.dict("os.environ", {}, clear=True):
            result = get_database_url(tmp_path)
        assert result == "postgresql://defaultuser@host/db"

    def test_no_config_returns_none(self, tmp_path: Path) -> None:
        """Test returns None when no config exists."""
        with patch.dict("os.environ", {}, clear=True):
            result = get_database_url(tmp_path)
        assert result is None

    def test_config_without_mitm_section(self, tmp_path: Path) -> None:
        """Test returns None when ccproxy.yaml has no mitm section."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text(
            """
ccproxy:
  debug: true
"""
        )

        with patch.dict("os.environ", {}, clear=True):
            result = get_database_url(tmp_path)
        assert result is None

    def test_config_without_database_url(self, tmp_path: Path) -> None:
        """Test returns None when mitm section has no database_url."""
        config_file = tmp_path / "ccproxy.yaml"
        config_file.write_text(
            """
ccproxy:
  mitm:
    port: 8081
"""
        )

        with patch.dict("os.environ", {}, clear=True):
            result = get_database_url(tmp_path)
        assert result is None


class TestExecuteSql:
    """Test suite for execute_sql function."""

    @pytest.mark.asyncio
    async def test_execute_sql_success(self) -> None:
        """Test successful SQL execution."""

        # Create mock records that behave like asyncpg Records
        # asyncpg records support keys() and dict() conversion
        class MockRecord(dict):
            def keys(self):
                return super().keys()

        mock_record1 = MockRecord({"id": 1, "name": "test"})
        mock_record2 = MockRecord({"id": 2, "name": "test2"})

        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [mock_record1, mock_record2]

        with patch("asyncpg.connect", return_value=mock_conn):
            rows, columns = await execute_sql(
                "postgresql://test@host/db", "SELECT * FROM test"
            )

        assert set(columns) == {"id", "name"}
        assert len(rows) == 2
        assert rows[0]["id"] == 1
        assert rows[1]["name"] == "test2"
        mock_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_sql_empty_results(self) -> None:
        """Test SQL execution with no results."""
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []

        with patch("asyncpg.connect", return_value=mock_conn):
            rows, columns = await execute_sql(
                "postgresql://test@host/db", "SELECT * FROM empty"
            )

        assert rows == []
        assert columns == []
        mock_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_sql_connection_error(self) -> None:
        """Test SQL execution with connection error."""
        with patch("asyncpg.connect", side_effect=Exception("Connection failed")):
            with pytest.raises(Exception, match="Connection failed"):
                await execute_sql("postgresql://test@host/db", "SELECT 1")


class TestResolveSqlInput:
    """Test suite for resolve_sql_input function."""

    def test_inline_query(self) -> None:
        """Test resolving inline SQL query."""
        cmd = DbSql(query="SELECT * FROM test")
        result = resolve_sql_input(cmd)
        assert result == "SELECT * FROM test"

    def test_file_query(self, tmp_path: Path) -> None:
        """Test resolving SQL query from file."""
        sql_file = tmp_path / "query.sql"
        sql_file.write_text("SELECT COUNT(*) FROM users")

        cmd = DbSql(file=sql_file)
        result = resolve_sql_input(cmd)
        assert result == "SELECT COUNT(*) FROM users"

    def test_stdin_query(self) -> None:
        """Test resolving SQL query from stdin."""
        cmd = DbSql()

        with patch("sys.stdin.isatty", return_value=False):
            with patch("sys.stdin.read", return_value="  SELECT 1  \n"):
                result = resolve_sql_input(cmd)

        assert result == "SELECT 1"

    def test_no_input_returns_none(self) -> None:
        """Test returns None when no input provided."""
        cmd = DbSql()

        with patch("sys.stdin.isatty", return_value=True):
            result = resolve_sql_input(cmd)

        assert result is None

    def test_inline_takes_precedence(self, tmp_path: Path) -> None:
        """Test inline query takes precedence over file."""
        sql_file = tmp_path / "query.sql"
        sql_file.write_text("SELECT FROM file")

        cmd = DbSql(query="SELECT FROM inline", file=sql_file)
        result = resolve_sql_input(cmd)
        assert result == "SELECT FROM inline"


class TestFormatTable:
    """Test suite for format_table function."""

    def test_format_table_basic(self) -> None:
        """Test basic table formatting."""
        from rich.console import Console

        rows = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        columns = ["id", "name"]

        output = io.StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        format_table(rows, columns, console)

        result = output.getvalue()
        assert "id" in result
        assert "name" in result
        assert "Alice" in result
        assert "Bob" in result
        assert "2 row(s)" in result

    def test_format_table_single_row(self) -> None:
        """Test table formatting with single row."""
        from rich.console import Console

        rows = [{"count": 42}]
        columns = ["count"]

        output = io.StringIO()
        console = Console(file=output, force_terminal=True, width=80)

        format_table(rows, columns, console)

        result = output.getvalue()
        assert "count" in result
        assert "42" in result
        assert "1 row(s)" in result


class TestFormatJsonOutput:
    """Test suite for format_json_output function."""

    def test_format_json_output(self, capsys) -> None:
        """Test JSON output formatting."""
        from rich.console import Console

        rows = [{"id": 1, "name": "test"}]

        console = Console()
        format_json_output(rows, console)

        captured = capsys.readouterr()
        result = captured.out
        assert '"id"' in result
        assert '"name"' in result

    def test_format_json_output_with_bytes(self, capsys) -> None:
        """Test JSON output with bytes fields (bytea columns)."""
        import json

        from rich.console import Console

        # Simulate bytea field containing JSON with newlines
        json_data = '{"messages": [{"role": "user", "content": "line1\\nline2"}]}'
        rows = [{"id": 1, "body": json_data.encode("utf-8")}]

        console = Console()
        format_json_output(rows, console)

        captured = capsys.readouterr()
        result = captured.out

        # Verify it's valid JSON
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["id"] == 1

        # Verify the body field is properly decoded and contains escaped newlines
        assert isinstance(parsed[0]["body"], str)
        body_content = parsed[0]["body"]

        # The body should be a JSON string (nested JSON)
        # It should contain escaped newlines (\\n) not literal newlines
        assert "\\n" in body_content
        # Parse the nested JSON to verify it's valid
        nested_json = json.loads(body_content)
        assert nested_json["messages"][0]["content"] == "line1\nline2"


class TestFormatCsvOutput:
    """Test suite for format_csv_output function."""

    def test_format_csv_output(self, capsys) -> None:
        """Test CSV output formatting."""
        rows = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        columns = ["id", "name"]

        format_csv_output(rows, columns)

        captured = capsys.readouterr()
        # Handle potential CRLF line endings from CSV module
        lines = [line.rstrip("\r") for line in captured.out.strip().split("\n")]
        assert lines[0] == "id,name"
        assert lines[1] == "1,Alice"
        assert lines[2] == "2,Bob"

    def test_format_csv_output_with_special_chars(self, capsys) -> None:
        """Test CSV output with special characters."""
        rows = [{"name": 'Test, "quoted"', "value": "line\nbreak"}]
        columns = ["name", "value"]

        format_csv_output(rows, columns)

        captured = capsys.readouterr()
        assert "name,value" in captured.out


class TestHandleDbSql:
    """Test suite for handle_db_sql function."""

    def test_handle_db_sql_mutually_exclusive_flags(
        self, tmp_path: Path, capsys
    ) -> None:
        """Test error when both --json and --csv are specified."""
        cmd = DbSql(query="SELECT 1", json=True, csv=True)

        with pytest.raises(SystemExit) as exc_info:
            handle_db_sql(tmp_path, cmd)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "--json and --csv are mutually exclusive" in captured.err

    def test_handle_db_sql_no_query(self, tmp_path: Path, capsys) -> None:
        """Test error when no SQL query provided."""
        cmd = DbSql()

        with patch("sys.stdin.isatty", return_value=True):
            with pytest.raises(SystemExit) as exc_info:
                handle_db_sql(tmp_path, cmd)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No SQL query provided" in captured.err

    def test_handle_db_sql_no_database_url(self, tmp_path: Path, capsys) -> None:
        """Test error when no database URL configured."""
        cmd = DbSql(query="SELECT 1")

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                handle_db_sql(tmp_path, cmd)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No database_url configured" in captured.err

    def test_handle_db_sql_connection_error(self, tmp_path: Path, capsys) -> None:
        """Test error handling for database connection failure."""
        cmd = DbSql(query="SELECT 1")

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test@host/db"}):
            with patch(
                "ccproxy.cli.execute_sql", side_effect=Exception("Connection refused")
            ):
                with pytest.raises(SystemExit) as exc_info:
                    handle_db_sql(tmp_path, cmd)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Connection refused" in captured.err

    def test_handle_db_sql_no_results_table(self, tmp_path: Path, capsys) -> None:
        """Test no results message for table output."""
        cmd = DbSql(query="SELECT * FROM empty")

        async def mock_execute(*args):
            return [], []

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test@host/db"}):
            with patch("ccproxy.cli.execute_sql", side_effect=mock_execute):
                handle_db_sql(tmp_path, cmd)

        captured = capsys.readouterr()
        assert "No results" in captured.err

    def test_handle_db_sql_no_results_json(self, tmp_path: Path, capsys) -> None:
        """Test empty array for JSON output with no results."""
        cmd = DbSql(query="SELECT * FROM empty", json=True)

        async def mock_execute(*args):
            return [], []

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test@host/db"}):
            with patch("ccproxy.cli.execute_sql", side_effect=mock_execute):
                handle_db_sql(tmp_path, cmd)

        captured = capsys.readouterr()
        assert captured.out.strip() == "[]"

    def test_handle_db_sql_success_table(self, tmp_path: Path, capsys) -> None:
        """Test successful SQL execution with table output."""
        cmd = DbSql(query="SELECT 1 as num")

        async def mock_execute(*args):
            return [{"num": 1}], ["num"]

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test@host/db"}):
            with patch("ccproxy.cli.execute_sql", side_effect=mock_execute):
                handle_db_sql(tmp_path, cmd)

        captured = capsys.readouterr()
        assert "num" in captured.out
        assert "1" in captured.out

    def test_handle_db_sql_success_csv(self, tmp_path: Path, capsys) -> None:
        """Test successful SQL execution with CSV output."""
        cmd = DbSql(query="SELECT 1 as num", csv=True)

        async def mock_execute(*args):
            return [{"num": 1}], ["num"]

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test@host/db"}):
            with patch("ccproxy.cli.execute_sql", side_effect=mock_execute):
                handle_db_sql(tmp_path, cmd)

        captured = capsys.readouterr()
        assert "num" in captured.out
        assert "1" in captured.out


class TestDbSqlMainDispatch:
    """Test suite for DbSql command dispatch in main()."""

    @patch("ccproxy.cli.handle_db_sql")
    def test_main_db_sql_command(self, mock_handle: Mock, tmp_path: Path) -> None:
        """Test main dispatches DbSql to handle_db_sql."""
        cmd = DbSql(query="SELECT 1")
        main(cmd, config_dir=tmp_path)

        mock_handle.assert_called_once_with(tmp_path, cmd)


class TestEntryPointRewriting:
    """Test suite for entry point rewriting of 'db sql' -> 'db-sql'."""

    def test_db_sql_rewrite(self) -> None:
        """Test that 'db sql' gets rewritten to 'db-sql'."""
        from ccproxy.cli import entry_point

        original_argv = sys.argv.copy()
        try:
            sys.argv = ["ccproxy", "db", "sql", "SELECT 1"]

            with patch("tyro.cli") as mock_tyro:
                entry_point()

            # Check argv was rewritten
            assert sys.argv == ["ccproxy", "db-sql", "SELECT 1"]
        finally:
            sys.argv = original_argv

    def test_db_sql_with_flags_rewrite(self) -> None:
        """Test that 'db sql --json' gets rewritten correctly."""
        from ccproxy.cli import entry_point

        original_argv = sys.argv.copy()
        try:
            sys.argv = ["ccproxy", "db", "sql", "--json", "SELECT 1"]

            with patch("tyro.cli") as mock_tyro:
                entry_point()

            assert sys.argv == ["ccproxy", "db-sql", "--json", "SELECT 1"]
        finally:
            sys.argv = original_argv

    def test_db_without_subcommand_not_rewritten(self) -> None:
        """Test that 'db' without subcommand is not rewritten."""
        from ccproxy.cli import entry_point

        original_argv = sys.argv.copy()
        try:
            sys.argv = ["ccproxy", "db"]

            with patch("tyro.cli") as mock_tyro:
                entry_point()

            # argv should not be changed (tyro will show help for invalid command)
            assert sys.argv == ["ccproxy", "db"]
        finally:
            sys.argv = original_argv
