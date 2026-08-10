"""Pydantic-graph FSM dispatcher for streaming response transformations.

The response-side dispatchers :func:`dispatch_intake` and :func:`dispatch_render`
return per-provider async FSM instances; the persistent-loop bridge in
:class:`ccproxy.lightllm.graph.sse_pipeline.SSEPipeline` drives them from
mitmproxy's sync stream callable.

The request-side :func:`dispatch_dump_sync` routes all providers (Anthropic,
OpenAI, OpenAI Responses, Google, Perplexity) to the new :mod:`ccproxy.lightllm.adapters`
``render`` classmethods. Each accepts an :class:`LLMRenderInput` (Protocol;
:class:`ccproxy.pipeline.context.Context` satisfies it).
"""

import logging
from typing import TYPE_CHECKING

from ccproxy.lightllm.graph.anthropic_intake import AnthropicResponseIntakeFSM
from ccproxy.lightllm.graph.anthropic_render import AnthropicResponseRenderFSM
from ccproxy.lightllm.graph.google_intake import GoogleResponseIntakeFSM
from ccproxy.lightllm.graph.openai_conversations_intake import OpenAIConversationsIntakeFSM
from ccproxy.lightllm.graph.openai_intake import OpenAIResponseIntakeFSM
from ccproxy.lightllm.graph.openai_render import OpenAIResponseRenderFSM
from ccproxy.lightllm.graph.openai_responses_intake import OpenAIResponsesIntakeFSM
from ccproxy.lightllm.graph.openai_responses_render import OpenAIResponsesRenderFSM
from ccproxy.lightllm.graph.perplexity_intake import PerplexityResponseIntakeFSM
from ccproxy.lightllm.parsed import InboundFormat

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestParameters

    from ccproxy.lightllm.adapters import LLMRenderInput

logger = logging.getLogger(__name__)

__all__ = [
    "AnyAsyncIntakeFSM",
    "AnyAsyncRenderFSM",
    "OpenAIConversationsIntakeFSM",
    "UnsupportedListenerError",
    "UnsupportedUpstreamError",
    "dispatch_dump",
    "dispatch_dump_sync",
    "dispatch_intake",
    "dispatch_render",
]


_ANTHROPIC_COMPATIBLE = frozenset({"anthropic", "deepseek", "zai", "minimax"})
_GOOGLE_COMPATIBLE = frozenset({"google", "gemini", "vertex_ai", "vertex_ai_beta"})


# Aliases for the union of all response-side FSM types. The Half-B
# :class:`SSEPipeline` types its ``intake`` / ``render`` parameters against
# these so any FSM the dispatchers can produce is acceptable.
AnyAsyncIntakeFSM = (
    AnthropicResponseIntakeFSM
    | OpenAIResponseIntakeFSM
    | OpenAIResponsesIntakeFSM
    | GoogleResponseIntakeFSM
    | PerplexityResponseIntakeFSM
    | OpenAIConversationsIntakeFSM
)
AnyAsyncRenderFSM = AnthropicResponseRenderFSM | OpenAIResponseRenderFSM | OpenAIResponsesRenderFSM


class UnsupportedUpstreamError(ValueError):
    """Raised when :func:`dispatch_dump` is asked to render to an unknown provider."""


class UnsupportedListenerError(ValueError):
    """Raised when :func:`dispatch_render` is asked for a listener format it doesn't know."""


async def dispatch_dump(req: "LLMRenderInput", *, provider_type: str) -> bytes:
    """Render ``req`` to the wire bytes the named upstream expects.

    All providers route through :func:`dispatch_dump_sync` (kept here for
    test compatibility with code that ``await``s the call).
    """
    return dispatch_dump_sync(req, provider_type=provider_type)


def dispatch_intake(
    *,
    provider_type: str,
    model: str,
    request_params: "ModelRequestParameters",
) -> AnyAsyncIntakeFSM:
    """Dispatch to the right per-upstream response intake FSM.

    Routes Anthropic-compatible providers to the Anthropic intake FSM,
    OpenAI to the OpenAI intake FSM, OpenAI Responses
    to the Responses intake FSM, Google family (google / gemini / vertex_ai /
    vertex_ai_beta) to the Google intake FSM, and Perplexity Pro to its own intake FSM. Raises
    :class:`UnsupportedUpstreamError` for anything else — there's no fallback,
    because an unknown upstream means we have no idea how to parse its SSE.
    """
    if provider_type in _ANTHROPIC_COMPATIBLE:
        logger.debug("dispatch_intake: provider_type=%s → AnthropicResponseIntakeFSM", provider_type)
        return AnthropicResponseIntakeFSM(model=model, request_params=request_params)
    if provider_type == "openai":
        logger.debug("dispatch_intake: provider_type=%s → OpenAIResponseIntakeFSM", provider_type)
        return OpenAIResponseIntakeFSM(model=model, request_params=request_params)
    if provider_type == "openai_responses":
        logger.debug("dispatch_intake: provider_type=%s → OpenAIResponsesIntakeFSM", provider_type)
        return OpenAIResponsesIntakeFSM(model=model, request_params=request_params)
    if provider_type in _GOOGLE_COMPATIBLE:
        logger.debug("dispatch_intake: provider_type=%s → GoogleResponseIntakeFSM", provider_type)
        return GoogleResponseIntakeFSM(model=model, request_params=request_params)
    if provider_type == "perplexity_pro":
        logger.debug("dispatch_intake: provider_type=%s → PerplexityResponseIntakeFSM", provider_type)
        return PerplexityResponseIntakeFSM(model=model, request_params=request_params)
    if provider_type == "openai_conversations":
        logger.debug("dispatch_intake: provider_type=%s → OpenAIConversationsIntakeFSM", provider_type)
        return OpenAIConversationsIntakeFSM(model=model, request_params=request_params)
    logger.warning("dispatch_intake: no response intake for provider_type=%r — request will fail", provider_type)
    raise UnsupportedUpstreamError(f"no response intake for provider_type={provider_type!r}")


