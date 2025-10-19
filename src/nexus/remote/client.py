"""Remote Nexus filesystem client.

This module implements a NexusFilesystem client that communicates with
a remote Nexus RPC server over HTTP. The client implements the full
NexusFilesystem interface, making it transparent to users whether they're
working with a local or remote filesystem.

Example:
    # Connect to remote Nexus server
    nx = RemoteNexusFS("http://localhost:8080", api_key="your-api-key")

    # Use exactly like local filesystem
    nx.write("/workspace/file.txt", b"Hello, World!")
    content = nx.read("/workspace/file.txt")
    files = nx.list("/workspace")

    # Works with FUSE mount
    from nexus.fuse import mount_nexus
    mount_nexus(nx, "/mnt/nexus")
"""

from __future__ import annotations

import builtins
import uuid
from typing import Any
from urllib.parse import urljoin

import requests

from nexus.core.exceptions import (
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.core.filesystem import NexusFilesystem
from nexus.server.protocol import (
    RPCErrorCode,
    RPCRequest,
    RPCResponse,
    decode_rpc_message,
    encode_rpc_message,
)


class RemoteNexusFS(NexusFilesystem):
    """Remote Nexus filesystem client.

    Implements NexusFilesystem interface by making RPC calls to a remote server.
    """

    def __init__(
        self,
        server_url: str,
        api_key: str | None = None,
        timeout: int = 30,
    ):
        """Initialize remote filesystem client.

        Args:
            server_url: Base URL of Nexus RPC server (e.g., "http://localhost:8080")
            api_key: Optional API key for authentication
            timeout: Request timeout in seconds (default: 30)
        """
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

        # Create HTTP session
        self.session = requests.Session()
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"

    def _call_rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Make RPC call to server.

        Args:
            method: Method name
            params: Method parameters

        Returns:
            Method result

        Raises:
            NexusError: On RPC error
        """
        # Build request
        request = RPCRequest(
            jsonrpc="2.0",
            id=str(uuid.uuid4()),
            method=method,
            params=params,
        )

        # Encode request
        body = encode_rpc_message(request.to_dict())

        # Make HTTP request
        url = urljoin(self.server_url, f"/api/nfs/{method}")

        try:
            response = self.session.post(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )

            # Check HTTP status
            if response.status_code != 200:
                raise NexusError(f"HTTP {response.status_code}: {response.text}")

            # Decode response
            response_dict = decode_rpc_message(response.content)
            rpc_response = RPCResponse(
                jsonrpc=response_dict.get("jsonrpc", "2.0"),
                id=response_dict.get("id"),
                result=response_dict.get("result"),
                error=response_dict.get("error"),
            )

            # Check for RPC error
            if rpc_response.error:
                self._handle_rpc_error(rpc_response.error)

            return rpc_response.result

        except requests.RequestException as e:
            raise NexusError(f"Network error: {e}") from e

    def _handle_rpc_error(self, error: dict[str, Any]) -> None:
        """Handle RPC error response.

        Args:
            error: Error dict from RPC response

        Raises:
            Appropriate NexusError subclass
        """
        code = error.get("code", -32603)
        message = error.get("message", "Unknown error")
        data = error.get("data")

        # Map error codes to exceptions
        if code == RPCErrorCode.FILE_NOT_FOUND.value:
            path = data.get("path") if data else None
            raise NexusFileNotFoundError(path or message)
        elif code == RPCErrorCode.FILE_EXISTS.value:
            raise FileExistsError(message)
        elif code == RPCErrorCode.INVALID_PATH.value:
            raise InvalidPathError(message)
        elif (
            code == RPCErrorCode.ACCESS_DENIED.value or code == RPCErrorCode.PERMISSION_ERROR.value
        ):
            raise NexusPermissionError(message)
        elif code == RPCErrorCode.VALIDATION_ERROR.value:
            raise ValidationError(message)
        else:
            raise NexusError(f"RPC error [{code}]: {message}")

    # ============================================================
    # Core File Operations
    # ============================================================

    def read(self, path: str) -> bytes:
        """Read file content as bytes."""
        result = self._call_rpc("read", {"path": path})
        return result  # type: ignore[no-any-return]

    def write(self, path: str, content: bytes) -> None:
        """Write content to a file."""
        self._call_rpc("write", {"path": path, "content": content})

    def delete(self, path: str) -> None:
        """Delete a file."""
        self._call_rpc("delete", {"path": path})

    def exists(self, path: str) -> bool:
        """Check if a file exists."""
        result = self._call_rpc("exists", {"path": path})
        return result["exists"]  # type: ignore[no-any-return]

    # ============================================================
    # File Discovery Operations
    # ============================================================

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List files in a directory."""
        result = self._call_rpc(
            "list",
            {
                "path": path,
                "recursive": recursive,
                "details": details,
                "prefix": prefix,
            },
        )
        return result["files"]  # type: ignore[no-any-return]

    def glob(self, pattern: str, path: str = "/") -> builtins.list[str]:
        """Find files matching a glob pattern."""
        result = self._call_rpc("glob", {"pattern": pattern, "path": path})
        return result["matches"]  # type: ignore[no-any-return]

    def grep(  # type: ignore[override]
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex patterns."""
        result = self._call_rpc(
            "grep",
            {
                "pattern": pattern,
                "path": path,
                "file_pattern": file_pattern,
                "ignore_case": ignore_case,
                "max_results": max_results,
            },
        )
        return result["results"]  # type: ignore[no-any-return]

    # ============================================================
    # Directory Operations
    # ============================================================

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory."""
        self._call_rpc("mkdir", {"path": path, "parents": parents, "exist_ok": exist_ok})

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """Remove a directory."""
        self._call_rpc("rmdir", {"path": path, "recursive": recursive})

    def is_directory(self, path: str) -> bool:
        """Check if path is a directory."""
        result = self._call_rpc("is_directory", {"path": path})
        return result["is_directory"]  # type: ignore[no-any-return]

    # ============================================================
    # Lifecycle Management
    # ============================================================

    def close(self) -> None:
        """Close the client and release resources."""
        self.session.close()
