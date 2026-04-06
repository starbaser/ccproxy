"""Pass-through credential fallback and OAuth Bearer auth for ccproxy.

Two patches:
1. get_credentials fallback — any provider with an oat_sources entry gains
   pass-through credential support via get_credentials fallback.
2. Bearer auth injection — pass-through requests to providers using OAuth
   send Authorization: Bearer instead of ?key= query parameter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from litellm.proxy.pass_through_endpoints.passthrough_endpoint_router import (
    PassthroughEndpointRouter,
)

from ccproxy.config import get_config

if TYPE_CHECKING:
    from ccproxy.handler import CCProxyHandler

logger = logging.getLogger(__name__)

_applied = False

# Providers whose credentials came from oat_sources (OAuth tokens, not API keys).
# Tracked per-request so the Bearer auth patch knows when to activate.
_oauth_providers: set[str] = set()

_BEARER_HOSTS = frozenset({
    "generativelanguage.googleapis.com",
})


def apply(handler: CCProxyHandler) -> None:
    global _applied
    if _applied:
        return

    _patch_get_credentials()
    _patch_bearer_auth()
    _applied = True


def _patch_get_credentials() -> None:
    """Fallback to oat_sources when LiteLLM has no env-var credential."""
    _original = PassthroughEndpointRouter.get_credentials
    _get_token = get_config().get_oauth_token

    def resolve_credentials(self: Any, custom_llm_provider: str, region_name: Any) -> Any:
        result = _original(self, custom_llm_provider, region_name)
        if result is not None:
            _oauth_providers.discard(custom_llm_provider)
            return result
        token = _get_token(custom_llm_provider)
        if token is not None:
            _oauth_providers.add(custom_llm_provider)
        return token

    setattr(PassthroughEndpointRouter, "get_credentials", resolve_credentials)  # noqa: B010


def _patch_bearer_auth() -> None:
    """Move OAuth tokens from ?key= to Authorization: Bearer for supported hosts."""
    from litellm.proxy.pass_through_endpoints import (
        pass_through_endpoints as pt_module,
    )

    _original_ptr = pt_module.pass_through_request

    async def _patched_pass_through_request(
        request: Any,
        target: str,
        custom_headers: dict[str, Any],
        user_api_key_dict: Any,
        **kwargs: Any,
    ) -> Any:
        query_params: dict[str, Any] | None = kwargs.get("query_params")
        custom_llm_provider: str | None = kwargs.get("custom_llm_provider")

        if (
            query_params
            and "key" in query_params
            and custom_llm_provider in _oauth_providers
            and any(host in target for host in _BEARER_HOSTS)
        ):
            token = query_params.pop("key")
            custom_headers["Authorization"] = f"Bearer {token}"
            logger.debug(
                "pass-through %s: moved OAuth token from ?key= to Bearer header",
                custom_llm_provider,
            )

        return await _original_ptr(
            request, target, custom_headers, user_api_key_dict, **kwargs
        )

    pt_module.pass_through_request = _patched_pass_through_request  # type: ignore[assignment]
