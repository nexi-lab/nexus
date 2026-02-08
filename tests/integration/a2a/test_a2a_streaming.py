"""Integration tests for A2A SSE streaming endpoints.

Tests real SSE event delivery.  Streaming tests use a background task to
push a sentinel to the task manager's queue so the SSE generator finishes
cleanly — this avoids hangs caused by httpx ASGITransport blocking until
the ASGI app completes.

Error cases use regular client.post() since errors return JSON responses
(not SSE streams).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nexus.a2a.router import build_router
from nexus.a2a.task_manager import TaskManager


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture
def task_manager() -> TaskManager:
    """Create a TaskManager shared between the app and tests."""
    return TaskManager()


@pytest.fixture
def app(task_manager: TaskManager) -> FastAPI:
    """Create a FastAPI app with the A2A router using the shared TaskManager."""
    app = FastAPI()
    router = build_router(base_url="http://testserver", task_manager=task_manager)
    app.include_router(router)
    return app


def _make_rpc(
    method: str, params: dict[str, Any] | None = None, request_id: str | int = "req-1"
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
        "id": request_id,
    }
    if params is not None:
        body["params"] = params
    return body


def _parse_sse_events(content: str) -> list[dict[str, Any]]:
    """Parse SSE event stream into list of data payloads."""
    events = []
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            payload = line[6:]
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


async def _close_streams_after(
    task_manager: TaskManager, delay: float = 0.05
) -> None:
    """Push sentinel to all active SSE queues after a short delay.

    This allows the SSE generator to yield its first event(s) before
    being cleanly shut down, preventing hangs in tests.
    """
    await asyncio.sleep(delay)
    for queues in task_manager._active_streams.values():
        for q in queues:
            q.put_nowait(None)


def _streaming_message_body() -> dict[str, Any]:
    return _make_rpc(
        "a2a.tasks.sendStreamingMessage",
        {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "stream me"}],
            }
        },
    )


# ======================================================================
# sendStreamingMessage
# ======================================================================


class TestSendStreamingMessage:
    @pytest.mark.asyncio
    async def test_returns_sse_content_type(
        self, app: FastAPI, task_manager: TaskManager
    ) -> None:
        body = _streaming_message_body()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            closer = asyncio.create_task(_close_streams_after(task_manager))
            resp = await client.post("/a2a", json=body)
            await closer

            assert resp.status_code == 200
            content_type = resp.headers.get("content-type", "")
            assert "text/event-stream" in content_type

    @pytest.mark.asyncio
    async def test_first_event_is_task(
        self, app: FastAPI, task_manager: TaskManager
    ) -> None:
        body = _streaming_message_body()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            closer = asyncio.create_task(_close_streams_after(task_manager))
            resp = await client.post("/a2a", json=body)
            await closer

            events = _parse_sse_events(resp.text)
            assert len(events) >= 1
            assert "task" in events[0]
            assert "id" in events[0]["task"]

    @pytest.mark.asyncio
    async def test_task_has_submitted_state(
        self, app: FastAPI, task_manager: TaskManager
    ) -> None:
        body = _streaming_message_body()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            closer = asyncio.create_task(_close_streams_after(task_manager))
            resp = await client.post("/a2a", json=body)
            await closer

            events = _parse_sse_events(resp.text)
            assert events[0]["task"]["status"]["state"] == "submitted"

    @pytest.mark.asyncio
    async def test_sse_headers(
        self, app: FastAPI, task_manager: TaskManager
    ) -> None:
        body = _streaming_message_body()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            closer = asyncio.create_task(_close_streams_after(task_manager))
            resp = await client.post("/a2a", json=body)
            await closer

            assert resp.headers.get("cache-control") == "no-cache"
            assert resp.headers.get("x-accel-buffering") == "no"


# ======================================================================
# subscribeToTask
# ======================================================================


class TestSubscribeToTask:
    @pytest.mark.asyncio
    async def test_subscribe_returns_current_state(
        self, app: FastAPI, task_manager: TaskManager
    ) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Create a task first
            create_body = _make_rpc(
                "a2a.tasks.send",
                {
                    "message": {
                        "role": "user",
                        "parts": [{"type": "text", "text": "subscribe test"}],
                    }
                },
            )
            create_resp = await client.post("/a2a", json=create_body)
            task_id = create_resp.json()["result"]["id"]

            # Subscribe (with background stream closer)
            sub_body = _make_rpc(
                "a2a.tasks.subscribeToTask",
                {"taskId": task_id},
            )
            closer = asyncio.create_task(_close_streams_after(task_manager))
            resp = await client.post("/a2a", json=sub_body)
            await closer

            assert resp.status_code == 200
            events = _parse_sse_events(resp.text)
            assert len(events) >= 1
            assert "task" in events[0]
            assert events[0]["task"]["id"] == task_id

    @pytest.mark.asyncio
    async def test_subscribe_nonexistent_task_returns_error(
        self, app: FastAPI
    ) -> None:
        """Subscribing to a nonexistent task should return a JSON-RPC error."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            body = _make_rpc(
                "a2a.tasks.subscribeToTask",
                {"taskId": "nonexistent-task-id"},
            )
            resp = await client.post("/a2a", json=body)
            assert resp.status_code == 200
            data = resp.json()
            assert "error" in data
            assert data["error"]["code"] == -32001  # Task not found


