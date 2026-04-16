"""Tests for the MCP /notify endpoint."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ccproxy.mcp.buffer import get_buffer
from ccproxy.mcp.routes import router as mcp_router


@pytest.fixture
def app() -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(mcp_router)
    return test_app


@pytest.mark.asyncio
async def test_valid_event_returns_200(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/mcp/notify",
            json={"task_id": "t1", "session_id": "s1", "event": {"type": "output", "text": "hello"}},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_valid_event_stored_in_buffer(app: FastAPI) -> None:
    event = {"type": "output", "text": "hello"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/mcp/notify",
            json={"task_id": "t1", "session_id": "s1", "event": event},
        )

    buf = get_buffer()
    assert not buf.is_empty()
    drained = buf.drain_session("s1")
    assert drained == {"t1": [event]}


@pytest.mark.asyncio
async def test_missing_task_id_returns_422(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/mcp/notify",
            json={"session_id": "s1", "event": {"type": "output"}},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_missing_session_id_returns_422(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/mcp/notify",
            json={"task_id": "t1", "event": {"type": "output"}},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_missing_event_returns_422(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/mcp/notify",
            json={"task_id": "t1", "session_id": "s1"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_multiple_posts_accumulate_in_buffer(app: FastAPI) -> None:
    events = [
        {"type": "output", "text": "line1"},
        {"type": "output", "text": "line2"},
        {"type": "exit", "code": 0},
    ]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for event in events:
            await client.post(
                "/mcp/notify",
                json={"task_id": "t1", "session_id": "s1", "event": event},
            )

    drained = get_buffer().drain_session("s1")
    assert drained == {"t1": events}


@pytest.mark.asyncio
async def test_different_session_ids_separated_in_buffer(app: FastAPI) -> None:
    event_a = {"type": "output", "text": "from session A"}
    event_b = {"type": "output", "text": "from session B"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/mcp/notify",
            json={"task_id": "t1", "session_id": "session-a", "event": event_a},
        )
        await client.post(
            "/mcp/notify",
            json={"task_id": "t2", "session_id": "session-b", "event": event_b},
        )

    buf = get_buffer()
    drained_a = buf.drain_session("session-a")
    drained_b = buf.drain_session("session-b")

    assert drained_a == {"t1": [event_a]}
    assert drained_b == {"t2": [event_b]}
