"""Edge case tests for comprehensive coverage."""

from ccproxy.classifier import RequestClassifier
from ccproxy.config import CCProxyConfig
from ccproxy.rules import MatchModelRule, MatchToolRule, ThinkingRule, TokenCountRule


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_messages_with_string_items(self) -> None:
        """Test token counting when messages contain string items."""
        rule = TokenCountRule(threshold=100)
        config = CCProxyConfig()

        # Messages with mixed string and dict items
        request = {
            "messages": [
                "This is a simple string message",  # Should count characters
                {"role": "user", "content": "Dict message"},
                "Another string",
            ]
        }

        # String chars: 31 + 16 = 47, Dict chars: 12
        # Total: 59 chars / 4 = ~14 tokens
        result = rule.evaluate(request, config)
        assert result is False  # Below threshold of 100

    def test_messages_with_none_content(self) -> None:
        """Test handling of None content in messages."""
        rule = TokenCountRule(threshold=100)
        config = CCProxyConfig()

        request = {
            "messages": [
                {"role": "user", "content": None},
                {"role": "assistant", "content": "Valid content"},
            ]
        }

        result = rule.evaluate(request, config)
        assert result is False

    def test_messages_with_numeric_content(self) -> None:
        """Test handling of numeric content in messages."""
        rule = TokenCountRule(threshold=100)
        config = CCProxyConfig()

        request = {
            "messages": [
                {"role": "user", "content": 12345},  # Numeric content
                {"role": "assistant", "content": 3.14159},  # Float content
            ]
        }

        result = rule.evaluate(request, config)
        assert result is False

    def test_empty_model_string(self) -> None:
        """Test MatchModelRule with empty string model."""
        rule = MatchModelRule(model_name="claude-haiku-4-5-20251001")
        config = CCProxyConfig()

        request = {"model": ""}
        result = rule.evaluate(request, config)
        assert result is False

    def test_thinking_field_false(self) -> None:
        """Test ThinkingRule when thinking field is explicitly False."""
        rule = ThinkingRule()
        config = CCProxyConfig()

        # thinking field exists but is False
        request = {"thinking": False}
        result = rule.evaluate(request, config)
        assert result is True  # Field exists, value doesn't matter

    def test_thinking_field_zero(self) -> None:
        """Test ThinkingRule when thinking field is 0."""
        rule = ThinkingRule()
        config = CCProxyConfig()

        request = {"thinking": 0}
        result = rule.evaluate(request, config)
        assert result is True  # Field exists, value doesn't matter

    def test_web_search_nested_tool_structure(self) -> None:
        """Test MatchToolRule with deeply nested tool structure."""
        rule = MatchToolRule(tool_name="web_search")
        config = CCProxyConfig()

        request = {
            "tools": [
                {
                    "function": {
                        "name": "search_web",  # Not exact match
                    }
                },
                {
                    "name": "WEB_SEARCH",  # Case insensitive match at top level
                },
            ]
        }

        result = rule.evaluate(request, config)
        assert result is True

    def test_tools_with_invalid_types(self) -> None:
        """Test MatchToolRule with invalid tool types."""
        rule = MatchToolRule(tool_name="web_search")
        config = CCProxyConfig()

        request = {
            "tools": [
                None,  # None tool
                123,  # Numeric tool
                ["web_search"],  # List as tool
                {"name": "valid_tool"},
            ]
        }

        result = rule.evaluate(request, config)
        assert result is False

    def test_very_large_token_count(self) -> None:
        """Test with extremely large token counts."""
        rule = TokenCountRule(threshold=1_000_000)
        config = CCProxyConfig()

        request = {"token_count": 999_999_999}  # Just under 1 billion
        result = rule.evaluate(request, config)
        assert result is True  # Above threshold

    def test_negative_token_count(self) -> None:
        """Test with negative token counts."""
        rule = TokenCountRule(threshold=10000)
        config = CCProxyConfig()

        request = {"token_count": -1000}
        result = rule.evaluate(request, config)
        assert result is False  # Negative is less than threshold

    def test_classifier_with_empty_request(self) -> None:
        """Test classifier with completely empty request."""
        classifier = RequestClassifier()
        result = classifier.classify({})
        assert result == "default"

    def test_classifier_with_none_request_fields(self) -> None:
        """Test classifier with None values in request fields."""
        classifier = RequestClassifier()
        request = {
            "model": None,
            "messages": None,
            "tools": None,
            # thinking: None would still trigger THINK rule since key exists
            "token_count": None,
        }
        result = classifier.classify(request)
        assert result == "default"

    def test_malformed_messages_structure(self) -> None:
        """Test with various malformed message structures."""
        rule = TokenCountRule(threshold=60000)
        config = CCProxyConfig()

        # Messages is not a list
        request = {"messages": "not a list"}
        result = rule.evaluate(request, config)
        assert result is False

        # Messages is a dict
        request = {"messages": {"content": "test"}}
        result = rule.evaluate(request, config)
        assert result is False

        # Messages is None
        request = {"messages": None}
        result = rule.evaluate(request, config)
        assert result is False

    def test_unicode_in_messages(self) -> None:
        """Test token counting with unicode characters."""
        rule = TokenCountRule(threshold=1000)
        config = CCProxyConfig()

        request = {
            "messages": [
                {"role": "user", "content": "Hello 你好 🌍"},  # Mixed unicode
                "Émojis: 🚀🎉🎨",  # String with emojis
            ]
        }

        # Should count all characters: 10 + 12 = 22 chars / 4 = ~5 tokens
        result = rule.evaluate(request, config)
        assert result is False  # Below threshold of 1000

    def test_concurrent_token_fields(self) -> None:
        """Test when multiple token count fields have different values."""
        rule = TokenCountRule(threshold=1000)
        config = CCProxyConfig()

        request = {
            "token_count": 500,
            "num_tokens": 1500,  # This one exceeds threshold
            "input_tokens": 750,
            "messages": [{"content": "short"}],  # Would be ~1 token
        }

        # Should use max of all fields (1500 > 1000)
        result = rule.evaluate(request, config)
        assert result is True  # Above threshold

    def test_model_name_partial_matches(self) -> None:
        """Test MatchModelRule substring matching behavior."""
        rule = MatchModelRule(model_name="claude-haiku-4-5-20251001")
        config = CCProxyConfig()

        # These should match (contain "claude-haiku-4-5-20251001")
        matches = [
            "claude-haiku-4-5-20251001",  # Exact substring
            "claude-haiku-4-5-20251001-20241022",  # With version
            "claude-haiku-4-5-20251001-vision",  # With suffix
        ]

        for model in matches:
            request = {"model": model}
            result = rule.evaluate(request, config)
            assert result is True, f"Should match model: {model}"

        # These should NOT match
        non_matches = [
            "claude-sonnet-4-5-20250929",  # Different model
            "claude-3-5",  # Incomplete
            "haiku",  # Just the suffix
            "claude-haiku-3-20241022",  # Different version
            "claude-35-haiku",  # Missing hyphens
        ]

        for model in non_matches:
            request = {"model": model}
            result = rule.evaluate(request, config)
            assert result is False, f"Should not match model: {model}"

    def test_web_search_tool_edge_cases(self) -> None:
        """Test MatchToolRule with various edge cases."""
        rule = MatchToolRule(tool_name="web_search")
        config = CCProxyConfig()

        # Tool with web_search in description, not name
        request = {"tools": [{"name": "search_tool", "description": "Uses web_search API"}]}
        result = rule.evaluate(request, config)
        assert result is False  # Only checks name

        # Nested name field
        request = {"tools": [{"function": {"name": {"value": "web_search"}}}]}
        result = rule.evaluate(request, config)
        assert result is False  # name is not a string

        # Tool name is a number
        request = {"tools": [{"name": 123}]}
        result = rule.evaluate(request, config)
        assert result is False
