"""lightllm — surgical nerve connector to LiteLLM's transformation system.

Imports LiteLLM's provider-to-provider request/response transformation
pipeline and exposes it as two functions, without pulling in cost tracking,
callbacks, caching, router, or proxy server machinery.
"""

from ccproxy.lightllm.dispatch import transform_to_openai, transform_to_provider
from ccproxy.lightllm.registry import get_config

__all__ = ["get_config", "transform_to_openai", "transform_to_provider"]
