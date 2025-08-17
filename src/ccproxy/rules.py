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
        """Evaluate the rule against the request.

        Args:
            request: The request to evaluate
            config: The current configuration

        Returns:
            True if the rule matches, False otherwise
        """


class DefaultRule(ClassificationRule):
    def __init__(self, passthrough: bool):
        self.passthrough = passthrough


class ThinkingRule(ClassificationRule):
    """Rule for classifying requests with thinking field."""

    def evaluate(self, request: dict[str, Any], config: "CCProxyConfig") -> bool:
        """Evaluate if request has thinking field.

        Args:
            request: The request to evaluate
            config: The current configuration

        Returns:
            True if request has thinking field, False otherwise
        """
        # Check top-level thinking field
        return "thinking" in request


class MatchModelRule(ClassificationRule):
    """Rule for classifying requests based on model name."""

    def __init__(self, model_name: str) -> None:
        """Initialize the rule with a model name to match.

        Args:
            model_name: The model name substring to match
        """
        self.model_name = model_name

    def evaluate(self, request: dict[str, Any], config: "CCProxyConfig") -> bool:
        """Evaluate if request matches the configured model name.

        Args:
            request: The request to evaluate
            config: The current configuration

        Returns:
            True if model matches, False otherwise
        """
        model = request.get("model", "")
        return isinstance(model, str) and self.model_name in model


class TokenCountRule(ClassificationRule):
    """Rule for classifying requests based on token count."""

    def __init__(self, threshold: int) -> None:
        """Initialize the rule with a threshold.

        Args:
            threshold: The token count threshold
        """
        self.threshold = threshold
        self._tokenizer_cache: dict[str, Any] = {}

    def _get_tokenizer(self, model: str) -> Any:
        """Get appropriate tokenizer for the model.

        Args:
            model: Model name to get tokenizer for

        Returns:
            Tokenizer instance or None if not available
        """
        # Cache tokenizers to avoid repeated initialization
        if model in self._tokenizer_cache:
            return self._tokenizer_cache[model]

        try:
            import tiktoken

            # Map model names to appropriate tiktoken encodings
            if "gpt-4" in model or "gpt-3.5" in model:
                encoding = tiktoken.encoding_for_model(model)
            elif "claude" in model:
                # Claude uses similar tokenization to cl100k_base
                encoding = tiktoken.get_encoding("cl100k_base")
            elif "gemini" in model:
                # Gemini uses similar tokenization to cl100k_base
                encoding = tiktoken.get_encoding("cl100k_base")
            else:
                # Default to cl100k_base for unknown models
                encoding = tiktoken.get_encoding("cl100k_base")

            self._tokenizer_cache[model] = encoding
            return encoding
        except Exception:
            # If tiktoken fails, return None to fall back to estimation
            return None

    def _count_tokens(self, text: str, model: str) -> int:
        """Count tokens in text using model-specific tokenizer.

        Args:
            text: Text to count tokens for
            model: Model name for tokenizer selection

        Returns:
            Token count
        """
        tokenizer = self._get_tokenizer(model)
        if tokenizer:
            try:
                return len(tokenizer.encode(text))
            except Exception as e:
                logger.warning(f"Token encoding failed for model {model}: {e}")
                # Fall through to estimation

        # Fallback to estimation if tokenizer not available
        # Updated estimation: ~3 chars per token for better accuracy
        return len(text) // 3

    def evaluate(self, request: dict[str, Any], config: "CCProxyConfig") -> bool:
        """Evaluate if request has high token count based on threshold.

        Args:
            request: The request to evaluate
            config: The current configuration

        Returns:
            True if token count exceeds threshold, False otherwise
        """
        # Check various token count fields
        token_count = 0

        # Get model for tokenizer selection
        model = request.get("model", "")

        # Check messages token count
        messages = request.get("messages", [])
        if isinstance(messages, list):
            total_text = ""
            for msg in messages:
                if isinstance(msg, dict):
                    # Handle message dict format
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        total_text += content + " "
                    elif isinstance(content, list):
                        # Handle multi-modal content
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                total_text += item.get("text", "") + " "
                else:
                    # Handle simple string messages
                    total_text += str(msg) + " "

            if total_text:
                token_count = self._count_tokens(total_text.strip(), model)

        # Check explicit token count fields
        token_count = max(
            token_count,
            request.get("token_count", 0) or 0,
            request.get("num_tokens", 0) or 0,
            request.get("input_tokens", 0) or 0,
        )

        # Check against threshold
        return token_count > self.threshold


class MatchToolRule(ClassificationRule):
    """Rule for classifying requests with specified tools."""

    def __init__(self, tool_name: str) -> None:
        """Initialize the rule with a tool name to match.

        Args:
            tool_name: The tool name substring to match
        """
        self.tool_name = tool_name.lower()

    def evaluate(self, request: dict[str, Any], config: "CCProxyConfig") -> bool:
        """Evaluate if request uses the specified tool.

        Args:
            request: The request to evaluate
            config: The current configuration

        Returns:
            True if request has the specified tool, False otherwise
        """
        tools = request.get("tools", [])
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict):
                    # Check direct name field
                    name = tool.get("name", "")
                    if isinstance(name, str) and self.tool_name in name.lower():
                        return True

                    # Check function.name field (OpenAI format)
                    function = tool.get("function", {})
                    if isinstance(function, dict):
                        function_name = function.get("name", "")
                        if isinstance(function_name, str) and self.tool_name in function_name.lower():
                            return True
                elif isinstance(tool, str) and self.tool_name in tool.lower():
                    return True

        return False
