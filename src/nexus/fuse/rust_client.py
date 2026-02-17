"""Python client for Rust FUSE daemon via Unix socket IPC.

This module provides a bridge between Python FUSE operations and the high-performance
Rust daemon, enabling 10-100x speedup on hot path operations.
"""

import base64
import json
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


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
    etag: str | None = None
    modified_at: str | None = None


class RustFUSEClient:
    """Client for communicating with Rust FUSE daemon via Unix socket.

    The Rust daemon provides high-performance file operations while Python
    handles permissions, namespaces, and orchestration.
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
        """Spawn Rust daemon process."""
        cmd = [
            self.rust_binary,
            "daemon",
            "--url",
            self.nexus_url,
            "--api-key",
            self.api_key,
        ]

        if self.agent_id:
            cmd.extend(["--agent-id", self.agent_id])

        logger.info("Starting Rust FUSE daemon", cmd=cmd)

        # Spawn daemon and capture stdout to get socket path
        self.daemon_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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

    def _send_request(self, method: str, params: dict) -> dict:
        """Send JSON-RPC request to Rust daemon.

        Args:
            method: RPC method name
            params: Method parameters

        Returns:
            Result from Rust daemon

        Raises:
            RuntimeError: If request fails
        """
        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params,
        }

        # Send request
        request_json = json.dumps(request) + "\n"
        if not self.sock:
            raise RuntimeError("Not connected to daemon")
        self.sock.sendall(request_json.encode())

        # Receive response
        response_data = b""
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("Connection closed by daemon")
            response_data += chunk
            if b"\n" in response_data:
                break

        response = json.loads(response_data.decode())

        # Check for errors
        if "error" in response:
            error = response["error"]
            errno = error.get("data", {}).get("errno", 5)  # Default to EIO
            raise OSError(errno, error["message"])

        return response.get("result", {})

    def read(self, path: str) -> bytes:
        """Read file contents.

        Args:
            path: File path

        Returns:
            File contents as bytes
        """
        result = self._send_request("read", {"path": path})
        return base64.b64decode(result["data"])

    def write(self, path: str, content: bytes) -> None:
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

    def list(self, path: str) -> list[FileEntry]:
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
            etag=result.get("etag"),
            modified_at=result.get("modified_at"),
        )

    def mkdir(self, path: str) -> None:
        """Create directory.

        Args:
            path: Directory path
        """
        self._send_request("mkdir", {"path": path})

    def delete(self, path: str) -> None:
        """Delete file or directory.

        Args:
            path: File or directory path
        """
        self._send_request("delete", {"path": path})

    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move file or directory.

        Args:
            old_path: Current path
            new_path: New path
        """
        self._send_request("rename", {"old_path": old_path, "new_path": new_path})

    def exists(self, path: str) -> bool:
        """Check if path exists.

        Args:
            path: File or directory path

        Returns:
            True if path exists
        """
        result = self._send_request("exists", {"path": path})
        return result["exists"]

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
