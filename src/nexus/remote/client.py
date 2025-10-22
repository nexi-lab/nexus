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
import logging
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from urllib3.util.retry import Retry

from nexus.core.exceptions import (
    ConflictError,
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

logger = logging.getLogger(__name__)


class RemoteFilesystemError(NexusError):
    """Enhanced remote filesystem error with detailed information.

    Attributes:
        message: Human-readable error message
        status_code: HTTP status code (if applicable)
        details: Additional error details
        method: RPC method that failed
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        method: str | None = None,
    ):
        """Initialize remote filesystem error.

        Args:
            message: Error message
            status_code: HTTP status code
            details: Additional error details
            method: RPC method that failed
        """
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.method = method

        # Build detailed error message
        error_parts = [message]
        if method:
            error_parts.append(f"(method: {method})")
        if status_code:
            error_parts.append(f"[HTTP {status_code}]")

        super().__init__(" ".join(error_parts))


class RemoteConnectionError(RemoteFilesystemError):
    """Error connecting to remote Nexus server."""

    pass


class RemoteTimeoutError(RemoteFilesystemError):
    """Timeout while communicating with remote server."""

    pass


class RemoteNexusFS(NexusFilesystem):
    """Remote Nexus filesystem client.

    Implements NexusFilesystem interface by making RPC calls to a remote server.
    """

    def __init__(
        self,
        server_url: str,
        api_key: str | None = None,
        timeout: int = 30,
        connect_timeout: int = 5,
        max_retries: int = 3,
        pool_connections: int = 10,
        pool_maxsize: int = 20,
    ):
        """Initialize remote filesystem client.

        Args:
            server_url: Base URL of Nexus RPC server (e.g., "http://localhost:8080")
            api_key: Optional API key for authentication
            timeout: Request timeout in seconds (default: 30)
            connect_timeout: Connection timeout in seconds (default: 5)
            max_retries: Maximum number of retry attempts (default: 3)
            pool_connections: Number of connection pools (default: 10)
            pool_maxsize: Maximum pool size (default: 20)
        """
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_retries = max_retries

        # Create HTTP session with connection pooling
        self.session = requests.Session()

        # Configure connection pooling with retry strategy
        # Retry on connection errors, timeouts, and 5xx server errors
        retry_strategy = Retry(
            total=0,  # We'll handle retries with tenacity at RPC level
            connect=0,
            read=0,
            status_forcelist=[500, 502, 503, 504],
            backoff_factor=0,
        )

        adapter = HTTPAdapter(
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
            max_retries=retry_strategy,
        )

        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(
            (requests.ConnectionError, requests.Timeout, RemoteConnectionError)
        ),
        reraise=True,
    )
    def _call_rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Make RPC call to server with automatic retry logic.

        This method automatically retries on transient failures (connection errors,
        timeouts) using exponential backoff (1s, 2s, 4s, up to 10s).

        Args:
            method: Method name
            params: Method parameters

        Returns:
            Method result

        Raises:
            NexusError: On RPC error
            RemoteConnectionError: On connection failure
            RemoteTimeoutError: On timeout
            RemoteFilesystemError: On other remote errors
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

        # Log API call
        start_time = time.time()
        logger.debug(f"API call: {method} with params: {params}")

        try:
            # Use tuple for timeout: (connect_timeout, read_timeout)
            response = self.session.post(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                timeout=(self.connect_timeout, self.timeout),
            )

            elapsed = time.time() - start_time

            # Check HTTP status
            if response.status_code != 200:
                logger.error(
                    f"API call failed: {method} - HTTP {response.status_code} ({elapsed:.3f}s)"
                )
                raise RemoteFilesystemError(
                    f"Request failed: {response.text}",
                    status_code=response.status_code,
                    method=method,
                )

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
                logger.error(
                    f"API call RPC error: {method} - {rpc_response.error.get('message')} ({elapsed:.3f}s)"
                )
                self._handle_rpc_error(rpc_response.error)

            logger.info(f"API call completed: {method} ({elapsed:.3f}s)")
            return rpc_response.result

        except requests.ConnectionError as e:
            elapsed = time.time() - start_time
            logger.error(f"API call connection error: {method} - {e} ({elapsed:.3f}s)")
            raise RemoteConnectionError(
                f"Failed to connect to server: {e}",
                details={"server_url": self.server_url},
                method=method,
            ) from e

        except requests.Timeout as e:
            elapsed = time.time() - start_time
            logger.error(f"API call timeout: {method} - {e} ({elapsed:.3f}s)")
            raise RemoteTimeoutError(
                f"Request timed out after {elapsed:.1f}s",
                details={
                    "connect_timeout": self.connect_timeout,
                    "read_timeout": self.timeout,
                },
                method=method,
            ) from e

        except requests.RequestException as e:
            elapsed = time.time() - start_time
            logger.error(f"API call network error: {method} - {e} ({elapsed:.3f}s)")
            raise RemoteFilesystemError(
                f"Network error: {e}",
                details={"elapsed": elapsed},
                method=method,
            ) from e

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
        elif code == RPCErrorCode.CONFLICT.value:
            # Extract etag info from data
            expected_etag = data.get("expected_etag") if data else "(unknown)"
            current_etag = data.get("current_etag") if data else "(unknown)"
            path = data.get("path") if data else "unknown"
            raise ConflictError(path, expected_etag, current_etag)
        else:
            raise NexusError(f"RPC error [{code}]: {message}")

    # ============================================================
    # Core File Operations
    # ============================================================

    def read(
        self,
        path: str,
        context: Any = None,  # noqa: ARG002
        return_metadata: bool = False,
    ) -> bytes | dict[str, Any]:
        """Read file content as bytes.

        Args:
            path: Virtual path to read
            context: Unused in remote client (handled server-side)
            return_metadata: If True, return dict with content and metadata (v0.3.9)

        Returns:
            If return_metadata=False: File content as bytes
            If return_metadata=True: Dict with content, etag, version, etc.
        """
        result = self._call_rpc("read", {"path": path, "return_metadata": return_metadata})
        return result  # type: ignore[no-any-return]

    def write(
        self,
        path: str,
        content: bytes,
        context: Any = None,  # noqa: ARG002
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Write content to a file with optional optimistic concurrency control.

        Args:
            path: Virtual path to write
            content: File content as bytes
            context: Unused in remote client (handled server-side)
            if_match: Optional etag for OCC (v0.3.9)
            if_none_match: If True, create-only mode (v0.3.9)
            force: If True, skip version check (v0.3.9)

        Returns:
            Dict with metadata (etag, version, modified_at, size)

        Raises:
            ConflictError: If if_match doesn't match current etag (v0.3.9)
        """
        result = self._call_rpc(
            "write",
            {
                "path": path,
                "content": content,
                "if_match": if_match,
                "if_none_match": if_none_match,
                "force": force,
            },
        )
        return result  # type: ignore[no-any-return]

    def delete(self, path: str) -> None:
        """Delete a file."""
        self._call_rpc("delete", {"path": path})

    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move a file (metadata-only operation)."""
        self._call_rpc("rename", {"old_path": old_path, "new_path": new_path})

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

    def get_available_namespaces(self) -> builtins.list[str]:
        """Get list of available namespace directories.

        Returns the built-in namespaces that should appear at root level.
        Filters based on tenant and admin context on the server side.

        Returns:
            List of namespace names (e.g., ["workspace", "shared", "external"])
        """
        result = self._call_rpc("get_available_namespaces", {})
        return result["namespaces"]  # type: ignore[no-any-return]

    # ============================================================
    # Version Tracking Operations (v0.3.5)
    # ============================================================

    def get_version(self, path: str, version: int) -> bytes:
        """Get a specific version of a file."""
        result = self._call_rpc("get_version", {"path": path, "version": version})
        return result  # type: ignore[no-any-return]

    def list_versions(self, path: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a file."""
        result = self._call_rpc("list_versions", {"path": path})
        return result["versions"]  # type: ignore[no-any-return]

    def rollback(self, path: str, version: int, context: Any = None) -> None:  # noqa: ARG002
        """Rollback file to a previous version."""
        # context is unused in remote client (handled server-side)
        self._call_rpc("rollback", {"path": path, "version": version})

    def diff_versions(
        self, path: str, v1: int, v2: int, mode: str = "metadata"
    ) -> dict[str, Any] | str:
        """Compare two versions of a file."""
        result = self._call_rpc("diff_versions", {"path": path, "v1": v1, "v2": v2, "mode": mode})
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Lifecycle Management
    # ============================================================

    def close(self) -> None:
        """Close the client and release resources."""
        self.session.close()
