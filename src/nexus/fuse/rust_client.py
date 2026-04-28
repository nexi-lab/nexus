"""Python client for Rust FUSE daemon via Unix socket IPC.

This module provides a bridge between Python FUSE operations and the high-performance
Rust daemon, enabling 10-100x speedup on hot path operations.
"""

import base64
import contextlib
import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Issue 3B: 64KB recv buffer to handle large file reads without O(n) loop
# iterations. 4KB was causing hundreds of loop iterations for multi-MB files.
_RECV_BUFFER_SIZE = 65536

# Issue 2A: Daemon auto-restart configuration
_MAX_RESTART_ATTEMPTS = 3
_INITIAL_BACKOFF_SECS = 0.5
_MAX_BACKOFF_SECS = 4.0


@dataclass
class FileEntry:
    """File/directory entry from listing."""

    name: str
    entry_type: str
    size: int
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class FileMetadata:
    """File metadata from stat."""

    size: int
    is_directory: bool
    content_id: str | None = None
    modified_at: str | None = None


class RustFUSEClient:
    """Client for communicating with Rust FUSE daemon via Unix socket.

    The Rust daemon provides high-performance file operations while Python
    handles permissions, namespaces, and orchestration.

    Issue 2A: Includes automatic daemon restart with exponential backoff
    if the daemon process dies unexpectedly.
    """

    def __init__(
        self,
        nexus_url: str,
        api_key: str,
        agent_id: str | None = None,
        rust_binary: str | None = None,
    ):
        """Initialize client and spawn Rust daemon.

        Args:
            nexus_url: Nexus server URL
            api_key: API key for authentication
            agent_id: Optional agent ID for attribution
            rust_binary: Path to nexus-fuse binary (default: auto-discover)
        """
        self.nexus_url = nexus_url
        self.api_key = api_key
        self.agent_id = agent_id
        self.rust_binary = rust_binary or self._find_rust_binary()
        self.daemon_process: subprocess.Popen | None = None
        self.socket_path: Path | None = None
        self.sock: socket.socket | None = None
        self.request_id = 0
        self._restart_count = 0
        self._lock = threading.Lock()

        self._start_daemon()
        self._connect()

    def _find_rust_binary(self) -> str:
        """Find nexus-fuse binary in PATH or build directories.

        Search order:
        1. nexus-fuse in PATH (production install)
        2. ../nexus-fuse/target/release/nexus-fuse (development)
        3. ../nexus-fuse/target/debug/nexus-fuse (development debug)

        Returns:
            Path to nexus-fuse binary

        Raises:
            RuntimeError: If binary not found
        """
        import shutil

        # Try PATH first (production)
        binary = shutil.which("nexus-fuse")
        if binary:
            return binary

        # Try development build directories relative to this file
        current_dir = Path(__file__).resolve().parent
        repo_root = current_dir.parent.parent.parent  # src/nexus/fuse -> repo root

        for variant in ["release", "debug"]:
            candidate = repo_root / "nexus-fuse" / "target" / variant / "nexus-fuse"
            if candidate.exists():
                logger.info("Found Rust binary in development build", path=str(candidate))
                return str(candidate)

        raise RuntimeError(
            "nexus-fuse binary not found. Install with 'cd nexus-fuse && cargo install --path .' "
            "or build with 'cd nexus-fuse && cargo build --release'"
        )

    def _start_daemon(self) -> None:
        """Spawn Rust daemon process.

        Security: API key is passed via NEXUS_API_KEY environment variable
        instead of command-line arguments, to avoid exposure in process listings
        (ps, /proc/pid/cmdline). The Rust daemon reads it via clap's env support.
        """
        cmd = [
            self.rust_binary,
            "daemon",
            "--url",
            self.nexus_url,
        ]

        if self.agent_id:
            cmd.extend(["--agent-id", self.agent_id])

        # Pass API key via environment to avoid exposure in argv
        daemon_env = {**os.environ, "NEXUS_API_KEY": self.api_key}

        logger.info("Starting Rust FUSE daemon", cmd=cmd)

        # Spawn daemon and capture stdout to get socket path
        self.daemon_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=daemon_env,
        )

        # Read socket path from stdout
        if not self.daemon_process.stdout:
            raise RuntimeError("Failed to capture daemon stdout")
        socket_line = self.daemon_process.stdout.readline().strip()
        self.socket_path = Path(socket_line)

        logger.info(
            "Rust daemon started", socket_path=self.socket_path, pid=self.daemon_process.pid
        )

        # Wait a bit for socket to be ready
        for _ in range(50):  # 5 seconds max
            if self.socket_path.exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Socket not created: {self.socket_path}")

    def _connect(self) -> None:
        """Connect to Rust daemon via Unix socket."""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self.socket_path))
        logger.info("Connected to Rust daemon", socket_path=self.socket_path)

    def _is_daemon_alive(self) -> bool:
        """Check if the daemon process is still running."""
        if self.daemon_process is None:
            return False
        return self.daemon_process.poll() is None

    def _reconnect(self) -> None:
        """Issue 2A: Restart daemon with exponential backoff.

        Called when the daemon crashes or the socket connection breaks.
        Uses exponential backoff to avoid hammering a failing daemon.

        Raises:
            RuntimeError: If max restart attempts exceeded
        """
        if self._restart_count >= _MAX_RESTART_ATTEMPTS:
            raise RuntimeError(
                f"Rust daemon failed {_MAX_RESTART_ATTEMPTS} times, giving up. "
                "Falling back to Python FUSE operations."
            )

        backoff = min(
            _INITIAL_BACKOFF_SECS * (2**self._restart_count),
            _MAX_BACKOFF_SECS,
        )
        self._restart_count += 1

        logger.warning(
            "Rust daemon died, restarting",
            attempt=self._restart_count,
            max_attempts=_MAX_RESTART_ATTEMPTS,
            backoff_secs=backoff,
        )

        # Cleanup old connection
        if self.sock:
            with contextlib.suppress(OSError):
                self.sock.close()
            self.sock = None

        if self.daemon_process:
            try:
                self.daemon_process.kill()
                self.daemon_process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.daemon_process = None

        if self.socket_path and self.socket_path.exists():
            with contextlib.suppress(OSError):
                self.socket_path.unlink()

        time.sleep(backoff)

        self._start_daemon()
        self._connect()
        logger.info("Rust daemon restarted successfully", attempt=self._restart_count)

    def _send_request(self, method: str, params: dict) -> dict:
        """Send JSON-RPC request to Rust daemon.

        Thread-safe: serializes all requests through a lock so concurrent
        FUSE threads cannot interleave sendall()/recv() on the shared socket.

        Issue 2A: Automatically reconnects if daemon has died.
        Issue 3B: Uses 64KB recv buffer for better large-file performance.

        Args:
            method: RPC method name
            params: Method parameters

        Returns:
            Result from Rust daemon

        Raises:
            RuntimeError: If request fails after reconnect attempts
        """
        with self._lock:
            # Issue 2A: Check daemon health before sending
            if not self._is_daemon_alive():
                self._reconnect()

            self.request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": method,
                "params": params,
            }

            request_json = json.dumps(request) + "\n"
            if not self.sock:
                raise RuntimeError("Not connected to daemon")

            try:
                self.sock.sendall(request_json.encode())

                # Issue 3B: Receive response with larger buffer (64KB vs old 4KB)
                response_data = b""
                while True:
                    chunk = self.sock.recv(_RECV_BUFFER_SIZE)
                    if not chunk:
                        raise RuntimeError("Connection closed by daemon")
                    response_data += chunk
                    if b"\n" in response_data:
                        break
            except (ConnectionError, BrokenPipeError, OSError) as e:
                # Issue 2A: Connection lost — try reconnecting and retrying once
                logger.warning(f"Daemon connection lost during {method}: {e}")
                self._reconnect()

                # Retry the request on the new connection
                if not self.sock:
                    raise RuntimeError("Reconnection failed") from e
                self.sock.sendall(request_json.encode())

                response_data = b""
                while True:
                    chunk = self.sock.recv(_RECV_BUFFER_SIZE)
                    if not chunk:
                        raise RuntimeError("Connection closed after reconnect") from None
                    response_data += chunk
                    if b"\n" in response_data:
                        break

            response = json.loads(response_data.decode())

            # Reset restart counter on successful request
            self._restart_count = 0

            # Check for errors
            if "error" in response:
                error = response["error"]
                error_errno = error.get("data", {}).get("errno", 5)  # Default to EIO
                raise OSError(error_errno, error["message"])

            result: dict[Any, Any] = response.get("result", {})
            return result

    def sys_read(self, path: str) -> bytes:
        """Read file contents.

        Args:
            path: File path

        Returns:
            File contents as bytes
        """
        result = self._send_request("read", {"path": path})
        return base64.b64decode(result["data"])

    def sys_write(self, path: str, content: bytes) -> None:
        """Write file contents.

        Args:
            path: File path
            content: File contents as bytes
        """
        encoded = base64.b64encode(content).decode()
        self._send_request(
            "write",
            {"path": path, "content": {"__type__": "bytes", "data": encoded}},
        )

    def sys_readdir(self, path: str) -> list[FileEntry]:
        """List directory contents.

        Args:
            path: Directory path

        Returns:
            List of file entries
        """
        result = self._send_request("list", {"path": path})
        return [
            FileEntry(
                name=f["name"],
                entry_type=f["type"],
                size=f.get("size", 0),
                created_at=f.get("created_at"),
                updated_at=f.get("updated_at"),
            )
            for f in result["files"]
        ]

    def stat(self, path: str) -> FileMetadata:
        """Get file/directory metadata.

        Args:
            path: File or directory path

        Returns:
            File metadata
        """
        result = self._send_request("stat", {"path": path})
        return FileMetadata(
            size=result.get("size", 0),
            is_directory=result.get("is_directory", False),
            content_id=result.get("content_id"),
            modified_at=result.get("modified_at"),
        )

    def mkdir(self, path: str) -> None:
        """Create directory.

        Args:
            path: Directory path
        """
        self._send_request("mkdir", {"path": path})

    def sys_unlink(self, path: str) -> None:
        """Delete file or directory.

        Args:
            path: File or directory path
        """
        self._send_request("delete", {"path": path})

    def sys_rename(self, old_path: str, new_path: str) -> None:
        """Rename/move file or directory.

        Args:
            old_path: Current path
            new_path: New path
        """
        self._send_request("rename", {"old_path": old_path, "new_path": new_path})

    def access(self, path: str) -> bool:
        """Check if path exists.

        Args:
            path: File or directory path

        Returns:
            True if path exists
        """
        result = self._send_request("exists", {"path": path})
        exists_value: bool = result["exists"]
        return exists_value

    def close(self) -> None:
        """Close connection and shutdown daemon."""
        if self.sock:
            self.sock.close()
            self.sock = None

        if self.daemon_process:
            logger.info("Shutting down Rust daemon", pid=self.daemon_process.pid)
            self.daemon_process.terminate()
            try:
                self.daemon_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Daemon didn't shutdown cleanly, killing", pid=self.daemon_process.pid
                )
                self.daemon_process.kill()
                self.daemon_process.wait()
            self.daemon_process = None

        if self.socket_path and self.socket_path.exists():
            self.socket_path.unlink()

    def __enter__(self) -> "RustFUSEClient":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Context manager exit."""
        self.close()

    def __del__(self) -> None:
        """Cleanup on deletion."""
        self.close()
