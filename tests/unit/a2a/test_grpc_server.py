"""Unit tests for A2A gRPC server.

Tests use an in-process gRPC server on an ephemeral port (port 0).
All business logic goes through the real TaskManager with an
InMemoryTaskStore, exercising the full gRPC transport path.
"""

from __future__ import annotations

import grpc
import grpc.aio
import pytest

from nexus.a2a import a2a_pb2, a2a_pb2_grpc
from nexus.a2a.grpc_server import create_grpc_server, stop_grpc_server
from nexus.a2a.models import TaskState
from nexus.a2a.task_manager import TaskManager


@pytest.fixture
async def grpc_channel():
    """Create an in-process gRPC server and return a connected channel."""
    tm = TaskManager()
    server = await create_grpc_server(tm, port=0)
    # Get the bound port
    port = server.add_insecure_port("[::]:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"localhost:{port}")
    yield channel, tm

    await channel.close()
    await stop_grpc_server(server, grace=0.5)


@pytest.fixture
def stub(grpc_channel):
    """Return the A2A gRPC stub."""
    channel, _ = grpc_channel
    return a2a_pb2_grpc.A2AServiceStub(channel)


@pytest.fixture
def task_manager(grpc_channel):
    """Return the TaskManager instance."""
    _, tm = grpc_channel
    return tm


# ------------------------------------------------------------------
# SendMessage
# ------------------------------------------------------------------


class TestSendMessage:
    """Tests for the SendMessage RPC."""

    @pytest.mark.asyncio
    async def test_creates_task(self, stub) -> None:
        request = a2a_pb2.SendMessageRequest(
            message=a2a_pb2.Message(
                role="user",
                parts=[a2a_pb2.Part(text="Hello, agent!")],
            ),
        )
        response = await stub.SendMessage(request)

        assert response.task.id
        assert response.task.status.state == a2a_pb2.TASK_STATE_SUBMITTED
        assert len(response.task.history) == 1

    @pytest.mark.asyncio
    async def test_returns_task_with_metadata(self, stub) -> None:
        from google.protobuf import struct_pb2

        meta = struct_pb2.Struct()
        meta.update({"session": "test-session"})

        request = a2a_pb2.SendMessageRequest(
            message=a2a_pb2.Message(
                role="user",
                parts=[a2a_pb2.Part(text="Hello")],
            ),
            metadata=meta,
        )
        response = await stub.SendMessage(request)

        assert response.task.id


# ------------------------------------------------------------------
# GetTask
# ------------------------------------------------------------------


class TestGetTask:
    """Tests for the GetTask RPC."""

    @pytest.mark.asyncio
    async def test_get_existing_task(self, stub) -> None:
        # Create a task first
        create_resp = await stub.SendMessage(
            a2a_pb2.SendMessageRequest(
                message=a2a_pb2.Message(
                    role="user",
                    parts=[a2a_pb2.Part(text="Create me")],
                ),
            )
        )
        task_id = create_resp.task.id

        # Get the task
        task = await stub.GetTask(a2a_pb2.GetTaskRequest(id=task_id))

        assert task.id == task_id
        assert task.status.state == a2a_pb2.TASK_STATE_SUBMITTED

    @pytest.mark.asyncio
    async def test_get_nonexistent_task(self, stub) -> None:
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.GetTask(a2a_pb2.GetTaskRequest(id="nonexistent"))

        assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


# ------------------------------------------------------------------
# CancelTask
# ------------------------------------------------------------------


class TestCancelTask:
    """Tests for the CancelTask RPC."""

    @pytest.mark.asyncio
    async def test_cancel_submitted_task(self, stub) -> None:
        # Create a task
        create_resp = await stub.SendMessage(
            a2a_pb2.SendMessageRequest(
                message=a2a_pb2.Message(
                    role="user",
                    parts=[a2a_pb2.Part(text="Cancel me")],
                ),
            )
        )
        task_id = create_resp.task.id

        # Cancel it
        task = await stub.CancelTask(a2a_pb2.CancelTaskRequest(id=task_id))

        assert task.id == task_id
        assert task.status.state == a2a_pb2.TASK_STATE_CANCELED

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task(self, stub) -> None:
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.CancelTask(a2a_pb2.CancelTaskRequest(id="nonexistent"))

        assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND

    @pytest.mark.asyncio
    async def test_cancel_already_canceled_task(self, stub) -> None:
        # Create and cancel a task
        create_resp = await stub.SendMessage(
            a2a_pb2.SendMessageRequest(
                message=a2a_pb2.Message(
                    role="user",
                    parts=[a2a_pb2.Part(text="Cancel me twice")],
                ),
            )
        )
        task_id = create_resp.task.id
        await stub.CancelTask(a2a_pb2.CancelTaskRequest(id=task_id))

        # Try to cancel again
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.CancelTask(a2a_pb2.CancelTaskRequest(id=task_id))

        assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION


# ------------------------------------------------------------------
# SubscribeToTask
# ------------------------------------------------------------------


class TestSubscribeToTask:
    """Tests for the SubscribeToTask server-streaming RPC."""

    @pytest.mark.asyncio
    async def test_subscribe_receives_initial_task(self, stub, task_manager) -> None:
        # Create a task
        create_resp = await stub.SendMessage(
            a2a_pb2.SendMessageRequest(
                message=a2a_pb2.Message(
                    role="user",
                    parts=[a2a_pb2.Part(text="Subscribe to me")],
                ),
            )
        )
        task_id = create_resp.task.id

        # Subscribe and collect messages
        responses = []
        call = stub.SubscribeToTask(a2a_pb2.SubscribeToTaskRequest(id=task_id))

        # First response should be the initial task
        first = await call.read()
        responses.append(first)

        assert first.HasField("task")
        assert first.task.id == task_id

        # Now update task state to trigger a stream event
        await task_manager.update_task_state(task_id, TaskState.WORKING)
        await task_manager.update_task_state(task_id, TaskState.COMPLETED)

        # Read the status updates
        second = await call.read()
        responses.append(second)
        assert second.HasField("status_update")
        assert second.status_update.task_id == task_id

        third = await call.read()
        responses.append(third)
        assert third.HasField("status_update")
        assert third.status_update.final is True

    @pytest.mark.asyncio
    async def test_subscribe_to_terminal_task(self, stub) -> None:
        # Create and cancel a task (terminal state)
        create_resp = await stub.SendMessage(
            a2a_pb2.SendMessageRequest(
                message=a2a_pb2.Message(
                    role="user",
                    parts=[a2a_pb2.Part(text="Already done")],
                ),
            )
        )
        task_id = create_resp.task.id
        await stub.CancelTask(a2a_pb2.CancelTaskRequest(id=task_id))

        # Subscribe - should get just the initial task and then EOF
        call = stub.SubscribeToTask(a2a_pb2.SubscribeToTaskRequest(id=task_id))
        first = await call.read()
        assert first.HasField("task")
        assert first.task.status.state == a2a_pb2.TASK_STATE_CANCELED

    @pytest.mark.asyncio
    async def test_subscribe_to_nonexistent_task(self, stub) -> None:
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            call = stub.SubscribeToTask(a2a_pb2.SubscribeToTaskRequest(id="nonexistent"))
            await call.read()

        assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


# ------------------------------------------------------------------
# SendStreamingMessage
# ------------------------------------------------------------------


class TestSendStreamingMessage:
    """Tests for the SendStreamingMessage server-streaming RPC."""

    @pytest.mark.asyncio
    async def test_streaming_returns_initial_task(self, stub) -> None:
        request = a2a_pb2.SendMessageRequest(
            message=a2a_pb2.Message(
                role="user",
                parts=[a2a_pb2.Part(text="Stream me")],
            ),
        )
        call = stub.SendStreamingMessage(request)
        first = await call.read()

        assert first.HasField("task")
        assert first.task.id
        assert first.task.status.state == a2a_pb2.TASK_STATE_SUBMITTED
