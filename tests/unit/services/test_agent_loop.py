"""Tests for AgentLoop — JSON-RPC 2.0 over PipeBackend base class."""

from __future__ import annotations

import asyncio
import json

import pytest

from nexus.core.pipe import PipeClosedError
from nexus.system_services.agent_runtime.loop import AgentLoop, AgentRpcError

# ---------------------------------------------------------------------------
# Mock PipeBackend
# ---------------------------------------------------------------------------


class MockPipeBackend:
    """In-memory PipeBackend for testing."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._written: list[bytes] = []
        self._closed = False
        self._write_count = 0
        self._read_count = 0

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        if self._closed:
            raise PipeClosedError("closed")
        self._written.append(data)
        self._write_count += 1
        return len(data)

    async def read(self, *, blocking: bool = True) -> bytes:
        if self._closed and self._queue.empty():
            raise PipeClosedError("closed")
        data = await self._queue.get()
        if data == b"__CLOSE__":
            raise PipeClosedError("closed")
        self._read_count += 1
        return data

    def write_nowait(self, data: bytes) -> int:
        if self._closed:
            raise PipeClosedError("closed")
        self._written.append(data)
        self._write_count += 1
        return len(data)

    def read_nowait(self) -> bytes:
        raise NotImplementedError

    async def wait_writable(self) -> None:
        pass

    async def wait_readable(self) -> None:
        pass

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> dict:
        return {"backend": "mock", "write_count": self._write_count}

    def inject(self, data: bytes) -> None:
        """Inject data into the read queue (simulate agent writing to stdout)."""
        self._queue.put_nowait(data)

    def inject_json(self, msg: dict) -> None:
        """Inject a JSON-RPC message."""
        self.inject(json.dumps(msg).encode() + b"\n")

    def signal_close(self) -> None:
        """Signal EOF to reader."""
        self._queue.put_nowait(b"__CLOSE__")


# ---------------------------------------------------------------------------
# Concrete subclass for testing
# ---------------------------------------------------------------------------


class ConcreteAgentLoop(AgentLoop):
    """Concrete AgentLoop for testing abstract methods."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.received_requests: list[dict] = []
        self.received_notifications: list[dict] = []

    def _handle_request(self, msg: dict) -> None:
        self.received_requests.append(msg)
        # Auto-respond with success
        self._respond(msg["id"], {"ok": True})

    def _handle_notification(self, msg: dict) -> None:
        self.received_notifications.append(msg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def pipes():
    stdin_pipe = MockPipeBackend()
    stdout_pipe = MockPipeBackend()
    return stdin_pipe, stdout_pipe


@pytest.fixture
def loop(pipes):
    stdin_pipe, stdout_pipe = pipes
    return ConcreteAgentLoop(
        stdin_pipe=stdin_pipe,
        stdout_pipe=stdout_pipe,
        cwd="/test",
    )


class TestAgentLoopTransport:
    """Test JSON-RPC transport methods."""

    def test_write_nowait(self, loop, pipes):
        stdin_pipe, _ = pipes
        loop._write({"jsonrpc": "2.0", "method": "test"})
        assert len(stdin_pipe._written) == 1
        written = json.loads(stdin_pipe._written[0].decode())
        assert written["method"] == "test"

    @pytest.mark.asyncio
    async def test_request_response(self, loop, pipes):
        stdin_pipe, stdout_pipe = pipes
        loop.start()
        try:
            # Simulate agent responding to our request
            async def respond_later():
                await asyncio.sleep(0.01)
                # Read what was written to stdin to get the msg_id
                assert len(stdin_pipe._written) > 0
                sent = json.loads(stdin_pipe._written[-1].decode())
                stdout_pipe.inject_json(
                    {
                        "jsonrpc": "2.0",
                        "id": sent["id"],
                        "result": {"hello": "world"},
                    }
                )

            asyncio.create_task(respond_later())
            result = await loop._request("test_method", {"key": "val"}, timeout=2.0)
            assert result == {"hello": "world"}
        finally:
            stdout_pipe.signal_close()
            await loop.disconnect()

    @pytest.mark.asyncio
    async def test_request_error(self, loop, pipes):
        stdin_pipe, stdout_pipe = pipes
        loop.start()
        try:

            async def error_later():
                await asyncio.sleep(0.01)
                sent = json.loads(stdin_pipe._written[-1].decode())
                stdout_pipe.inject_json(
                    {
                        "jsonrpc": "2.0",
                        "id": sent["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )

            asyncio.create_task(error_later())
            with pytest.raises(AgentRpcError, match="Method not found"):
                await loop._request("bad_method", {}, timeout=2.0)
        finally:
            stdout_pipe.signal_close()
            await loop.disconnect()

    @pytest.mark.asyncio
    async def test_request_timeout(self, loop, pipes):
        _, stdout_pipe = pipes
        loop.start()
        try:
            with pytest.raises(TimeoutError):
                await loop._request("slow_method", {}, timeout=0.05)
        finally:
            stdout_pipe.signal_close()
            await loop.disconnect()


class TestAgentLoopDispatch:
    """Test message dispatch to subclass handlers."""

    @pytest.mark.asyncio
    async def test_dispatch_request(self, loop, pipes):
        _, stdout_pipe = pipes
        loop.start()
        try:
            # Inject a request from the agent
            stdout_pipe.inject_json(
                {
                    "jsonrpc": "2.0",
                    "id": 42,
                    "method": "fs/read_text_file",
                    "params": {"path": "/test.txt"},
                }
            )
            await asyncio.sleep(0.05)

            assert len(loop.received_requests) == 1
            assert loop.received_requests[0]["method"] == "fs/read_text_file"
        finally:
            stdout_pipe.signal_close()
            await loop.disconnect()

    @pytest.mark.asyncio
    async def test_dispatch_notification(self, loop, pipes):
        _, stdout_pipe = pipes
        loop.start()
        try:
            stdout_pipe.inject_json(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"update": {"type": "usage"}},
                }
            )
            await asyncio.sleep(0.05)

            assert len(loop.received_notifications) == 1
            assert loop.received_notifications[0]["method"] == "session/update"
        finally:
            stdout_pipe.signal_close()
            await loop.disconnect()


class TestAgentLoopLifecycle:
    """Test lifecycle methods."""

    @pytest.mark.asyncio
    async def test_disconnect_closes_pipes(self, loop, pipes):
        stdin_pipe, stdout_pipe = pipes
        await loop.disconnect()
        assert stdin_pipe.closed
        assert stdout_pipe.closed

    @pytest.mark.asyncio
    async def test_disconnect_fails_pending(self, loop, pipes):
        _, stdout_pipe = pipes
        loop.start()

        # Create a pending request
        asyncio.get_running_loop().create_future()
        loop._pending[1] = asyncio.get_running_loop().create_future()

        stdout_pipe.signal_close()
        await loop.disconnect()

        assert loop._pending == {}

    @pytest.mark.asyncio
    async def test_stderr_collection(self):
        stdin_pipe = MockPipeBackend()
        stdout_pipe = MockPipeBackend()
        stderr_pipe = MockPipeBackend()

        # Inject stderr lines into the pipe
        stderr_pipe.inject(b"error line 1\n")
        stderr_pipe.inject(b"error line 2\n")

        agent = ConcreteAgentLoop(
            stdin_pipe=stdin_pipe,
            stdout_pipe=stdout_pipe,
            stderr_pipe=stderr_pipe,
        )
        agent.start()

        # Wait for stderr collector to read lines
        await asyncio.sleep(0.05)

        assert "error line 1" in agent.stderr_output
        assert "error line 2" in agent.stderr_output

        stderr_pipe.signal_close()
        stdout_pipe.signal_close()
        await agent.disconnect()

    @pytest.mark.asyncio
    async def test_reader_loop_handles_pipe_closed(self, loop, pipes):
        _, stdout_pipe = pipes
        loop.start()
        stdout_pipe.signal_close()
        # Give reader loop time to exit cleanly
        await asyncio.sleep(0.05)
        # Should not raise — PipeClosedError is caught
        await loop.disconnect()

    @pytest.mark.asyncio
    async def test_non_json_lines_skipped(self, loop, pipes):
        _, stdout_pipe = pipes
        loop.start()
        try:
            stdout_pipe.inject(b"this is not json\n")
            stdout_pipe.inject_json(
                {
                    "jsonrpc": "2.0",
                    "method": "valid_notification",
                    "params": {},
                }
            )
            await asyncio.sleep(0.05)

            # Only the valid JSON-RPC message should be dispatched
            assert len(loop.received_notifications) == 1
        finally:
            stdout_pipe.signal_close()
            await loop.disconnect()
