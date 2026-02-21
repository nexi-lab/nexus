"""Integration tests for A2A gRPC transport binding.

Tests the full gRPC transport path with a real in-process server
and the TaskManager shared between transports.
"""

import asyncio
import time

import grpc
import grpc.aio
import pytest

from nexus.bricks.a2a import a2a_pb2, a2a_pb2_grpc
from nexus.bricks.a2a.grpc_server import create_grpc_server, stop_grpc_server
from nexus.bricks.a2a.models import Message, TaskState, TextPart
from nexus.bricks.a2a.task_manager import TaskManager


@pytest.fixture
async def shared_setup():
    """Create a shared TaskManager, gRPC server, and channel."""
    tm = TaskManager()
    server = await create_grpc_server(tm, port=0)
    port = server.add_insecure_port("[::]:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"localhost:{port}")
    stub = a2a_pb2_grpc.A2AServiceStub(channel)

    yield tm, stub

    await channel.close()
    await stop_grpc_server(server, grace=0.5)


# ------------------------------------------------------------------
# Full flow: create -> get -> update -> verify
# ------------------------------------------------------------------


class TestFullTaskFlow:
    """Integration test for the full task lifecycle via gRPC."""

    @pytest.mark.asyncio
    async def test_create_get_cancel_flow(self, shared_setup) -> None:
        tm, stub = shared_setup

        # 1. Create task via gRPC
        create_resp = await stub.SendMessage(
            a2a_pb2.SendMessageRequest(
                message=a2a_pb2.Message(
                    role="user",
                    parts=[a2a_pb2.Part(text="Integration test task")],
                ),
            )
        )
        task_id = create_resp.task.id
        assert task_id
        assert create_resp.task.status.state == a2a_pb2.TASK_STATE_SUBMITTED

        # 2. Get task via gRPC
        task = await stub.GetTask(a2a_pb2.GetTaskRequest(id=task_id))
        assert task.id == task_id
        assert task.status.state == a2a_pb2.TASK_STATE_SUBMITTED

        # 3. Cancel task via gRPC
        canceled = await stub.CancelTask(a2a_pb2.CancelTaskRequest(id=task_id))
        assert canceled.status.state == a2a_pb2.TASK_STATE_CANCELED

        # 4. Verify via gRPC get
        final = await stub.GetTask(a2a_pb2.GetTaskRequest(id=task_id))
        assert final.status.state == a2a_pb2.TASK_STATE_CANCELED


# ------------------------------------------------------------------
# Cross-transport: create via TaskManager, get via gRPC
# ------------------------------------------------------------------


class TestCrossTransport:
    """Test that tasks created via TaskManager are visible via gRPC."""

    @pytest.mark.asyncio
    async def test_task_manager_task_visible_via_grpc(self, shared_setup) -> None:
        tm, stub = shared_setup

        # Create task via Python API (simulates HTTP path)
        msg = Message(role="user", parts=[TextPart(text="Created via Python")])
        task = await tm.create_task(msg)

        # Get via gRPC
        grpc_task = await stub.GetTask(a2a_pb2.GetTaskRequest(id=task.id))
        assert grpc_task.id == task.id
        assert grpc_task.status.state == a2a_pb2.TASK_STATE_SUBMITTED

    @pytest.mark.asyncio
    async def test_grpc_task_visible_via_task_manager(self, shared_setup) -> None:
        tm, stub = shared_setup

        # Create task via gRPC
        create_resp = await stub.SendMessage(
            a2a_pb2.SendMessageRequest(
                message=a2a_pb2.Message(
                    role="user",
                    parts=[a2a_pb2.Part(text="Created via gRPC")],
                ),
            )
        )
        task_id = create_resp.task.id

        # Get via TaskManager (simulates HTTP path)
        task = await tm.get_task(task_id)
        assert task.id == task_id


# ------------------------------------------------------------------
# Streaming integration
# ------------------------------------------------------------------


class TestStreamingIntegration:
    """Integration tests for streaming RPCs."""

    @pytest.mark.asyncio
    async def test_subscribe_with_state_transitions(self, shared_setup) -> None:
        tm, stub = shared_setup

        # Create a task
        msg = Message(role="user", parts=[TextPart(text="Stream test")])
        task = await tm.create_task(msg)

        # Subscribe via gRPC
        call = stub.SubscribeToTask(a2a_pb2.SubscribeToTaskRequest(id=task.id))

        # Read initial task
        first = await call.read()
        assert first.HasField("task")

        # Trigger state transitions via TaskManager
        await tm.update_task_state(task.id, TaskState.WORKING)
        await tm.update_task_state(task.id, TaskState.COMPLETED)

        # Read updates
        working = await call.read()
        assert working.HasField("status_update")
        assert working.status_update.status.state == a2a_pb2.TASK_STATE_WORKING

        completed = await call.read()
        assert completed.HasField("status_update")
        assert completed.status_update.final is True


# ------------------------------------------------------------------
# Throughput benchmark
# ------------------------------------------------------------------


class TestThroughput:
    """Simple throughput benchmark for SendMessage."""

    @pytest.mark.asyncio
    async def test_1k_send_message_calls(self, shared_setup) -> None:
        _, stub = shared_setup
        n = 1000

        request = a2a_pb2.SendMessageRequest(
            message=a2a_pb2.Message(
                role="user",
                parts=[a2a_pb2.Part(text="Benchmark message")],
            ),
        )

        start = time.monotonic()
        tasks = [stub.SendMessage(request) for _ in range(n)]
        await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start

        rate = n / elapsed
        # Just log the result; don't fail on slow CI
        print(f"\n  gRPC SendMessage: {n} calls in {elapsed:.2f}s ({rate:.0f} msg/s)")
        assert elapsed < 30.0, f"1K SendMessage took too long: {elapsed:.2f}s"
