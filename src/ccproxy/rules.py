"""Classification rules for request routing."""

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ccproxy.config import CCProxyConfig


class ClassificationRule(ABC):
    """Abstract base class for classification rules.

    To create a custom classification rule:

    1. Inherit from ClassificationRule
    2. Implement the evaluate method
    3. Return True if the rule matches, False otherwise

    The rule can accept parameters in __init__ to configure its behavior.
    """

    @abstractmethod
    def evaluate(self, request: dict[str, Any], config: "CCProxyConfig") -> bool:
        """Evaluate the rule against the request."""


class ThinkingRule(ClassificationRule):
    """Rule for classifying requests with thinking field."""

    def evaluate(self, request: dict[str, Any], config: "CCProxyConfig") -> bool:
        return "thinking" in request


class MatchModelRule(ClassificationRule):
    """Rule for classifying requests based on model name."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def evaluate(self, request: dict[str, Any], config: "CCProxyConfig") -> bool:
        model = request.get("model", "")
        return isinstance(model, str) and self.model_name in model


class TokenCountRule(ClassificationRule):
    """Rule for classifying requests based on token count."""

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self._tokenizer_cache: dict[str, Any] = {}

    def _get_tokenizer(self, model: str) -> Any:
        """Get appropriate tokenizer for the model, with caching."""
        if model in self._tokenizer_cache:
            return self._tokenizer_cache[model]

        try:
            import tiktoken

            if "gpt-4" in model or "gpt-3.5" in model:
                encoding = tiktoken.encoding_for_model(model)
            else:
                encoding = tiktoken.get_encoding("cl100k_base")

            self._tokenizer_cache[model] = encoding
            return encoding
        except Exception:
            # If tiktoken fails, return None to fall back to estimation
            return None

    def _count_tokens(self, text: str, model: str) -> int:
        """Count tokens in text using model-specific tokenizer."""
        tokenizer = self._get_tokenizer(model)
        if tokenizer:
            try:
                return len(tokenizer.encode(text))
            except Exception as e:
                logger.warning(f"Token encoding failed for model {model}: {e}")
                # Fall through to estimation

        # ~3 chars per token estimation
        return len(text) // 3

    def evaluate(self, request: dict[str, Any], config: "CCProxyConfig") -> bool:
        token_count = 0

        model = request.get("model", "")

        messages = request.get("messages", [])
        if isinstance(messages, list):
            total_text = ""
            for msg in messages:
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        total_text += content + " "
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                total_text += item.get("text", "") + " "
                else:
                    total_text += str(msg) + " "

            if total_text:
                token_count = self._count_tokens(total_text.strip(), model)

        token_count = max(
            token_count,
            request.get("token_count", 0) or 0,
            request.get("num_tokens", 0) or 0,
            request.get("input_tokens", 0) or 0,
        )

        return token_count > self.threshold


class MatchToolRule(ClassificationRule):
    """Rule for classifying requests with specified tools."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name.lower()

    def evaluate(self, request: dict[str, Any], config: "CCProxyConfig") -> bool:
        tools = request.get("tools", [])
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict):
                    name = tool.get("name", "")
                    if isinstance(name, str) and self.tool_name in name.lower():
                        return True

                    # Check function.name (OpenAI format)
                    function = tool.get("function", {})
                    if isinstance(function, dict):
                        function_name = function.get("name", "")
                        if isinstance(function_name, str) and self.tool_name in function_name.lower():
                            return True
                elif isinstance(tool, str) and self.tool_name in tool.lower():
                    return True

        return False
