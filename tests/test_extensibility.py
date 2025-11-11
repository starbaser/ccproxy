"""Tests demonstrating classifier extensibility."""

from ccproxy.classifier import RequestClassifier
from ccproxy.config import CCProxyConfig
from ccproxy.rules import ClassificationRule


class CustomHeaderRule(ClassificationRule):
    """Example custom rule that routes based on headers."""

    def evaluate(self, request: dict, config: CCProxyConfig) -> bool:
        """Return True if X-Priority header is 'low'."""
        headers = request.get("headers", {})
        return isinstance(headers, dict) and headers.get("X-Priority") == "low"


class CustomUserAgentRule(ClassificationRule):
    """Example rule that routes based on user agent."""

    def evaluate(self, request: dict, config: CCProxyConfig) -> bool:
        """Return True if user agent contains 'bot'."""
        headers = request.get("headers", {})
        user_agent = headers.get("User-Agent", "").lower()
        return "bot" in user_agent


class CustomEnvironmentRule(ClassificationRule):
    """Example rule that uses config for decisions."""

    def __init__(self, env_key: str = "TEST_ENV"):
        """Initialize with environment key to check."""
        self.env_key = env_key

    def evaluate(self, request: dict, config: CCProxyConfig) -> bool:
        """Return True if environment matches env_key."""
        metadata = request.get("metadata", {})
        env = metadata.get("environment", "")
        return env == self.env_key