# ======================================================================
# SSE Event Format
# ======================================================================


class TestSSEFormat:
    @pytest.mark.asyncio
    async def test_event_data_prefix(
        self, app: FastAPI, task_manager: TaskManager
    ) -> None:
        """Each SSE event line must start with 'data: '."""
        body = _streaming_message_body()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            closer = asyncio.create_task(_close_streams_after(task_manager))
            resp = await client.post("/a2a", json=body)
            await closer

            data_lines = [
                line
                for line in resp.text.split("\n")
                if line.strip() and not line.strip().startswith(":")
            ]
            for line in data_lines:
                assert line.startswith("data: "), f"Expected 'data: ' prefix, got: {line!r}"

    @pytest.mark.asyncio
    async def test_events_separated_by_double_newline(
        self, app: FastAPI, task_manager: TaskManager
    ) -> None:
        body = _streaming_message_body()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            closer = asyncio.create_task(_close_streams_after(task_manager))
            resp = await client.post("/a2a", json=body)
            await closer

            assert "\n\n" in resp.text

    @pytest.mark.asyncio
    async def test_event_payload_is_valid_json(
        self, app: FastAPI, task_manager: TaskManager
    ) -> None:
        body = _streaming_message_body()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            closer = asyncio.create_task(_close_streams_after(task_manager))
            resp = await client.post("/a2a", json=body)
            await closer

            events = _parse_sse_events(resp.text)
            assert len(events) >= 1
            for event in events:
                assert isinstance(event, dict)


# ======================================================================
# Streaming error handling
# ======================================================================


class TestStreamingErrors:
    @pytest.mark.asyncio
    async def test_send_streaming_invalid_params(self, app: FastAPI) -> None:
        """Invalid params for streaming method should return JSON-RPC error."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            body = _make_rpc(
                "a2a.tasks.sendStreamingMessage",
                {"bad": "params"},
            )
            resp = await client.post("/a2a", json=body)
            assert resp.status_code == 200
            data = resp.json()
            assert "error" in data
            assert data["error"]["code"] == -32602  # Invalid params

    @pytest.mark.asyncio
    async def test_subscribe_invalid_params(self, app: FastAPI) -> None:
        """Invalid params for subscribe should return JSON-RPC error."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            body = _make_rpc(
                "a2a.tasks.subscribeToTask",
                {"bad": "params"},
            )
            resp = await client.post("/a2a", json=body)
            assert resp.status_code == 200
            data = resp.json()
            assert "error" in data
            assert data["error"]["code"] == -32602  # Invalid params
