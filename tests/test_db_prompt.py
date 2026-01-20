"""Tests for the ccproxy db prompt CLI command."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccproxy.cli import (
    DbPrompt,
    format_content_block,
    format_trace_markdown,
    handle_db_prompt,
    parse_anthropic_request,
    parse_anthropic_response,
    parse_streaming_response,
)


class TestParseAnthropicRequest:
    """Test suite for parse_anthropic_request function."""

    def test_basic_request(self):
        """Test parsing basic messages request."""
        body = json.dumps(
            {
                "model": "claude-sonnet-4-5-20250929",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
            }
        ).encode()

        result = parse_anthropic_request(body)

        assert result["model"] == "claude-sonnet-4-5-20250929"
        assert len(result["messages"]) == 1
        assert result["max_tokens"] == 1024
        assert result["system"] is None

    def test_with_system_string(self):
        """Test parsing request with string system message."""
        body = json.dumps(
            {
                "model": "claude-sonnet-4-5-20250929",
                "messages": [{"role": "user", "content": "Hello"}],
                "system": "You are a helpful assistant.",
            }
        ).encode()

        result = parse_anthropic_request(body)

        assert result["system"] == "You are a helpful assistant."

    def test_with_system_blocks(self):
        """Test parsing request with system as content blocks."""
        body = json.dumps(
            {
                "model": "claude-sonnet-4-5-20250929",
                "messages": [{"role": "user", "content": "Hello"}],
                "system": [
                    {"type": "text", "text": "You are Claude Code."},
                    {"type": "text", "text": "Follow instructions."},
                ],
            }
        ).encode()

        result = parse_anthropic_request(body)

        assert isinstance(result["system"], list)
        assert len(result["system"]) == 2

    def test_with_tools(self):
        """Test parsing request with tool definitions."""
        body = json.dumps(
            {
                "model": "claude-sonnet-4-5-20250929",
                "messages": [{"role": "user", "content": "Hello"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "Get current weather",
                        "input_schema": {"type": "object"},
                    }
                ],
            }
        ).encode()

        result = parse_anthropic_request(body)

        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "get_weather"

    def test_with_thinking(self):
        """Test parsing request with thinking enabled."""
        body = json.dumps(
            {
                "model": "claude-sonnet-4-5-20250929",
                "messages": [{"role": "user", "content": "Hello"}],
                "thinking": {"type": "enabled", "budget_tokens": 10000},
            }
        ).encode()

        result = parse_anthropic_request(body)

        assert result["thinking"]["budget_tokens"] == 10000

    def test_invalid_json(self):
        """Test handling invalid JSON body."""
        body = b"not valid json"

        result = parse_anthropic_request(body)

        assert "error" in result
        assert "Failed to parse JSON" in result["error"]

    def test_empty_body(self):
        """Test handling empty request body."""
        result = parse_anthropic_request(None)

        assert "error" in result
        assert result["error"] == "Empty request body"


class TestParseAnthropicResponse:
    """Test suite for parse_anthropic_response function."""

    def test_non_streaming_response(self):
        """Test parsing standard JSON response."""
        body = json.dumps(
            {
                "content": [{"type": "text", "text": "Hello!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "model": "claude-sonnet-4-5-20250929",
            }
        ).encode()

        result = parse_anthropic_response(body, "application/json")

        assert len(result["content"]) == 1
        assert result["content"][0]["text"] == "Hello!"
        assert result["stop_reason"] == "end_turn"
        assert result["usage"]["input_tokens"] == 10

    def test_streaming_response(self):
        """Test parsing SSE streaming response."""
        sse_data = "\n".join(
            [
                "event: message_start",
                'data: {"type":"message_start","message":{"model":"claude-sonnet-4-5-20250929","usage":{"input_tokens":10}}}',
                "",
                "event: content_block_start",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world!"}}',
                "",
                "event: message_delta",
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}',
                "",
            ]
        )

        result = parse_anthropic_response(sse_data.encode(), "text/event-stream")

        assert result["streaming"] is True
        assert len(result["content"]) == 1
        assert result["content"][0]["text"] == "Hello world!"
        assert result["stop_reason"] == "end_turn"

    def test_with_thinking_blocks(self):
        """Test parsing response with thinking content."""
        sse_data = "\n".join(
            [
                "event: message_start",
                'data: {"type":"message_start","message":{"model":"claude-sonnet-4-5-20250929"}}',
                "",
                "event: content_block_start",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Let me think..."}}',
                "",
                "event: content_block_start",
                'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Here is my answer."}}',
                "",
            ]
        )

        result = parse_anthropic_response(sse_data.encode(), "text/event-stream")

        assert len(result["content"]) == 2
        assert result["content"][0]["type"] == "thinking"
        assert result["content"][0]["thinking"] == "Let me think..."
        assert result["content"][1]["text"] == "Here is my answer."

    def test_empty_body(self):
        """Test handling empty response body."""
        result = parse_anthropic_response(None, "application/json")

        assert "error" in result
        assert result["error"] == "Empty response body"

    def test_invalid_json(self):
        """Test handling invalid JSON in non-streaming response."""
        result = parse_anthropic_response(b"not json", "application/json")

        assert "error" in result
        assert "Failed to parse JSON" in result["error"]


class TestParseStreamingResponse:
    """Test suite for parse_streaming_response function."""

    def test_consolidates_text_deltas(self):
        """Test that text deltas are properly consolidated."""
        text = "\n".join(
            [
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"A"}}',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"B"}}',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"C"}}',
            ]
        )

        result = parse_streaming_response(text)

        assert result["content"][0]["text"] == "ABC"

    def test_handles_malformed_json_lines(self):
        """Test that malformed JSON lines are skipped."""
        text = "\n".join(
            [
                "data: not json",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":"ok"}}',
            ]
        )

        result = parse_streaming_response(text)

        assert len(result["content"]) == 1


class TestFormatContentBlock:
    """Test suite for format_content_block function."""

    def test_text_block(self):
        """Test formatting text block."""
        block = {"type": "text", "text": "Hello world"}

        lines = format_content_block(block)

        assert lines == ["Hello world"]

    def test_thinking_block(self):
        """Test formatting thinking block."""
        block = {"type": "thinking", "thinking": "Let me think..."}

        lines = format_content_block(block)

        assert "<details>" in lines
        assert "<summary>Thinking</summary>" in lines
        assert "Let me think..." in lines
        assert "</details>" in lines

    def test_tool_use_block(self):
        """Test formatting tool_use block."""
        block = {
            "type": "tool_use",
            "id": "tool_123",
            "name": "get_weather",
            "input": {"city": "Tokyo"},
        }

        lines = format_content_block(block)

        assert any("**Tool Use: get_weather**" in line for line in lines)
        assert any("tool_123" in line for line in lines)
        assert "```json" in lines

    def test_tool_result_block(self):
        """Test formatting tool_result block."""
        block = {
            "type": "tool_result",
            "tool_use_id": "tool_123",
            "content": "Weather is sunny",
        }

        lines = format_content_block(block)

        assert any("**Tool Result**" in line for line in lines)
        assert any("Weather is sunny" in line for line in lines)

    def test_tool_result_error(self):
        """Test formatting tool_result with error."""
        block = {
            "type": "tool_result",
            "tool_use_id": "tool_123",
            "content": "Error occurred",
            "is_error": True,
        }

        lines = format_content_block(block)

        assert any("[ERROR]" in line for line in lines)

    def test_image_block(self):
        """Test formatting image block."""
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png"},
        }

        lines = format_content_block(block)

        assert any("*[Image: image/png]*" in line for line in lines)

    def test_unknown_block(self):
        """Test formatting unknown block type."""
        block = {"type": "custom_type", "data": "value"}

        lines = format_content_block(block)

        assert any("*[custom_type]*" in line for line in lines)


class TestFormatTraceMarkdown:
    """Test suite for format_trace_markdown function."""

    @pytest.fixture
    def sample_trace(self):
        """Create sample trace data."""
        return {
            "trace_id": "abc-123-def",
            "proxy_direction": 1,
            "session_id": "session-456",
            "url": "https://api.anthropic.com/v1/messages",
            "status_code": 200,
            "duration_ms": 1234.56,
            "start_time": datetime(2025, 1, 20, 12, 0, 0, tzinfo=timezone.utc),
            "request_headers": {"content-type": "application/json"},
            "response_headers": {"content-type": "application/json"},
        }

    @pytest.fixture
    def sample_request(self):
        """Create sample parsed request."""
        return {
            "model": "claude-sonnet-4-5-20250929",
            "system": "You are a helpful assistant.",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ],
            "max_tokens": 1024,
            "temperature": 0.7,
            "thinking": None,
            "tools": None,
            "stream": False,
        }

    @pytest.fixture
    def sample_response(self):
        """Create sample parsed response."""
        return {
            "content": [{"type": "text", "text": "How can I help?"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 20},
        }

    def test_basic_conversation(self, sample_trace, sample_request, sample_response):
        """Test formatting simple user/assistant exchange."""
        md = format_trace_markdown(sample_trace, sample_request, sample_response)

        assert "# MITM Trace: abc-123-def" in md
        assert "claude-sonnet-4-5-20250929" in md
        assert "Forward (LiteLLM→Provider)" in md
        assert "## System Message" in md
        assert "You are a helpful assistant." in md
        assert "## Conversation" in md
        assert "### User" in md
        assert "Hello" in md
        assert "### Assistant (Response)" in md
        assert "How can I help?" in md
        assert "*Stop reason: end_turn*" in md

    def test_with_headers(self, sample_trace, sample_request, sample_response):
        """Test including HTTP headers."""
        md = format_trace_markdown(
            sample_trace, sample_request, sample_response, include_headers=True
        )

        assert "## HTTP Headers" in md
        assert "### Request Headers" in md
        assert "### Response Headers" in md

    def test_sensitive_header_redaction(self, sample_trace, sample_request, sample_response):
        """Test that auth headers are redacted."""
        sample_trace["request_headers"]["authorization"] = "Bearer sk-ant-api-key-12345678901234567890"

        md = format_trace_markdown(
            sample_trace, sample_request, sample_response, include_headers=True
        )

        # Should be truncated/redacted
        assert "sk-ant-api-key-12345678901234567890" not in md
        assert "..." in md or "[REDACTED]" in md

    def test_with_tools(self, sample_trace, sample_request, sample_response):
        """Test formatting with tool definitions."""
        sample_request["tools"] = [
            {"name": "get_weather", "description": "Get current weather for a city"},
            {"name": "search", "description": "Search the web"},
        ]

        md = format_trace_markdown(sample_trace, sample_request, sample_response)

        assert "## Tools" in md
        assert "*2 tools defined*" in md
        assert "**get_weather**" in md

    def test_with_thinking(self, sample_trace, sample_request, sample_response):
        """Test formatting with thinking blocks."""
        sample_request["thinking"] = {"type": "enabled", "budget_tokens": 10000}
        sample_response["content"] = [
            {"type": "thinking", "thinking": "Let me reason through this..."},
            {"type": "text", "text": "Here is my answer."},
        ]

        md = format_trace_markdown(sample_trace, sample_request, sample_response)

        assert "**thinking:** enabled (budget: 10000)" in md
        assert "<details>" in md
        assert "Let me reason through this..." in md

    def test_token_usage(self, sample_trace, sample_request, sample_response):
        """Test token usage display."""
        sample_response["usage"]["cache_read_input_tokens"] = 100

        md = format_trace_markdown(sample_trace, sample_request, sample_response)

        assert "### Token Usage" in md
        assert "**Input tokens:** 50" in md
        assert "**Output tokens:** 20" in md
        assert "**Cache read:** 100" in md

    def test_error_in_response(self, sample_trace, sample_request, sample_response):
        """Test formatting when response has error."""
        sample_response = {"error": "Rate limit exceeded"}

        md = format_trace_markdown(sample_trace, sample_request, sample_response)

        assert "## Error" in md
        assert "**Rate limit exceeded**" in md

    def test_reverse_direction(self, sample_trace, sample_request, sample_response):
        """Test reverse proxy direction label."""
        sample_trace["proxy_direction"] = 0

        md = format_trace_markdown(sample_trace, sample_request, sample_response)

        assert "Reverse (Client→LiteLLM)" in md

    def test_no_system_message(self, sample_trace, sample_request, sample_response):
        """Test when no system message is present."""
        sample_request["system"] = None

        md = format_trace_markdown(sample_trace, sample_request, sample_response)

        assert "*No system message*" in md

    def test_system_as_blocks(self, sample_trace, sample_request, sample_response):
        """Test system message as content blocks."""
        sample_request["system"] = [
            {"type": "text", "text": "You are Claude Code."},
            {"type": "text", "text": "Be helpful.", "cache_control": {"type": "ephemeral"}},
        ]

        md = format_trace_markdown(sample_trace, sample_request, sample_response)

        assert "You are Claude Code." in md
        assert "*[cache_control:" in md


class TestHandleDbPrompt:
    """Test suite for handle_db_prompt function integration."""

    @pytest.fixture
    def mock_trace(self):
        """Create a mock trace record."""
        return {
            "trace_id": "test-trace-id",
            "proxy_direction": 1,
            "session_id": "test-session",
            "method": "POST",
            "url": "https://api.anthropic.com/v1/messages",
            "host": "api.anthropic.com",
            "path": "/v1/messages",
            "status_code": 200,
            "duration_ms": 500.0,
            "start_time": datetime(2025, 1, 20, 12, 0, 0, tzinfo=timezone.utc),
            "end_time": datetime(2025, 1, 20, 12, 0, 1, tzinfo=timezone.utc),
            "request_headers": {},
            "response_headers": {},
            "request_body": json.dumps(
                {
                    "model": "claude-sonnet-4-5-20250929",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 1024,
                }
            ).encode(),
            "response_body": json.dumps(
                {
                    "content": [{"type": "text", "text": "Hi!"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            ).encode(),
            "response_content_type": "application/json",
        }

    @pytest.mark.asyncio
    async def test_fetch_trace_found(self, mock_trace):
        """Test fetching an existing trace."""
        from ccproxy.cli import fetch_trace

        # asyncpg is imported inside fetch_trace, so patch at module level
        with patch.dict("sys.modules", {"asyncpg": AsyncMock()}):
            import sys

            mock_asyncpg = sys.modules["asyncpg"]
            mock_conn = AsyncMock()
            mock_conn.fetchrow.return_value = mock_trace
            mock_conn.close = AsyncMock()
            mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

            result = await fetch_trace("postgres://localhost/test", "test-trace-id")

            assert result is not None
            assert result["trace_id"] == "test-trace-id"

    @pytest.mark.asyncio
    async def test_fetch_trace_not_found(self):
        """Test fetching a non-existent trace."""
        from ccproxy.cli import fetch_trace

        with patch.dict("sys.modules", {"asyncpg": AsyncMock()}):
            import sys

            mock_asyncpg = sys.modules["asyncpg"]
            mock_conn = AsyncMock()
            mock_conn.fetchrow.return_value = None
            mock_conn.close = AsyncMock()
            mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

            result = await fetch_trace("postgres://localhost/test", "nonexistent")

            assert result is None


class TestHandleDbPromptIntegration:
    """Integration tests for handle_db_prompt function."""

    @pytest.fixture
    def mock_trace_data(self):
        """Mock trace data for integration tests."""
        return {
            "trace_id": "test-trace-id",
            "proxy_direction": 0,
            "request_body": json.dumps(
                {
                    "model": "claude-sonnet-4-5-20250929",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 1024,
                }
            ).encode(),
            "response_body": json.dumps(
                {
                    "id": "msg_123",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi there!"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            ).encode(),
            "response_content_type": "application/json",
            "created_at": datetime.now(timezone.utc),
        }

    def test_handle_db_prompt_success_markdown(
        self, tmp_path, mock_trace_data, capsys
    ):
        """Test successful markdown output."""
        config_dir = tmp_path / ".ccproxy"
        config_dir.mkdir()

        cmd = DbPrompt(
            trace_id="test-trace-id",
            direction="reverse",
            include_headers=False,
            raw=False,
            output=None,
        )

        with (
            patch("ccproxy.cli.get_database_url") as mock_db_url,
            patch("ccproxy.cli.fetch_trace", new_callable=AsyncMock) as mock_fetch,
        ):
            mock_db_url.return_value = "postgresql://localhost/test"
            mock_fetch.return_value = mock_trace_data

            # Mock asyncio.run within the function scope
            with patch("asyncio.run", return_value=mock_trace_data):
                handle_db_prompt(config_dir, cmd)

            captured = capsys.readouterr()
            assert "# MITM Trace" in captured.out
            assert "### User" in captured.out
            assert "### Assistant" in captured.out
            assert "Hello" in captured.out
            assert "Hi there!" in captured.out

    def test_handle_db_prompt_with_output_file(self, tmp_path, mock_trace_data):
        """Test writing output to file."""
        config_dir = tmp_path / ".ccproxy"
        config_dir.mkdir()
        output_file = tmp_path / "output.md"

        cmd = DbPrompt(
            trace_id="test-trace-id",
            direction="reverse",
            include_headers=False,
            raw=False,
            output=output_file,
        )

        with (
            patch("ccproxy.cli.get_database_url") as mock_db_url,
            patch("ccproxy.cli.fetch_trace", new_callable=AsyncMock) as mock_fetch,
            patch("asyncio.run") as mock_run,
        ):
            mock_db_url.return_value = "postgresql://localhost/test"
            mock_run.return_value = mock_trace_data
            mock_fetch.return_value = mock_trace_data

            handle_db_prompt(config_dir, cmd)

            assert output_file.exists()
            content = output_file.read_text()
            assert "# MITM Trace" in content
            assert "### User" in content
            assert "### Assistant" in content

    def test_handle_db_prompt_raw_json(self, tmp_path, mock_trace_data, capsys):
        """Test raw JSON output."""
        config_dir = tmp_path / ".ccproxy"
        config_dir.mkdir()

        cmd = DbPrompt(
            trace_id="test-trace-id",
            direction="reverse",
            include_headers=False,
            raw=True,
            output=None,
        )

        with (
            patch("ccproxy.cli.get_database_url") as mock_db_url,
            patch("ccproxy.cli.fetch_trace", new_callable=AsyncMock) as mock_fetch,
            patch("asyncio.run") as mock_run,
        ):
            mock_db_url.return_value = "postgresql://localhost/test"
            mock_run.return_value = mock_trace_data
            mock_fetch.return_value = mock_trace_data

            handle_db_prompt(config_dir, cmd)

            captured = capsys.readouterr()
            output_data = json.loads(captured.out)
            assert "trace" in output_data
            assert "parsed_request" in output_data
            assert "parsed_response" in output_data
            assert output_data["trace"]["trace_id"] == "test-trace-id"

    def test_handle_db_prompt_trace_not_found(self, tmp_path):
        """Test error handling when trace not found."""
        config_dir = tmp_path / ".ccproxy"
        config_dir.mkdir()

        cmd = DbPrompt(
            trace_id="nonexistent",
            direction="reverse",
            include_headers=False,
            raw=False,
            output=None,
        )

        with (
            patch("ccproxy.cli.get_database_url") as mock_db_url,
            patch("ccproxy.cli.fetch_trace", new_callable=AsyncMock) as mock_fetch,
            patch("asyncio.run") as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            mock_db_url.return_value = "postgresql://localhost/test"
            mock_run.return_value = None
            mock_fetch.return_value = None

            handle_db_prompt(config_dir, cmd)

        assert exc_info.value.code == 1

    def test_handle_db_prompt_no_database_url(self, tmp_path):
        """Test error when no database URL configured."""
        config_dir = tmp_path / ".ccproxy"
        config_dir.mkdir()

        cmd = DbPrompt(
            trace_id="test-trace-id",
            direction="reverse",
            include_headers=False,
            raw=False,
            output=None,
        )

        with (
            patch("ccproxy.cli.get_database_url") as mock_db_url,
            pytest.raises(SystemExit) as exc_info,
        ):
            mock_db_url.return_value = None

            handle_db_prompt(config_dir, cmd)

        assert exc_info.value.code == 1

    def test_handle_db_prompt_invalid_direction(self, tmp_path):
        """Test error with invalid direction."""
        config_dir = tmp_path / ".ccproxy"
        config_dir.mkdir()

        cmd = DbPrompt(
            trace_id="test-trace-id",
            direction="invalid",
            include_headers=False,
            raw=False,
            output=None,
        )

        with pytest.raises(SystemExit) as exc_info:
            handle_db_prompt(config_dir, cmd)

        assert exc_info.value.code == 1

    def test_handle_db_prompt_direction_filter(self, tmp_path, mock_trace_data):
        """Test direction filtering with warning."""
        config_dir = tmp_path / ".ccproxy"
        config_dir.mkdir()

        # Set proxy_direction to 1 (forward) but filter for reverse
        mock_trace_data["proxy_direction"] = 1

        cmd = DbPrompt(
            trace_id="test-trace-id",
            direction="reverse",
            include_headers=False,
            raw=False,
            output=None,
        )

        with (
            patch("ccproxy.cli.get_database_url") as mock_db_url,
            patch("ccproxy.cli.fetch_trace", new_callable=AsyncMock) as mock_fetch,
            patch("asyncio.run") as mock_run,
        ):
            mock_db_url.return_value = "postgresql://localhost/test"
            mock_run.return_value = mock_trace_data
            mock_fetch.return_value = mock_trace_data

            # Should not raise, just warn
            handle_db_prompt(config_dir, cmd)

    def test_handle_db_prompt_exception_handling(self, tmp_path):
        """Test exception handling during fetch."""
        config_dir = tmp_path / ".ccproxy"
        config_dir.mkdir()

        cmd = DbPrompt(
            trace_id="test-trace-id",
            direction="reverse",
            include_headers=False,
            raw=False,
            output=None,
        )

        with (
            patch("ccproxy.cli.get_database_url") as mock_db_url,
            patch("asyncio.run") as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            mock_db_url.return_value = "postgresql://localhost/test"
            mock_run.side_effect = Exception("Database connection failed")

            handle_db_prompt(config_dir, cmd)

        assert exc_info.value.code == 1
