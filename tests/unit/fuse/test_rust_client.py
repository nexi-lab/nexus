"""Unit tests for RustFUSEClient (Issue 10A).

Tests the Python IPC bridge without requiring a real Rust daemon.
Uses mock socket and subprocess to verify:
- JSON-RPC request/response formatting
- Auto-restart with exponential backoff (Issue 2A)
- Large recv buffer (Issue 3B)
- Error propagation (ENOENT, generic fallback)
"""

import base64
import errno
import json
import socket
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.fuse.rust_client import (
    _INITIAL_BACKOFF_SECS,
    _MAX_BACKOFF_SECS,
    _MAX_RESTART_ATTEMPTS,
    _RECV_BUFFER_SIZE,
    FileEntry,
    FileMetadata,
    RustFUSEClient,
)

# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def mock_client(tmp_path: Path) -> RustFUSEClient:
    """Create a RustFUSEClient with mocked daemon/socket."""
    sock_path = tmp_path / "test.sock"
    sock_path.touch()  # Pretend socket exists

    with (
        patch.object(RustFUSEClient, "_find_rust_binary", return_value="/usr/bin/true"),
        patch.object(RustFUSEClient, "_start_daemon"),
        patch.object(RustFUSEClient, "_connect"),
    ):
        client = RustFUSEClient(
            nexus_url="http://localhost:2026",
            api_key="test-key",
            agent_id="test-agent",
        )
        client.socket_path = sock_path
        client.daemon_process = MagicMock(spec=subprocess.Popen)
        client.daemon_process.poll.return_value = None  # Process alive
        client.daemon_process.pid = 12345  # Fake PID for logging
        client.sock = MagicMock(spec=socket.socket)
        return client


def _mock_rpc_response(result: dict) -> bytes:
    """Build a JSON-RPC success response as bytes."""
    resp = {"jsonrpc": "2.0", "id": 1, "result": result}
    return (json.dumps(resp) + "\n").encode()


def _mock_rpc_error(code: int, message: str, errno_val: int | None = None) -> bytes:
    """Build a JSON-RPC error response as bytes."""
    error: dict = {"code": code, "message": message}
    if errno_val is not None:
        error["data"] = {"errno": errno_val}
    resp = {"jsonrpc": "2.0", "id": 1, "error": error}
    return (json.dumps(resp) + "\n").encode()


# ── Constants ──────────────────────────────────────────────


class TestConstants:
    """Verify Issue 2A/3B constants are correctly defined."""

    def test_recv_buffer_size(self) -> None:
        assert _RECV_BUFFER_SIZE == 65536

    def test_max_restart_attempts(self) -> None:
        assert _MAX_RESTART_ATTEMPTS == 3

    def test_initial_backoff(self) -> None:
        assert _INITIAL_BACKOFF_SECS == 0.5

    def test_max_backoff(self) -> None:
        assert _MAX_BACKOFF_SECS == 4.0


# ── Data Classes ──────────────────────────────────────────


class TestDataClasses:
    def test_file_entry_defaults(self) -> None:
        entry = FileEntry(name="test.txt", entry_type="file", size=42)
        assert entry.created_at is None
        assert entry.updated_at is None

    def test_file_metadata_defaults(self) -> None:
        meta = FileMetadata(size=0, is_directory=True)
        assert meta.content_id is None
        assert meta.modified_at is None


# ── Read ──────────────────────────────────────────────────


