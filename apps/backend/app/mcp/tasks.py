"""In-memory registry for long-running MCP operations.

Tailoring, generation and PDF export can outlast an MCP client's request
timeout. Tools start the work here, wait up to ``wait_seconds`` and otherwise
hand back a ``task_id`` the agent polls with ``get_task``. State is
process-local, which is safe because the backend runs a single process.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger(__name__)

TaskStatus = Literal["running", "succeeded", "failed", "cancelled"]
Presenter = Callable[[dict[str, Any]], dict[str, Any]]

DEFAULT_MAX_TASKS = 32
DEFAULT_RETENTION_SECONDS = 3600.0
GENERIC_TASK_ERROR = "The operation failed unexpectedly. Please try again."
# Large result fields handed out once: after the first delivery they are
# dropped from the retained record so the registry does not pin big payloads.
RELEASE_AFTER_DELIVERY_KEYS = ("content_base64",)


class TaskCapacityError(ToolError):
    """Raised when every slot holds a running task or an unread result."""


@dataclass
class TaskRecord:
    """State of one long-running operation."""

    task_id: str
    name: str
    created_at: float
    status: TaskStatus = "running"
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    delivered: bool = False
    # Shapes the stored result for get_task (the starting call's view of it).
    presenter: Presenter | None = field(default=None, repr=False)
    _task: asyncio.Task[dict[str, Any]] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for tool output; the result is included only on success."""
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "name": self.name,
            "status": self.status,
        }
        if self.status == "succeeded" and self.result is not None:
            payload["result"] = self.presenter(self.result) if self.presenter else self.result
        if self.error is not None:
            payload["error"] = self.error
        return payload

    def mark_delivered(self) -> None:
        """Record that the client has seen the final state; release payloads."""
        if self.status == "running":
            return
        self.delivered = True
        if self.result is None:
            return
        released = [key for key in RELEASE_AFTER_DELIVERY_KEYS if key in self.result]
        if released:
            self.result = {k: v for k, v in self.result.items() if k not in released}
            self.result["payload_released"] = True


class TaskRegistry:
    """Bounded set of background operations with retained results."""

    def __init__(
        self,
        max_tasks: int = DEFAULT_MAX_TASKS,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._records: OrderedDict[str, TaskRecord] = OrderedDict()
        self._max_tasks = max_tasks
        self._retention_seconds = retention_seconds
        self._clock = clock

    def start(
        self,
        name: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        presenter: Presenter | None = None,
    ) -> TaskRecord:
        """Schedule ``operation`` and return its record immediately.

        ``presenter``, if given, shapes the stored result whenever the record
        is serialized with ``to_dict``.

        Raises:
            TaskCapacityError: If every slot holds a running operation or a
                finished result the client has not read yet.
        """
        self._prune()
        if len(self._records) >= self._max_tasks:
            raise TaskCapacityError(
                "Too many operations are running or have unread results. Poll "
                "get_task for finished ones or cancel one with cancel_task, then retry."
            )
        record = TaskRecord(
            task_id=uuid4().hex, name=name, created_at=self._clock(), presenter=presenter
        )

        async def runner() -> dict[str, Any]:
            return await operation()

        task = asyncio.create_task(runner(), name=f"mcp-{name}-{record.task_id}")
        record._task = task
        task.add_done_callback(lambda done: self._finish(record, done))
        self._records[record.task_id] = record
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        """Return a task's record, or None when unknown or expired."""
        self._prune()
        return self._records.get(task_id)

    async def wait(self, task_id: str, timeout: float) -> TaskRecord | None:
        """Wait up to ``timeout`` seconds for a task to settle; never cancels it."""
        record = self.get(task_id)
        if record is None or record._task is None or record.status != "running":
            return record
        done, _ = await asyncio.wait({record._task}, timeout=max(timeout, 0))
        if done:
            # The done-callback runs on the next loop iteration; settle now so
            # callers see the final state as soon as the task has finished.
            self._finish(record, record._task)
        return record

    def cancel(self, task_id: str) -> TaskRecord | None:
        """Request cancellation of a running task; returns its record."""
        record = self.get(task_id)
        if record is not None and record.status == "running" and record._task is not None:
            record._task.cancel()
        return record

    async def shutdown(self) -> None:
        """Cancel every running task and wait for them to unwind."""
        running = [
            record._task
            for record in self._records.values()
            if record._task is not None and not record._task.done()
        ]
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        for record in self._records.values():
            if record._task is not None:
                self._finish(record, record._task)

    def _finish(self, record: TaskRecord, task: asyncio.Task[dict[str, Any]]) -> None:
        if record.status != "running" or not task.done():
            return
        record.finished_at = self._clock()
        if task.cancelled():
            record.status = "cancelled"
            record.error = "The operation was cancelled."
            return
        exc = task.exception()
        if exc is None:
            record.status = "succeeded"
            record.result = task.result()
        elif isinstance(exc, ToolError):
            record.status = "failed"
            record.error = str(exc)
        else:
            record.status = "failed"
            record.error = GENERIC_TASK_ERROR
            logger.error(
                "MCP task %s (%s) failed", record.task_id, record.name, exc_info=exc
            )

    def _prune(self) -> None:
        now = self._clock()
        expired = [
            task_id
            for task_id, record in self._records.items()
            if record.finished_at is not None
            and now - record.finished_at >= self._retention_seconds
        ]
        for task_id in expired:
            del self._records[task_id]
        # At capacity: make room only by dropping results the client has
        # already read (oldest first); unread results are kept until they
        # expire, so a slow poller never loses its answer.
        if len(self._records) >= self._max_tasks:
            for task_id in [key for key, value in self._records.items() if value.delivered]:
                if len(self._records) < self._max_tasks:
                    break
                del self._records[task_id]
