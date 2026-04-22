"""Bidirectional wire format <-> Pydantic AI type conversion.

Parses LLM API request bodies (Anthropic Messages API, OpenAI Chat
Completions) into Pydantic AI typed objects and serializes them back.
The body is self-describing — format detected from structure.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.messages import (
    CachePoint,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.tools import ToolDefinition

from ccproxy.pipeline.types import CachedSystemPromptPart, CachedToolDefinition

# ---------------------------------------------------------------------------
# Parse: wire format dict -> Pydantic AI types
# ---------------------------------------------------------------------------


def parse_messages(raw_messages: list[dict[str, Any]]) -> list[ModelMessage]:
    """Parse a wire-format messages list into Pydantic AI ModelMessage objects."""
    result: list[ModelMessage] = []
    for msg in raw_messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "assistant":
            result.append(_parse_assistant_message(content))
        else:
            result.append(_parse_request_message(msg))
    return result


def parse_system(raw_system: str | list[dict[str, Any]] | None) -> list[SystemPromptPart]:
    """Parse wire-format system prompts into SystemPromptPart objects."""
    if raw_system is None:
        return []
    if isinstance(raw_system, str):
        return [SystemPromptPart(content=raw_system)] if raw_system else []
    parts: list[SystemPromptPart] = []
    for block in raw_system:
        text = block.get("text", "")
        cc = block.get("cache_control")
        if cc:
            parts.append(CachedSystemPromptPart(content=text, cache_control=cc))
        else:
            parts.append(SystemPromptPart(content=text))
    return parts


def parse_tools(raw_tools: list[dict[str, Any]]) -> list[ToolDefinition]:
    """Parse wire-format tool definitions into ToolDefinition objects."""
    result: list[ToolDefinition] = []
    for tool in raw_tools:
        # Anthropic: input_schema, OpenAI: parameters (under function)
        if "function" in tool:
            func = tool["function"]
            name = func.get("name", "")
            desc = func.get("description")
            schema = func.get("parameters", {})
            cc = None
        else:
            name = tool.get("name", "")
            desc = tool.get("description")
            schema = tool.get("input_schema", {})
            cc = tool.get("cache_control")

        if cc:
            result.append(CachedToolDefinition(
                name=name, description=desc, parameters_json_schema=schema, cache_control=cc,
            ))
        else:
            result.append(ToolDefinition(name=name, description=desc, parameters_json_schema=schema))
    return result


# ---------------------------------------------------------------------------
# Serialize: Pydantic AI types -> wire format dict
# ---------------------------------------------------------------------------


def serialize_messages(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    """Serialize Pydantic AI ModelMessage objects to wire-format messages list."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            result.extend(_serialize_request(msg))
        elif isinstance(msg, ModelResponse):
            result.append(_serialize_response(msg))
    return result


def serialize_system(parts: list[SystemPromptPart]) -> str | list[dict[str, Any]]:
    """Serialize SystemPromptPart objects to wire-format system prompt."""
    if not parts:
        return []
    if len(parts) == 1 and not isinstance(parts[0], CachedSystemPromptPart):
        return parts[0].content
    blocks: list[dict[str, Any]] = []
    for part in parts:
        block: dict[str, Any] = {"type": "text", "text": part.content}
        if isinstance(part, CachedSystemPromptPart) and part.cache_control:
            block["cache_control"] = part.cache_control
        blocks.append(block)
    return blocks


def serialize_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """Serialize ToolDefinition objects to wire-format tool list."""
    result: list[dict[str, Any]] = []
    for tool in tools:
        entry: dict[str, Any] = {
            "name": tool.name,
            "input_schema": tool.parameters_json_schema,
        }
        if tool.description:
            entry["description"] = tool.description
        if isinstance(tool, CachedToolDefinition) and tool.cache_control:
            entry["cache_control"] = tool.cache_control
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Internal: parse helpers
# ---------------------------------------------------------------------------


def _parse_request_message(msg: dict[str, Any]) -> ModelRequest:
    """Parse a user/system role message into ModelRequest."""
    content = msg.get("content", "")
    parts: list[SystemPromptPart | UserPromptPart | ToolReturnPart] = []

    if isinstance(content, str):
        if msg.get("role") == "system":
            parts.append(SystemPromptPart(content=content))
        else:
            parts.append(UserPromptPart(content=content))
        return ModelRequest(parts=parts)

    if not isinstance(content, list):
        return ModelRequest(parts=[])

    # Anthropic: content is list of typed blocks
    # Accumulate user content items for a single UserPromptPart
    user_content_items: list[UserContent] = []

    for block in content:
        block_type = block.get("type", "")

        if block_type == "tool_result":
            # Flush any accumulated user content first
            if user_content_items:
                parts.append(UserPromptPart(content=list(user_content_items)))
                user_content_items = []
            parts.append(_parse_tool_result_block(block))

        elif block_type == "text":
            user_content_items.append(block.get("text", ""))
            cc = block.get("cache_control")
            if cc:
                user_content_items.append(_cache_control_to_cache_point(cc))

        elif block_type == "image":
            source = block.get("source", {})
            user_content_items.append(source.get("data", ""))
            cc = block.get("cache_control")
            if cc:
                user_content_items.append(_cache_control_to_cache_point(cc))

        else:
            # Unknown block type — store as text representation
            user_content_items.append(str(block))

    if user_content_items:
        parts.append(UserPromptPart(content=list(user_content_items)))

    return ModelRequest(parts=parts)


