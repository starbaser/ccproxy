"""Thread-safe notification buffer for MCP terminal events."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_EVENTS = 50
DEFAULT_TTL_SECONDS = 600


@dataclass
class TaskBuffer:
    """Buffer for a single task's events."""

    task_id: str
    session_id: str
    events: list[dict[str, Any]] = field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    last_seen: float = field(default_factory=time.time)


class NotificationBuffer:
    """Thread-safe buffer for MCP notification events, keyed by task_id."""

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        self._buffers: dict[str, TaskBuffer] = {}
        self._lock = threading.Lock()
        self._max_events = max_events

    def append(self, task_id: str, session_id: str, event: dict[str, Any]) -> None:
        """Append an event to the buffer for a task. Creates buffer if needed."""
        with self._lock:
            buf = self._buffers.get(task_id)
            if buf is None:
                buf = TaskBuffer(task_id=task_id, session_id=session_id)
                self._buffers[task_id] = buf
            buf.events.append(event)
            buf.last_seen = time.time()
            # Cap at max_events, drop oldest
            if len(buf.events) > self._max_events:
                buf.events = buf.events[-self._max_events :]

    def drain_session(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        """Atomically drain all events for a session. Returns {task_id: events}."""
        result: dict[str, list[dict[str, Any]]] = {}
        with self._lock:
            to_remove: list[str] = []
            for task_id, buf in self._buffers.items():
                if buf.session_id == session_id and buf.events:
                    result[task_id] = buf.events
                    buf.events = []
                    to_remove.append(task_id)
            for task_id in to_remove:
                del self._buffers[task_id]
        return result

    def expire(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
        """Remove entries older than ttl_seconds. Returns count removed."""
        now = time.time()
        removed = 0
        with self._lock:
            expired = [tid for tid, buf in self._buffers.items() if now - buf.last_seen > ttl_seconds]
            for tid in expired:
                del self._buffers[tid]
                removed += 1
        return removed

    def has_events_for_session(self, session_id: str) -> bool:
        """Check if any task with matching session_id has buffered events."""
        with self._lock:
            return any(buf.session_id == session_id and buf.events for buf in self._buffers.values())

    def is_empty(self) -> bool:
        with self._lock:
            return len(self._buffers) == 0


_buffer: NotificationBuffer | None = None
_buffer_lock = threading.Lock()


def get_buffer() -> NotificationBuffer:
    """Creates buffer if needed."""
    global _buffer
    if _buffer is None:
        with _buffer_lock:
            if _buffer is None:
                _buffer = NotificationBuffer()
    return _buffer


def clear_buffer() -> None:
    """Reset the singleton buffer. For testing."""
    global _buffer
    with _buffer_lock:
        _buffer = None