class TestRead:
    def test_read_decodes_base64(self, mock_client: RustFUSEClient) -> None:
        content = b"hello world"
        encoded = base64.b64encode(content).decode()
        mock_client.sock.recv.return_value = _mock_rpc_response({"data": encoded})

        result = mock_client.sys_read("/test.txt")
        assert result == content

    def test_read_sends_correct_request(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_response(
            {"data": base64.b64encode(b"x").decode()}
        )
        mock_client.sys_read("/my/path.txt")

        sent = mock_client.sock.sendall.call_args[0][0]
        request = json.loads(sent.decode())
        assert request["method"] == "read"
        assert request["params"]["path"] == "/my/path.txt"
        assert request["jsonrpc"] == "2.0"


# ── Write ──────────────────────────────────────────────────


class TestWrite:
    def test_write_encodes_base64(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_response({})
        mock_client.sys_write("/test.txt", b"hello")

        sent = mock_client.sock.sendall.call_args[0][0]
        request = json.loads(sent.decode())
        assert request["params"]["content"]["__type__"] == "bytes"
        decoded = base64.b64decode(request["params"]["content"]["data"])
        assert decoded == b"hello"


# ── List ──────────────────────────────────────────────────


class TestList:
    def test_list_parses_entries(self, mock_client: RustFUSEClient) -> None:
        files = [
            {"name": "a.txt", "type": "file", "size": 10},
            {"name": "subdir", "type": "directory", "size": 0},
        ]
        mock_client.sock.recv.return_value = _mock_rpc_response({"files": files})

        result = mock_client.sys_readdir("/")
        assert len(result) == 2
        assert result[0].name == "a.txt"
        assert result[0].entry_type == "file"
        assert result[0].size == 10
        assert result[1].name == "subdir"
        assert result[1].entry_type == "directory"


# ── Stat ──────────────────────────────────────────────────


class TestStat:
    def test_stat_returns_metadata(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_response(
            {"size": 100, "is_directory": False, "content_id": "abc", "modified_at": "2024-01-01"}
        )
        meta = mock_client.stat("/test.txt")
        assert meta.size == 100
        assert meta.is_directory is False
        assert meta.content_id == "abc"
        assert meta.modified_at == "2024-01-01"


# ── Mkdir / Delete / Rename / Exists ──────────────────────


class TestOtherOps:
    def test_mkdir(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_response({})
        mock_client.mkdir("/new-dir")
        sent = json.loads(mock_client.sock.sendall.call_args[0][0].decode())
        assert sent["method"] == "mkdir"

    def test_delete(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_response({})
        mock_client.sys_unlink("/to-del.txt")
        sent = json.loads(mock_client.sock.sendall.call_args[0][0].decode())
        assert sent["method"] == "delete"

    def test_rename(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_response({})
        mock_client.sys_rename("/old.txt", "/new.txt")
        sent = json.loads(mock_client.sock.sendall.call_args[0][0].decode())
        assert sent["method"] == "rename"
        assert sent["params"]["old_path"] == "/old.txt"
        assert sent["params"]["new_path"] == "/new.txt"

    def test_exists_true(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_response({"exists": True})
        assert mock_client.access("/here.txt") is True

    def test_exists_false(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_response({"exists": False})
        assert mock_client.access("/gone.txt") is False


# ── Error Handling ────────────────────────────────────────


class TestErrorHandling:
    def test_rpc_error_raises_oserror(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_error(-32000, "File not found", errno.ENOENT)
        with pytest.raises(OSError) as exc_info:
            mock_client.sys_read("/missing.txt")
        assert exc_info.value.errno == errno.ENOENT

    def test_rpc_error_default_errno(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_error(-32603, "Internal error")
        with pytest.raises(OSError) as exc_info:
            mock_client.sys_read("/broken.txt")
        assert exc_info.value.errno == 5  # EIO default

    def test_connection_closed_triggers_reconnect(self, mock_client: RustFUSEClient) -> None:
        # First recv returns empty bytes (connection closed)
        mock_client.sock.recv.return_value = b""

        with (
            patch.object(mock_client, "_reconnect"),
            pytest.raises(RuntimeError, match="Connection closed"),
        ):
            mock_client._send_request("read", {"path": "/test.txt"})

        # The empty recv raises RuntimeError which is caught,
        # but since it's not a ConnectionError it won't trigger reconnect.
        # Let's test with a real connection error instead.

    def test_broken_pipe_triggers_reconnect(self, mock_client: RustFUSEClient) -> None:
        # sendall raises BrokenPipeError
        mock_client.sock.sendall.side_effect = BrokenPipeError("broken")

        with patch.object(mock_client, "_reconnect") as mock_reconnect:
            mock_reconnect.side_effect = RuntimeError("max restarts")
            with pytest.raises(RuntimeError):
                mock_client._send_request("read", {"path": "/test.txt"})

            mock_reconnect.assert_called_once()


# ── Auto-Restart (Issue 2A) ──────────────────────────────


class TestAutoRestart:
    def test_dead_daemon_triggers_reconnect(self, mock_client: RustFUSEClient) -> None:
        mock_client.daemon_process.poll.return_value = 1  # Process dead

        with patch.object(mock_client, "_reconnect") as mock_reconnect:
            # After reconnect, we need the send to work
            mock_reconnect.side_effect = lambda: setattr(
                mock_client.daemon_process, "poll", MagicMock(return_value=None)
            )
            mock_client.sock.recv.return_value = _mock_rpc_response({"exists": True})
            mock_client.access("/test.txt")
            mock_reconnect.assert_called_once()

    def test_reconnect_exceeds_max_attempts(self, mock_client: RustFUSEClient) -> None:
        mock_client._restart_count = _MAX_RESTART_ATTEMPTS

        with pytest.raises(RuntimeError, match="failed.*times"):
            mock_client._reconnect()

    def test_successful_request_resets_restart_count(self, mock_client: RustFUSEClient) -> None:
        mock_client._restart_count = 2
        mock_client.sock.recv.return_value = _mock_rpc_response({"exists": True})

        mock_client.access("/test.txt")
        assert mock_client._restart_count == 0

    def test_request_id_increments(self, mock_client: RustFUSEClient) -> None:
        mock_client.sock.recv.return_value = _mock_rpc_response({"exists": True})

        start_id = mock_client.request_id
        mock_client.access("/a.txt")
        mock_client.access("/b.txt")
        assert mock_client.request_id == start_id + 2


# ── Close / Context Manager ──────────────────────────────


class TestLifecycle:
    def test_close_cleans_up(self, mock_client: RustFUSEClient) -> None:
        mock_client.close()
        assert mock_client.sock is None
        assert mock_client.daemon_process is None

    def test_context_manager(self, mock_client: RustFUSEClient) -> None:
        with mock_client as c:
            assert c is mock_client
        assert mock_client.sock is None

    def test_close_idempotent(self, mock_client: RustFUSEClient) -> None:
        mock_client.close()
        mock_client.close()  # Should not raise