def _parse_tool_result_block(block: dict[str, Any]) -> ToolReturnPart:
    """Parse an Anthropic tool_result content block."""
    content = block.get("content", "")
    if isinstance(content, list):
        # Multi-block tool result: extract text parts
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        content = "\n".join(texts) if texts else str(content)
    return ToolReturnPart(
        tool_name="",  # wire format doesn't carry tool_name in tool_result
        content=content,
        tool_call_id=block.get("tool_use_id", ""),
    )


def _parse_assistant_message(content: str | list[dict[str, Any]]) -> ModelResponse:
    """Parse an assistant role message into ModelResponse."""
    if isinstance(content, str):
        return ModelResponse(parts=[TextPart(content=content)])

    parts: list[ModelResponsePart] = []
    for block in content:
        block_type = block.get("type", "")
        if block_type == "text":
            parts.append(TextPart(content=block.get("text", "")))
        elif block_type == "tool_use":
            parts.append(ToolCallPart(
                tool_name=block.get("name", ""),
                args=block.get("input"),
                tool_call_id=block.get("id", ""),
            ))
        elif block_type == "thinking":
            parts.append(ThinkingPart(
                content=block.get("thinking", ""),
                signature=block.get("signature"),
            ))
        elif block_type == "redacted_thinking":
            parts.append(ThinkingPart(
                content="",
                id="redacted_thinking",
                signature=block.get("data"),
            ))
        else:
            # Unknown block — store as text
            parts.append(TextPart(content=str(block)))

    return ModelResponse(parts=parts) if parts else ModelResponse(parts=[TextPart(content="")])


def _cache_control_to_cache_point(cc: dict[str, Any]) -> CachePoint:
    """Convert a wire cache_control annotation to a CachePoint marker."""
    ttl = cc.get("ttl", "5m")
    if ttl not in ("5m", "1h"):
        ttl = "5m"
    return CachePoint(ttl=ttl)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Internal: serialize helpers
# ---------------------------------------------------------------------------


def _serialize_request(req: ModelRequest) -> list[dict[str, Any]]:
    """Serialize a ModelRequest into one or more wire-format messages.

    Groups parts by role: SystemPromptPart → role=system if standalone,
    otherwise all request parts → role=user blocks.
    """
    messages: list[dict[str, Any]] = []

    for part in req.parts:
        if isinstance(part, UserPromptPart):
            blocks = _serialize_user_prompt_content(part)
            messages.append({"role": "user", "content": blocks})
        elif isinstance(part, ToolReturnPart):
            block = _serialize_tool_return(part)
            # Tool results go in role=user messages
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
        elif isinstance(part, SystemPromptPart):
            # System parts in ModelRequest are unusual but possible
            messages.append({"role": "user", "content": [{"type": "text", "text": part.content}]})

    return messages


def _serialize_user_prompt_content(part: UserPromptPart) -> list[dict[str, Any]]:
    """Serialize UserPromptPart content into wire-format content blocks."""
    if isinstance(part.content, str):
        return [{"type": "text", "text": part.content}]

    blocks: list[dict[str, Any]] = []
    for item in part.content:
        if isinstance(item, CachePoint):
            # Apply cache_control to the preceding block
            if blocks:
                blocks[-1]["cache_control"] = {"type": "ephemeral"}
                if item.ttl != "5m":
                    blocks[-1]["cache_control"]["ttl"] = item.ttl
        elif isinstance(item, str):
            blocks.append({"type": "text", "text": item})
        else:
            # TextContent or other UserContent types
            content_str = getattr(item, "content", str(item))
            blocks.append({"type": "text", "text": content_str})

    return blocks


def _serialize_tool_return(part: ToolReturnPart) -> dict[str, Any]:
    """Serialize a ToolReturnPart into a wire-format tool_result block."""
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": part.tool_call_id,
    }
    if isinstance(part.content, str):
        block["content"] = part.content
    else:
        block["content"] = str(part.content)
    return block


def _serialize_response(resp: ModelResponse) -> dict[str, Any]:
    """Serialize a ModelResponse into a wire-format assistant message."""
    blocks: list[dict[str, Any]] = []
    for part in resp.parts:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.content})
        elif isinstance(part, ToolCallPart):
            block: dict[str, Any] = {
                "type": "tool_use",
                "id": part.tool_call_id,
                "name": part.tool_name,
                "input": part.args if isinstance(part.args, dict) else {},
            }
            blocks.append(block)
        elif isinstance(part, ThinkingPart):
            if part.id == "redacted_thinking":
                blocks.append({"type": "redacted_thinking", "data": part.signature})
            else:
                block = {"type": "thinking", "thinking": part.content}
                if part.signature:
                    block["signature"] = part.signature
                blocks.append(block)
        else:
            blocks.append({"type": "text", "text": str(part)})

    return {"role": "assistant", "content": blocks}
