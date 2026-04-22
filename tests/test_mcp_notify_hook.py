"""Tests for inject_mcp_notifications pipeline hook."""

import json
from unittest.mock import MagicMock

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from ccproxy.hooks.inject_mcp_notifications import (
    inject_mcp_notifications,
    inject_mcp_notifications_guard,
)
from ccproxy.mcp.buffer import get_buffer
from ccproxy.pipeline.context import Context


def make_ctx(messages=None, session_id=None):
    body: dict = {"model": "test-model", "messages": messages if messages is not None else []}
    flow = MagicMock()
    flow.id = "test-id"
    flow.request.content = json.dumps(body).encode()
    flow.request.headers = {}
    flow.metadata = {}
    if session_id:
        flow.metadata["ccproxy.session_id"] = session_id
    return Context.from_flow(flow)


def user_msg(text="hello"):
    return {"role": "user", "content": text}


def assistant_msg(text="hi"):
    return {"role": "assistant", "content": text}


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------


def test_guard_false_no_messages():
    ctx = make_ctx(messages=[], session_id="sess-1")
    assert inject_mcp_notifications_guard(ctx) is False


def test_guard_false_no_session_id():
    ctx = make_ctx(messages=[user_msg()], session_id=None)
    assert inject_mcp_notifications_guard(ctx) is False


def test_guard_false_buffer_empty_for_session():
    buf = get_buffer()
    buf.append("task-other", "sess-other", {"type": "output"})
    ctx = make_ctx(messages=[user_msg()], session_id="sess-1")
    assert inject_mcp_notifications_guard(ctx) is False


def test_guard_true_buffer_has_events():
    buf = get_buffer()
    buf.append("task-1", "sess-1", {"type": "output", "text": "done"})
    ctx = make_ctx(messages=[user_msg()], session_id="sess-1")
    assert inject_mcp_notifications_guard(ctx) is True


# ---------------------------------------------------------------------------
# Hook no-op tests
# ---------------------------------------------------------------------------


def test_noop_empty_buffer():
    messages = [user_msg("hello")]
    ctx = make_ctx(messages=messages, session_id="sess-1")
    result = inject_mcp_notifications(ctx, {})
    assert len(result.messages) == 1
    assert isinstance(result.messages[0], ModelRequest)


def test_noop_no_session_id():
    messages = [user_msg("hello")]
    ctx = make_ctx(messages=messages, session_id=None)
    get_buffer().append("task-1", "sess-1", {"type": "output"})
    result = inject_mcp_notifications(ctx, {})
    assert len(result.messages) == 1


# ---------------------------------------------------------------------------
# Injection tests
# ---------------------------------------------------------------------------


def test_injects_pair_for_single_task():
    buf = get_buffer()
    events = [
        {"type": "output", "text": "line 1"},
        {"type": "output", "text": "line 2"},
        {"type": "exit", "code": 0},
    ]
    for ev in events:
        buf.append("task-1", "sess-1", ev)

    ctx = make_ctx(messages=[user_msg("run it")], session_id="sess-1")
    result = inject_mcp_notifications(ctx, {})

    # 2 injected messages + 1 original = 3 total
    assert len(result.messages) == 3

    assistant = result.messages[0]
    user = result.messages[1]
    final = result.messages[2]

    assert isinstance(assistant, ModelResponse)
    assert len(assistant.parts) == 1
    tc = assistant.parts[0]
    assert isinstance(tc, ToolCallPart)
    assert tc.tool_name == "tasks_get"
    assert tc.args == {"taskId": "task-1"}

    assert isinstance(user, ModelRequest)
    assert len(user.parts) == 1
    tr = user.parts[0]
    assert isinstance(tr, ToolReturnPart)
    assert tr.tool_call_id == tc.tool_call_id
    assert json.loads(tr.content) == events

    assert isinstance(final, ModelRequest)
    assert isinstance(final.parts[0], UserPromptPart)


def test_buffer_drained_after_inject():
    buf = get_buffer()
    buf.append("task-1", "sess-1", {"type": "output"})

    ctx = make_ctx(messages=[user_msg()], session_id="sess-1")
    inject_mcp_notifications(ctx, {})

    assert not buf.has_events_for_session("sess-1")


def test_session_isolation():
    buf = get_buffer()
    buf.append("task-a", "sess-A", {"type": "output", "text": "a"})
    buf.append("task-b", "sess-B", {"type": "output", "text": "b"})

    ctx = make_ctx(messages=[user_msg("from A")], session_id="sess-A")
    result = inject_mcp_notifications(ctx, {})

    assert len(result.messages) == 3
    assistant = result.messages[0]
    assert isinstance(assistant, ModelResponse)
    tc = assistant.parts[0]
    assert isinstance(tc, ToolCallPart)
    assert tc.args == {"taskId": "task-a"}

    assert buf.has_events_for_session("sess-B")
    assert not buf.has_events_for_session("sess-A")


def test_multiple_task_ids_same_session():
    buf = get_buffer()
    buf.append("task-1", "sess-1", {"type": "output", "text": "t1"})
    buf.append("task-2", "sess-1", {"type": "output", "text": "t2"})

    ctx = make_ctx(messages=[user_msg("go")], session_id="sess-1")
    result = inject_mcp_notifications(ctx, {})

    # 2 tasks x 2 messages each + 1 original = 5
    assert len(result.messages) == 5
    assert isinstance(result.messages[-1], ModelRequest)

    # Alternating ModelResponse / ModelRequest for injected pairs
    assert isinstance(result.messages[0], ModelResponse)
    assert isinstance(result.messages[1], ModelRequest)
    assert isinstance(result.messages[2], ModelResponse)
    assert isinstance(result.messages[3], ModelRequest)

    task_ids = set()
    for i in [0, 2]:
        tc = result.messages[i].parts[0]
        assert isinstance(tc, ToolCallPart)
        task_ids.add(tc.args["taskId"])
    assert task_ids == {"task-1", "task-2"}


def test_insertion_before_final_user_message():
    prior = [assistant_msg("prev"), user_msg("earlier"), assistant_msg("ok")]
    final = user_msg("final")
    messages = [*prior, final]

    buf = get_buffer()
    buf.append("task-1", "sess-1", {"type": "exit", "code": 0})

    ctx = make_ctx(messages=messages, session_id="sess-1")
    result = inject_mcp_notifications(ctx, {})

    # First 3 are original prior messages, then 2 injected, then final
    assert len(result.messages) == 6
    assert isinstance(result.messages[3], ModelResponse)  # injected assistant
    assert isinstance(result.messages[4], ModelRequest)    # injected user
    final_msg = result.messages[-1]
    assert isinstance(final_msg, ModelRequest)
    assert isinstance(final_msg.parts[0], UserPromptPart)


def test_tool_use_id_format():
    buf = get_buffer()
    buf.append("task-1", "sess-1", {"type": "output"})

    ctx = make_ctx(messages=[user_msg()], session_id="sess-1")
    result = inject_mcp_notifications(ctx, {})

    assistant = result.messages[0]
    assert isinstance(assistant, ModelResponse)
    tc = assistant.parts[0]
    assert isinstance(tc, ToolCallPart)
    assert tc.tool_call_id.startswith("toolu_")

    user = result.messages[1]
    assert isinstance(user, ModelRequest)
    tr = user.parts[0]
    assert isinstance(tr, ToolReturnPart)
    assert tr.tool_call_id == tc.tool_call_id
