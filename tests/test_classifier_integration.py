"""Integration tests for the request classifier with all rules."""

import pytest

from ccproxy.classifier import RequestClassifier
from ccproxy.config import CCProxyConfig, RuleConfig, clear_config_instance, set_config_instance


class TestRequestClassifierIntegration:
    """Integration tests for RequestClassifier with all rules."""

    @pytest.fixture
    def config(self) -> CCProxyConfig:
        """Create a test configuration."""
        # Create config with test rules
        config = CCProxyConfig()
        config.rules = [
            RuleConfig("large_context", "ccproxy.rules.TokenCountRule", [{"threshold": 10000}]),
            RuleConfig("background", "ccproxy.rules.MatchModelRule", [{"model_name": "claude-haiku-4-5-20251001"}]),
            RuleConfig("think", "ccproxy.rules.ThinkingRule", []),
            RuleConfig("web_search", "ccproxy.rules.MatchToolRule", [{"tool_name": "web_search"}]),
        ]
        return config

    @pytest.fixture
    def classifier(self, config: CCProxyConfig) -> RequestClassifier:
        """Create a classifier with all rules configured."""
        # Set the test config as the global config
        clear_config_instance()
        set_config_instance(config)
        try:
            yield RequestClassifier()
        finally:
            clear_config_instance()

    def test_priority_1_token_count_overrides_all(self, classifier: RequestClassifier) -> None:
        """Test that large context has highest priority."""
        # Request that matches multiple rules
        request = {
            "token_count": 15000,  # > 10000 threshold
            "model": "claude-haiku-4-5-20251001",  # Would match background
            "thinking": True,  # Would match thinking
            "tools": ["web_search"],  # Would match web_search
        }
        # Should return large_context due to priority
        assert classifier.classify(request) == "large_context"

    def test_priority_2_background_overrides_lower(self, classifier: RequestClassifier) -> None:
        """Test that background model has second priority."""
        request = {
            "token_count": 5000,  # Below threshold
            "model": "claude-haiku-4-5-20251001-20241022",  # Matches background
            "thinking": True,  # Would match thinking
            "tools": ["web_search"],  # Would match web_search
        }
        # Should return background due to priority
        assert classifier.classify(request) == "background"

    def test_priority_3_thinking_overrides_web_search(self, classifier: RequestClassifier) -> None:
        """Test that thinking has third priority."""
        request = {
            "token_count": 5000,  # Below threshold
            "model": "gpt-4",  # Doesn't match background
            "thinking": True,  # Matches thinking
            "tools": ["web_search"],  # Would match web_search
        }
        # Should return think due to priority
        assert classifier.classify(request) == "think"

    def test_priority_4_web_search(self, classifier: RequestClassifier) -> None:
        """Test that web search has fourth priority."""
        request = {
            "token_count": 5000,  # Below threshold
            "model": "gpt-4",  # Doesn't match background
            # No thinking field
            "tools": [{"name": "web_search"}],  # Matches web_search
        }
        # Should return web_search
        assert classifier.classify(request) == "web_search"

    def test_priority_5_default(self, classifier: RequestClassifier) -> None:
        """Test that default is returned when no rules match."""
        request = {
            "token_count": 5000,  # Below threshold
            "model": "gpt-4",  # Doesn't match background
            # No thinking field
            "tools": ["calculator"],  # Doesn't match web_search
        }
        # Should return default
        assert classifier.classify(request) == "default"

    def test_realistic_claude_code_request(self, classifier: RequestClassifier) -> None:
        """Test with a realistic Claude Code API request."""
        request = {
            "model": "claude-sonnet-4-5-20250929",
            "messages": [
                {"role": "user", "content": "Write a Python function to calculate fibonacci"},
            ],
            "temperature": 0.7,
            "max_tokens": 4000,
        }
        # Should return default (no special routing needed)
        assert classifier.classify(request) == "default"

    def test_realistic_long_context_request(self, classifier: RequestClassifier) -> None:
        """Test with a realistic long context request."""
        # Create a very long message that exceeds 10000 token threshold
        # Using varied text to prevent efficient encoding of repeated characters
        varied_text = "The quick brown fox jumps over the lazy dog. " * 500
        # This will be ~5001 tokens, need to double for >10000
        long_content = varied_text * 3  # ~15,003 tokens
        request = {
            "model": "claude-sonnet-4-5-20250929",
            "messages": [
                {"role": "user", "content": long_content},
            ],
        }
        # Should return large_context
        assert classifier.classify(request) == "large_context"

    def test_realistic_thinking_request(self, classifier: RequestClassifier) -> None:
        """Test with a realistic thinking request."""
        request = {
            "model": "claude-sonnet-4-5-20250929",
            "messages": [
                {"role": "user", "content": "Solve this complex problem..."},
            ],
            "thinking": True,  # Claude's thinking mode
        }
        # Should return think
        assert classifier.classify(request) == "think"

    def test_realistic_background_task(self, classifier: RequestClassifier) -> None:
        """Test with a realistic background task using haiku."""
        request = {
            "model": "claude-haiku-4-5-20251001",
            "messages": [
                {"role": "user", "content": "Format this JSON data"},
            ],
            "temperature": 0.0,  # Deterministic for background tasks
        }
        # Should return background
        assert classifier.classify(request) == "background"

    def test_realistic_web_search_request(self, classifier: RequestClassifier) -> None:
        """Test with a realistic web search request."""
        request = {
            "model": "claude-sonnet-4-5-20250929",
            "messages": [
                {"role": "user", "content": "Search for the latest news about AI"},
            ],
            "tools": [
                {
                    "name": "web_search",
                    "description": "Search the web for information",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ],
        }
        # Should return web_search
        assert classifier.classify(request) == "web_search"

    def test_edge_case_empty_request(self, classifier: RequestClassifier) -> None:
        """Test with an empty request."""
        request = {}
        # Should return default
        assert classifier.classify(request) == "default"

    def test_edge_case_malformed_messages(self, classifier: RequestClassifier) -> None:
        """Test with malformed messages field."""
        request = {
            "model": "gpt-4",
            "messages": "not a list",  # Invalid type
        }
        # Should handle gracefully and return default
        assert classifier.classify(request) == "default"

    def test_custom_rules_after_reset(self, classifier: RequestClassifier) -> None:
        """Test that _setup_rules restores default behavior."""
        # Clear all rules
        classifier._clear_rules()

        # Should return default (no rules)
        request = {"thinking": True}
        assert classifier.classify(request) == "default"

        # Reset to defaults
        classifier._setup_rules()

        # Should now match thinking rule
        assert classifier.classify(request) == "think"

    def test_token_estimation_from_messages(self, classifier: RequestClassifier) -> None:
        """Test accurate token estimation from message content."""
        # Using varied text for realistic tokenization
        base_text = "The quick brown fox jumps over the lazy dog. " * 50  # ~501 tokens
        messages = [
            {"role": "user", "content": base_text * 6},  # ~3006 tokens
            {"role": "assistant", "content": base_text * 6},  # ~3006 tokens
            {"role": "user", "content": base_text * 3},  # ~1503 tokens
        ]
        request = {"messages": messages}

        # Total ~7515 tokens, below 10000 threshold
        assert classifier.classify(request) == "default"

        # Add one more message to go over threshold
        messages.append({"role": "assistant", "content": base_text * 6})  # ~3006 tokens
        request = {"messages": messages}

        # Total ~10521 tokens, should trigger large context
        assert classifier.classify(request) == "large_context"
