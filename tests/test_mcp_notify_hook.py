"""Tests for inject_mcp_notifications pipeline hook."""

import json

from ccproxy.hooks.inject_mcp_notifications import (
    inject_mcp_notifications,
    inject_mcp_notifications_guard,
)
from ccproxy.mcp.buffer import get_buffer
from ccproxy.pipeline.context import Context


def make_ctx(messages=None, session_id=None):
    metadata = {}
    if session_id:
        metadata["session_id"] = session_id
    return Context(
        messages=messages if messages is not None else [],
        metadata=metadata,
    )


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
    assert result.messages == messages


def test_noop_no_session_id():
    messages = [user_msg("hello")]
    ctx = make_ctx(messages=messages, session_id=None)
    get_buffer().append("task-1", "sess-1", {"type": "output"})
    result = inject_mcp_notifications(ctx, {})
    assert result.messages == messages


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

    assert assistant["role"] == "assistant"
    assert len(assistant["content"]) == 1
    block = assistant["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "tasks_get"
    assert block["input"] == {"taskId": "task-1"}

    assert user["role"] == "user"
    assert len(user["content"]) == 1
    tr = user["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == block["id"]
    assert json.loads(tr["content"]) == events

    assert final == user_msg("run it")


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

    # sess-A's events injected, sess-B's preserved
    assert len(result.messages) == 3
    block = result.messages[0]["content"][0]
    assert block["input"] == {"taskId": "task-a"}

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
    assert result.messages[-1] == user_msg("go")

    roles = [m["role"] for m in result.messages[:-1]]
    assert roles == ["assistant", "user", "assistant", "user"]

    task_ids = {result.messages[i]["content"][0]["input"]["taskId"] for i in [0, 2]}
    assert task_ids == {"task-1", "task-2"}


def test_insertion_before_final_user_message():
    prior = [assistant_msg("prev"), user_msg("earlier"), assistant_msg("ok")]
    final = user_msg("final")
    messages = [*prior, final]

    buf = get_buffer()
    buf.append("task-1", "sess-1", {"type": "exit", "code": 0})

    ctx = make_ctx(messages=messages, session_id="sess-1")
    result = inject_mcp_notifications(ctx, {})

    assert result.messages[:3] == prior
    assert result.messages[-1] == final
    assert result.messages[3]["role"] == "assistant"
    assert result.messages[4]["role"] == "user"


def test_tool_use_id_format():
    buf = get_buffer()
    buf.append("task-1", "sess-1", {"type": "output"})

    ctx = make_ctx(messages=[user_msg()], session_id="sess-1")
    result = inject_mcp_notifications(ctx, {})

    tool_use_id = result.messages[0]["content"][0]["id"]
    assert tool_use_id.startswith("toolu_")

    tr_id = result.messages[1]["content"][0]["tool_use_id"]
    assert tr_id == tool_use_id
