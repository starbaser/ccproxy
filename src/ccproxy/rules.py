"""Classification rules for request routing."""

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, cast

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

    @staticmethod
    def _extract_text(messages: list[Any]) -> str:
        """Extract text content from a messages list for token counting."""
        parts: list[str] = []
        for msg in messages:
            if isinstance(msg, dict):
                msg_dict = cast(dict[str, Any], msg)
                content: Any = msg_dict.get("content", "")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for item in cast(list[Any], content):
                        if isinstance(item, dict):
                            item_dict = cast(dict[str, Any], item)
                            if item_dict.get("type") == "text":
                                parts.append(str(item_dict.get("text", "")))
            else:
                parts.append(str(msg))
        return " ".join(parts)

    def evaluate(self, request: dict[str, Any], config: "CCProxyConfig") -> bool:
        token_count = 0

        model: str = str(request.get("model", ""))

        messages: Any = request.get("messages", [])
        if isinstance(messages, list):
            total_text = self._extract_text(cast(list[Any], messages))
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
        tools: Any = request.get("tools", [])
        if not isinstance(tools, list):
            return False
        for tool in cast(list[Any], tools):
            if isinstance(tool, dict):
                tool_dict = cast(dict[str, Any], tool)
                name: Any = tool_dict.get("name", "")
                if isinstance(name, str) and self.tool_name in name.lower():
                    return True

                # Check function.name (OpenAI format)
                function: Any = tool_dict.get("function", {})
                if isinstance(function, dict):
                    fn_dict = cast(dict[str, Any], function)
                    fn_name: Any = fn_dict.get("name", "")
                    if isinstance(fn_name, str) and self.tool_name in fn_name.lower():
                        return True
            elif isinstance(tool, str) and self.tool_name in tool.lower():
                return True

        return False