class TestClassifierExtensibility:
    """Test suite demonstrating classifier extensibility."""

    def test_add_custom_rule(self) -> None:
        """Test adding a custom rule to the classifier."""
        classifier = RequestClassifier()
        custom_rule = CustomHeaderRule()

        # Add custom rule with model_name
        classifier.add_rule("background", custom_rule)

        # Test that custom rule works
        request = {
            "model": "claude-sonnet-4-5-20250929",
            "messages": [{"role": "user", "content": "Hello"}],
            "headers": {"X-Priority": "low"},
        }

        model_name = classifier.classify(request)
        assert model_name == "background"

    def test_custom_rule_priority(self) -> None:
        """Test that custom rules respect order of addition."""
        classifier = RequestClassifier()

        # Clear default rules and add custom rules
        classifier._clear_rules()
        classifier.add_rule("background", CustomHeaderRule())  # Maps to background
        classifier.add_rule("think", CustomUserAgentRule())  # Maps to think

        # Request matches both rules
        request = {
            "headers": {
                "X-Priority": "low",
                "User-Agent": "MyBot/1.0",
            },
        }

        # Should match first rule (CustomHeaderRule)
        model_name = classifier.classify(request)
        assert model_name == "background"

        # Now reverse the order
        classifier._clear_rules()
        classifier.add_rule("think", CustomUserAgentRule())
        classifier.add_rule("background", CustomHeaderRule())

        # Same request should now return think (first matching rule)
        model_name = classifier.classify(request)
        assert model_name == "think"

    def test_custom_rule_with_config(self) -> None:
        """Test custom rule that uses configuration."""
        classifier = RequestClassifier()
        env_rule = CustomEnvironmentRule("staging")

        classifier.add_rule("think", env_rule)

        request = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {"environment": "staging"},
        }

        model_name = classifier.classify(request)
        assert model_name == "think"

    def test_replace_all_rules(self) -> None:
        """Test completely replacing default rules with custom ones."""
        classifier = RequestClassifier()

        # Clear all default rules
        classifier._clear_rules()

        # Add only custom rules
        classifier.add_rule("background", CustomHeaderRule())
        classifier.add_rule("web_search", CustomUserAgentRule())

        # Test that default rules no longer apply
        # This would normally trigger TokenCountRule
        request = {
            "model": "claude-sonnet-4-5-20250929",
            "token_count": 100000,  # Would trigger token_count normally
        }

        model_name = classifier.classify(request)
        assert model_name == "default"  # No rules match

        # But custom rules still work
        request["headers"] = {"X-Priority": "low"}
        model_name = classifier.classify(request)
        assert model_name == "background"

    def test_reset_to_default_rules(self) -> None:
        """Test resetting to default rules after customization."""

        from ccproxy.config import CCProxyConfig, RuleConfig, clear_config_instance, set_config_instance

        # Create test config with token_count rule
        test_config = CCProxyConfig()
        test_config.rules = [
            RuleConfig(name="token_count", rule_path="ccproxy.rules.TokenCountRule", params=[{"threshold": 60000}])
        ]

        # Set the test config
        clear_config_instance()
        set_config_instance(test_config)

        try:
            classifier = RequestClassifier()

            # Add custom rule
            classifier.add_rule("background", CustomHeaderRule())

            # Clear and add only custom
            classifier._clear_rules()
            classifier.add_rule("background", CustomHeaderRule())

            # Verify default rules don't work
            request = {"token_count": 100000}
            model_name = classifier.classify(request)
            assert model_name == "default"

            # Reset to defaults
            classifier._setup_rules()

            # Now default rules work again
            model_name = classifier.classify(request)
            assert model_name == "token_count"
        finally:
            clear_config_instance()

    def test_mixed_default_and_custom_rules(self) -> None:
        """Test using both default and custom rules together."""
        from ccproxy.config import CCProxyConfig, RuleConfig, clear_config_instance, set_config_instance

        # Create test config with token_count rule
        test_config = CCProxyConfig()
        test_config.rules = [
            RuleConfig(name="token_count", rule_path="ccproxy.rules.TokenCountRule", params=[{"threshold": 60000}])
        ]

        # Set the test config
        clear_config_instance()
        set_config_instance(test_config)

        try:
            classifier = RequestClassifier()

            # Add custom rule on top of defaults
            classifier.add_rule("production", CustomEnvironmentRule("production"))

            # Test default rule (token count)
            request = {"token_count": 100000}
            model_name = classifier.classify(request)
            assert model_name == "token_count"

            # Test custom rule
            request = {
                "model": "claude-sonnet-4-5-20250929",
                "metadata": {"environment": "production"},
            }
            model_name = classifier.classify(request)
            assert model_name == "production"
        finally:
            clear_config_instance()

    def test_custom_rule_edge_cases(self) -> None:
        """Test edge cases with custom rules."""
        classifier = RequestClassifier()

        # Rule that always returns False
        class NeverMatchRule(ClassificationRule):
            def evaluate(self, request: dict, config: CCProxyConfig) -> bool:
                return False

        # Rule that checks nested data
        class NestedDataRule(ClassificationRule):
            def evaluate(self, request: dict, config: CCProxyConfig) -> bool:
                try:
                    nested = request.get("data", {}).get("nested", {}).get("value")
                    return nested == "special"
                except (AttributeError, TypeError):
                    return False

        classifier.add_rule("never", NeverMatchRule())
        classifier.add_rule("web_search", NestedDataRule())

        # Test never-matching rule
        request = {"model": "any"}
        model_name = classifier.classify(request)
        assert model_name == "default"

        # Test nested data rule
        request = {"data": {"nested": {"value": "special"}}}
        model_name = classifier.classify(request)
        assert model_name == "web_search"

    def test_stateful_custom_rule(self) -> None:
        """Test custom rule with internal state."""

        class CounterRule(ClassificationRule):
            """Rule that alternates between matching based on call count."""

            def __init__(self):
                self.count = 0

            def evaluate(self, request: dict, config: CCProxyConfig) -> bool:
                self.count += 1
                return self.count % 2 == 0

        classifier = RequestClassifier()
        counter_rule = CounterRule()
        classifier.add_rule("background", counter_rule)

        request = {"model": "claude"}

        # First call - no match (count=1)
        model_name = classifier.classify(request)
        assert model_name == "default"

        # Second call - match (count=2)
        model_name = classifier.classify(request)
        assert model_name == "background"

        # Third call - no match (count=3)
        model_name = classifier.classify(request)
        assert model_name == "default"
