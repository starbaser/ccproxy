"""Database storage layer for HTTP/HTTPS traffic traces."""

import asyncio
import logging
from typing import Any

from prisma import Prisma
from prisma.fields import Base64, Json

logger = logging.getLogger(__name__)


def _convert_for_prisma(data: dict[str, Any]) -> dict[str, Any]:
    """Convert Python types to Prisma-compatible types.

    Args:
        data: Dict with raw Python types

    Returns:
        Dict with Prisma-compatible types (Json, Base64)
    """
    result = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[key] = Json(value)
        elif isinstance(value, bytes):
            result[key] = Base64.encode(value)
        else:
            result[key] = value
    return result


class TraceStorage:
    """Manage traffic trace storage using Prisma async client."""

    def __init__(self, database_url: str) -> None:
        """Initialize trace storage.

        Args:
            database_url: PostgreSQL connection URL
        """
        self.database_url = database_url
        self.client = Prisma(datasource={"url": database_url})
        self._write_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._worker_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()

    async def connect(self) -> None:
        """Initialize Prisma connection and start background worker."""
        await self.client.connect()
        logger.info("Connected to database")

        # Start background worker for buffered writes
        self._worker_task = asyncio.create_task(self._write_worker())

    async def disconnect(self) -> None:
        """Close Prisma connection and stop background worker."""
        # Signal shutdown and wait for queue to drain
        self._shutdown.set()

        if self._worker_task:
            await self._worker_task

        await self.client.disconnect()
        logger.info("Disconnected from database")

    async def _write_worker(self) -> None:
        """Background worker for processing buffered writes."""
        while not self._shutdown.is_set() or not self._write_queue.empty():
            try:
                # Wait for item with timeout to check shutdown flag
                operation = await asyncio.wait_for(self._write_queue.get(), timeout=1.0)

                # Process the operation
                op_type = operation.get("type")
                data = operation.get("data", {})

                if op_type == "create":
                    await self._do_create_trace(data)
                elif op_type == "complete":
                    trace_id = operation.get("trace_id")
                    if trace_id:
                        await self._do_complete_trace(trace_id, data)

                self._write_queue.task_done()

            except TimeoutError:
                # Timeout is expected - allows checking shutdown flag
                continue
            except Exception as e:
                logger.error("Error in write worker: %s", e, exc_info=True)

    async def create_trace(self, data: dict[str, Any]) -> str:
        """Queue creation of a new trace record.

        Args:
            data: Trace data including trace_id, method, url, headers, etc.

        Returns:
            Trace ID
        """
        trace_id = str(data.get("trace_id", ""))
        if not trace_id:
            raise ValueError("trace_id is required in trace data")

        # Queue the create operation (non-blocking)
        try:
            self._write_queue.put_nowait({"type": "create", "data": data})
        except asyncio.QueueFull:
            logger.warning("Write queue full, dropping trace %s", trace_id)

        return trace_id

    async def _do_create_trace(self, data: dict[str, Any]) -> None:
        """Create a new trace record in the database.

        Args:
            data: Trace data
        """
        try:
            prisma_data = _convert_for_prisma(data)
            await self.client.ccproxy_httptraces.create(data=prisma_data)
            logger.debug("Created trace: %s", data.get("trace_id"))
        except Exception as e:
            logger.error("Failed to create trace %s: %s", data.get("trace_id"), e, exc_info=True)

    async def complete_trace(self, trace_id: str, data: dict[str, Any]) -> None:
        """Queue update of trace record with response data.

        Args:
            trace_id: Trace identifier
            data: Response data including status_code, response_headers, response_body, etc.
        """
        # Queue the complete operation (non-blocking)
        try:
            self._write_queue.put_nowait({"type": "complete", "trace_id": trace_id, "data": data})
        except asyncio.QueueFull:
            logger.warning("Write queue full, dropping completion for trace %s", trace_id)

    async def _do_complete_trace(self, trace_id: str, data: dict[str, Any]) -> None:
        """Update trace record with response data.

        Args:
            trace_id: Trace identifier
            data: Response data
        """
        try:
            prisma_data = _convert_for_prisma(data)
            await self.client.ccproxy_httptraces.update(where={"trace_id": trace_id}, data=prisma_data)
            logger.debug("Completed trace: %s", trace_id)
        except Exception as e:
            logger.error("Failed to complete trace %s: %s", trace_id, e, exc_info=True)

    async def get_traces(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query traces with optional filters.

        Args:
            filters: Optional filter conditions
            limit: Maximum number of records to return
            offset: Number of records to skip

        Returns:
            List of trace records
        """
        try:
            # Build where clause from filters
            where = filters or {}

            # Query with pagination
            traces = await self.client.ccproxy_httptraces.find_many(
                where=where,
                take=limit,
                skip=offset,
                order={"created_at": "desc"},
            )

            return [trace.model_dump() for trace in traces]
        except Exception as e:
            logger.error("Failed to query traces: %s", e, exc_info=True)
            return []