def dispatch_render(*, inbound_format: InboundFormat, model: str = "unknown") -> AnyAsyncRenderFSM:
    """Dispatch to the right per-inbound-format response render FSM.

    Routes ``ANTHROPIC_MESSAGES`` to the Anthropic render FSM and
    ``OPENAI_CHAT`` to the OpenAI render FSM. Raises
    :class:`UnsupportedListenerError` for ``UNKNOWN`` — there's no fallback,
    because an unknown inbound format means we have no idea what wire
    shape to produce.
    """
    if inbound_format is InboundFormat.ANTHROPIC_MESSAGES:
        logger.debug("dispatch_render: inbound_format=%s → AnthropicResponseRenderFSM", inbound_format)
        return AnthropicResponseRenderFSM(model=model)
    if inbound_format is InboundFormat.OPENAI_CHAT:
        logger.debug("dispatch_render: inbound_format=%s → OpenAIResponseRenderFSM", inbound_format)
        return OpenAIResponseRenderFSM(model=model)
    if inbound_format is InboundFormat.OPENAI_RESPONSES:
        logger.debug("dispatch_render: inbound_format=%s → OpenAIResponsesRenderFSM", inbound_format)
        return OpenAIResponsesRenderFSM(model=model)
    logger.warning("dispatch_render: no response render for inbound_format=%s — request will fail", inbound_format)
    raise UnsupportedListenerError(f"no response render for inbound_format={inbound_format}")


def dispatch_dump_sync(req: "LLMRenderInput", *, provider_type: str) -> bytes:
    """Synchronous outbound dispatcher.

    Routes :class:`LLMRenderInput` to the matching adapter's ``render``
    classmethod. Each adapter renders ``req``'s typed fields (messages,
    settings, raw_extras, request_parameters, model, stream) to wire bytes.
    """
    if provider_type in _ANTHROPIC_COMPATIBLE:
        from ccproxy.lightllm.adapters.anthropic import AnthropicAdapter

        logger.debug("dispatch_dump_sync: provider_type=%s → AnthropicAdapter", provider_type)
        return AnthropicAdapter.render(req)
    if provider_type == "openai":
        from ccproxy.lightllm.adapters.openai_chat import OpenAIChatAdapter

        logger.debug("dispatch_dump_sync: provider_type=%s → OpenAIChatAdapter", provider_type)
        return OpenAIChatAdapter.render(req)
    if provider_type == "openai_responses":
        from ccproxy.lightllm.adapters.openai_responses import OpenAIResponsesAdapter

        logger.debug("dispatch_dump_sync: provider_type=%s → OpenAIResponsesAdapter", provider_type)
        return OpenAIResponsesAdapter.render(req)
    if provider_type in _GOOGLE_COMPATIBLE:
        from ccproxy.lightllm.adapters.google import GoogleAdapter

        logger.debug("dispatch_dump_sync: provider_type=%s → GoogleAdapter", provider_type)
        return GoogleAdapter.render(req)
    if provider_type == "perplexity_pro":
        from ccproxy.lightllm.adapters.perplexity import PerplexityAdapter

        logger.debug("dispatch_dump_sync: provider_type=%s → PerplexityAdapter", provider_type)
        return PerplexityAdapter.render(req)
    if provider_type == "openai_conversations":
        from ccproxy.lightllm.adapters.openai_conversations import OpenAIConversationsAdapter

        logger.debug("dispatch_dump_sync: provider_type=%s → OpenAIConversationsAdapter", provider_type)
        return OpenAIConversationsAdapter.render(req)

    logger.warning("dispatch_dump_sync: no outbound renderer for provider_type=%r — request will fail", provider_type)
    raise UnsupportedUpstreamError(f"no outbound renderer for provider_type={provider_type!r}")
