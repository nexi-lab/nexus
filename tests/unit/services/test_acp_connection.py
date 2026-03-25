"""Tests for AcpConnection — ACP protocol adapter over PipeBackend."""

from __future__ import annotations

import asyncio
import json

import pytest

from nexus.system_services.acp.connection import AcpConnection, AcpRpcError

# ---------------------------------------------------------------------------
# Mock PipeBackend (same as test_agent_loop.py)
# ---------------------------------------------------------------------------


class MockPipeBackend:
    """In-memory PipeBackend for testing."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._written: list[bytes] = []
        self._closed = False

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        from nexus.core.pipe import PipeClosedError

        if self._closed:
            raise PipeClosedError("closed")
        self._written.append(data)
        return len(data)

    async def read(self, *, blocking: bool = True) -> bytes:
        from nexus.core.pipe import PipeClosedError

        if self._closed and self._queue.empty():
            raise PipeClosedError("closed")
        data = await self._queue.get()
        if data == b"__CLOSE__":
            raise PipeClosedError("closed")
        return data

    def write_nowait(self, data: bytes) -> int:
        from nexus.core.pipe import PipeClosedError

        if self._closed:
            raise PipeClosedError("closed")
        self._written.append(data)
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
        return {}

    def inject_json(self, msg: dict) -> None:
        self._queue.put_nowait(json.dumps(msg).encode() + b"\n")

    def signal_close(self) -> None:
        self._queue.put_nowait(b"__CLOSE__")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def acp_conn():
    stdin = MockPipeBackend()
    stdout = MockPipeBackend()
    conn = AcpConnection(
        stdin_pipe=stdin,
        stdout_pipe=stdout,
        cwd="/test",
    )
    return conn, stdin, stdout


class TestAcpConnectionDispatch:
    """Test ACP-specific request/notification dispatch."""

    @pytest.mark.asyncio
    async def test_permission_request_auto_granted(self, acp_conn):
        conn, stdin, stdout = acp_conn
        conn.start()
        try:
            stdout.inject_json(
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "session/request_permission",
                    "params": {"permission": "write"},
                }
            )
            await asyncio.sleep(0.05)

            # Should have responded with allow_once
            assert len(stdin._written) > 0
            resp = json.loads(stdin._written[-1].decode())
            assert resp["id"] == 99
            assert resp["result"]["outcome"]["outcome"] == "selected"
        finally:
            stdout.signal_close()
            await conn.disconnect()

    @pytest.mark.asyncio
    async def test_fs_read_with_callable(self, acp_conn):
        conn, stdin, stdout = acp_conn

        async def mock_read(path: str) -> str:
            return f"content of {path}"

        conn._fs_read = mock_read
        conn.start()
        try:
            stdout.inject_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "fs/read_text_file",
                    "params": {"path": "/test/file.txt"},
                }
            )
            await asyncio.sleep(0.05)

            # Should respond with file content
            assert len(stdin._written) > 0
            resp = json.loads(stdin._written[-1].decode())
            assert resp["id"] == 1
            assert "content of" in resp["result"]["content"]
        finally:
            stdout.signal_close()
            await conn.disconnect()

    @pytest.mark.asyncio
    async def test_fs_write_with_callable(self, acp_conn):
        conn, stdin, stdout = acp_conn
        written_files: dict[str, str] = {}

        async def mock_write(path: str, content: str) -> None:
            written_files[path] = content

        conn._fs_write = mock_write
        conn.start()
        try:
            stdout.inject_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "fs/write_text_file",
                    "params": {"path": "/test/out.txt", "content": "hello"},
                }
            )
            await asyncio.sleep(0.05)

            assert "/test/out.txt" in written_files
            assert written_files["/test/out.txt"] == "hello"
        finally:
            stdout.signal_close()
            await conn.disconnect()

    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self, acp_conn):
        conn, stdin, stdout = acp_conn
        conn.start()
        try:
            stdout.inject_json(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "unknown/method",
                    "params": {},
                }
            )
            await asyncio.sleep(0.05)

            resp = json.loads(stdin._written[-1].decode())
            assert resp["id"] == 3
            assert "error" in resp
            assert resp["error"]["code"] == -32601
        finally:
            stdout.signal_close()
            await conn.disconnect()

    @pytest.mark.asyncio
    async def test_fs_read_without_vfs_returns_error(self, acp_conn):
        """fs/read_text_file without VFS bound should error, not fall back to host open()."""
        conn, stdin, stdout = acp_conn
        # _fs_read is None by default — VFS not bound
        conn.start()
        try:
            stdout.inject_json(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "fs/read_text_file",
                    "params": {"path": "/some/file.txt"},
                }
            )
            await asyncio.sleep(0.05)

            resp = json.loads(stdin._written[-1].decode())
            assert resp["id"] == 10
            assert "error" in resp
            assert resp["error"]["code"] == -32002
            assert "VFS not available" in resp["error"]["message"]
        finally:
            stdout.signal_close()
            await conn.disconnect()

    @pytest.mark.asyncio
    async def test_fs_write_without_vfs_returns_error(self, acp_conn):
        """fs/write_text_file without VFS bound should error, not fall back to host open()."""
        conn, stdin, stdout = acp_conn
        conn.start()
        try:
            stdout.inject_json(
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "fs/write_text_file",
                    "params": {"path": "/some/file.txt", "content": "hello"},
                }
            )
            await asyncio.sleep(0.05)

            resp = json.loads(stdin._written[-1].decode())
            assert resp["id"] == 11
            assert "error" in resp
            assert resp["error"]["code"] == -32002
        finally:
            stdout.signal_close()
            await conn.disconnect()


class TestAcpConnectionNotifications:
    """Test session/update notification accumulation."""

    @pytest.mark.asyncio
    async def test_tool_call_counting(self, acp_conn):
        conn, _, stdout = acp_conn
        conn.start()
        try:
            for _ in range(3):
                stdout.inject_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {"update": {"sessionUpdate": "tool_call"}},
                    }
                )
            await asyncio.sleep(0.05)

            assert conn.num_turns == 3
        finally:
            stdout.signal_close()
            await conn.disconnect()


class TestAcpRpcError:
    """Test AcpRpcError alias."""

    def test_acp_rpc_error_is_agent_rpc_error(self):
        from nexus.system_services.agent_runtime.loop import AgentRpcError

        assert AcpRpcError is AgentRpcError

    def test_acp_rpc_error_attrs(self):
        err = AcpRpcError("test error", code=42, data={"detail": "x"})
        assert str(err) == "test error"
        assert err.code == 42
        assert err.data == {"detail": "x"}
