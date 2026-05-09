"""Tests for the task dispatch pipe consumer."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, cast

import pytest

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.task_manager.acp_adapter import AcpCallResult
from nexus.task_manager.dispatch_consumer import TaskDispatchPipeConsumer


class _BlockingReadNx:
    def __init__(self) -> None:
        self.closed = False
        self.read_started = threading.Event()
        self.read_timeouts: list[int | None] = []
        self.setattr_calls: list[tuple[str, dict[str, Any]]] = []

    def sys_setattr(self, path: str, **attrs: Any) -> None:
        self.setattr_calls.append((path, attrs))

    def sys_read(self, path: str, *, timeout_ms: int | None = None) -> bytes:
        self.read_started.set()
        self.read_timeouts.append(timeout_ms)
        time.sleep(0.5)
        if self.closed:
            raise NexusFileNotFoundError(path=path)
        return b""

    def sys_write(self, path: str, data: bytes) -> None:  # noqa: ARG002
        return None

    def pipe_close(self, path: str) -> None:  # noqa: ARG002
        self.closed = True


class _FakeTaskService:
    def __init__(
        self,
        task: dict[str, Any],
        comments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.task = dict(task)
        self.comments = list(comments or [])
        self.audit_entries: list[dict[str, str]] = []

    async def get_task(self, task_id: str) -> dict[str, Any]:
        assert task_id == self.task["id"]
        return dict(self.task)

    async def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        assert task_id == self.task["id"]
        self.task.update(fields)
        return dict(self.task)

    async def create_audit_entry(
        self,
        task_id: str,
        action: str,
        *,
        actor: str = "system",
        detail: str = "",
    ) -> dict[str, str]:
        assert task_id == self.task["id"]
        entry = {"action": action, "actor": actor, "detail": detail}
        self.audit_entries.append(entry)
        return entry

    async def create_comment(
        self,
        task_id: str,
        author: str,
        content: str,
        artifact_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        assert task_id == self.task["id"]
        comment = {
            "task_id": task_id,
            "author": author,
            "content": content,
            "artifact_refs": artifact_refs or [],
        }
        self.comments.append(comment)
        return comment

    async def get_comments(self, task_id: str) -> list[dict[str, Any]]:
        assert task_id == self.task["id"]
        return list(self.comments)


class _FakeAcpService:
    def __init__(self, *results: AcpCallResult) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def call_agent(self, **kwargs: Any) -> AcpCallResult:
        self.calls.append(kwargs)
        assert self._results
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_blocking_pipe_read_does_not_block_event_loop() -> None:
    nx = _BlockingReadNx()
    consumer = TaskDispatchPipeConsumer()
    consumer.set_nx(cast(Any, nx))

    await consumer.start()
    try:

        async def _wait_for_read() -> None:
            while not nx.read_started.is_set():
                await asyncio.sleep(0.01)

        started_at = time.perf_counter()
        await asyncio.wait_for(_wait_for_read(), timeout=1.0)
        assert time.perf_counter() - started_at < 0.25
        assert nx.read_timeouts == [0]
    finally:
        await consumer.stop()


def test_worker_prompt_requests_direct_response_without_server_credentials() -> None:
    consumer = TaskDispatchPipeConsumer()
    prompt = consumer._build_worker_prompt("task-123", "do the thing")

    assert "Return your work output directly in your final response." in prompt
    assert "curl -X" not in prompt
    assert "Authorization:" not in prompt


@pytest.mark.asyncio
async def test_start_worker_records_agent_output_in_process() -> None:
    task_id = "task-123"
    task_service = _FakeTaskService(
        {"id": task_id, "instruction": "do the thing", "status": "created", "worker_type": "claude"}
    )
    acp_service = _FakeAcpService(
        AcpCallResult(pid="pid-1", agent_id="claude", response="Implemented the task.")
    )
    consumer = TaskDispatchPipeConsumer(acp_service=acp_service)
    consumer.set_task_service(cast(Any, task_service))

    await consumer._start_worker(task_id)

    assert task_service.comments == [
        {
            "task_id": task_id,
            "author": "worker",
            "content": "Implemented the task.",
            "artifact_refs": [],
        }
    ]
    assert task_service.task["status"] == "in_review"
    assert task_service.task["worker_pid"] == "pid-1"
    assert task_service.task["agent_name"] == "claude"
    assert "curl -X" not in acp_service.calls[0]["prompt"]
    assert "Authorization:" not in acp_service.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_copilot_review_records_feedback_and_rejects_in_process() -> None:
    task_id = "task-123"
    task_service = _FakeTaskService(
        {
            "id": task_id,
            "instruction": "do the thing",
            "status": "in_review",
            "worker_type": "claude",
        },
        comments=[{"task_id": task_id, "author": "worker", "content": "Here is the output."}],
    )
    acp_service = _FakeAcpService(
        AcpCallResult(
            pid="pid-2",
            agent_id="gemini",
            response="DECISION: reject\nREVIEW: Missing a required test.",
        )
    )
    consumer = TaskDispatchPipeConsumer(acp_service=acp_service)
    consumer.set_task_service(cast(Any, task_service))

    await consumer._copilot_review(task_id)

    assert task_service.comments[-1] == {
        "task_id": task_id,
        "author": "copilot",
        "content": "Missing a required test.",
        "artifact_refs": [],
    }
    assert task_service.task["status"] == "failed"
    assert "curl -X" not in acp_service.calls[0]["prompt"]
    assert "Authorization:" not in acp_service.calls[0]["prompt"]


def test_bricks_module_aliases_live_dispatch_consumer() -> None:
    from nexus.bricks.task_manager.dispatch_consumer import (
        TaskDispatchPipeConsumer as BricksTaskDispatchPipeConsumer,
    )
    from nexus.task_manager.dispatch_consumer import (
        TaskDispatchPipeConsumer as LiveTaskDispatchPipeConsumer,
    )

    assert BricksTaskDispatchPipeConsumer is LiveTaskDispatchPipeConsumer
