"""Tests for the task dispatch pipe consumer."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, cast

import pytest

from nexus.contracts.exceptions import NexusFileNotFoundError
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
