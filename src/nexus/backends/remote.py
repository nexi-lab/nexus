"""RemoteBackend — ObjectStoreABC proxy for REMOTE deployment profile.

Proxies content operations to a Nexus server over HTTP/JSON-RPC.
Implements the ``ObjectStoreABC`` interface so the kernel can run its
natural VFS pipeline identically to standalone/federation modes.

The kernel calls ``read_content(hash, context)`` / ``write_content(content,
context)`` etc.  RemoteBackend extracts the virtual path from the
``OperationContext`` and forwards the call to the server's NexusFS-level
RPC endpoint, which runs the same operation server-side.

Content deletion (``delete_content``) is a deliberate no-op: the kernel
always follows with ``metastore.delete(path)`` which triggers
``RemoteMetastore`` → server ``delete`` RPC → full server-side delete.

Issue #844: Converge RemoteNexusFS → NexusFS(profile=REMOTE).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nexus.contracts.exceptions import (
    RemoteConnectionError,
    RemoteFilesystemError,
    RemoteTimeoutError,
)
from nexus.contracts.rpc_types import RPCRequest, RPCResponse
from nexus.core.object_store import ObjectStoreABC, WriteResult
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.remote.base_client import BaseRemoteNexusFS

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


class RemoteBackend(ObjectStoreABC):
    """ObjectStoreABC implementation that proxies to a remote Nexus server.

    Uses HTTP/JSON-RPC over httpx with HTTP/2, connection pooling, and
    automatic retry (tenacity: 3 attempts, exponential backoff 1–10 s).

    Args:
        server_url: Base URL of the Nexus server (e.g. ``http://localhost:2026``).
        api_key: Optional Bearer token for authentication.
        timeout: Read/write timeout in seconds.
        connect_timeout: Connection timeout in seconds.
    """

    def __init__(
        self,
        server_url: str,
        api_key: str | None = None,
        timeout: int = 90,
        connect_timeout: int = 5,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._connect_timeout = connect_timeout

        self._default_timeout = httpx.Timeout(
            connect=connect_timeout,
            read=timeout,
            write=timeout,
            pool=timeout,
        )

        limits = httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
        )

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self._session = httpx.Client(
            limits=limits,
            timeout=self._default_timeout,
            headers=headers,
            http2=True,
            trust_env=False,
        )

        # Reuse BaseRemoteNexusFS error handling (static method access)
        self._error_handler = BaseRemoteNexusFS()

    # === Identity ===

    @property
    def name(self) -> str:
        return "remote"

    @property
    def has_root_path(self) -> bool:
        """Remote server always has a configured root path."""
        return True

    # === RPC Transport ===

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(
            (httpx.ConnectError, httpx.TimeoutException, RemoteConnectionError)
        ),
        reraise=True,
    )
    def _call_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        """Make RPC call to server with automatic retry logic."""
        request = RPCRequest(
            jsonrpc="2.0",
            id=str(uuid.uuid4()),
            method=method,
            params=params,
        )
        body = encode_rpc_message(request.to_dict())
        url = urljoin(self._server_url, f"/api/nfs/{method}")

        start_time = time.time()
        logger.debug("RemoteBackend RPC: %s params=%s", method, params)

        try:
            headers: dict[str, str] = {
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
            }

            if read_timeout is not None:
                request_timeout = httpx.Timeout(
                    connect=self._connect_timeout,
                    read=read_timeout,
                    write=read_timeout,
                    pool=read_timeout,
                )
            else:
                request_timeout = self._default_timeout

            response = self._session.post(
                url,
                content=body,
                headers=headers,
                timeout=request_timeout,
            )
            elapsed = time.time() - start_time

            if response.status_code != 200:
                logger.error(
                    "RemoteBackend RPC failed: %s — HTTP %d (%.3fs)",
                    method,
                    response.status_code,
                    elapsed,
                )
                raise RemoteFilesystemError(
                    f"Request failed: {response.text}",
                    status_code=response.status_code,
                    method=method,
                )

            response_dict = decode_rpc_message(response.content)
            rpc_response = RPCResponse(
                jsonrpc=response_dict.get("jsonrpc", "2.0"),
                id=response_dict.get("id"),
                result=response_dict.get("result"),
                error=response_dict.get("error"),
            )

            if rpc_response.error:
                logger.error(
                    "RemoteBackend RPC error: %s — %s (%.3fs)",
                    method,
                    rpc_response.error.get("message"),
                    elapsed,
                )
                self._error_handler._handle_rpc_error(rpc_response.error)

            logger.debug("RemoteBackend RPC OK: %s (%.3fs)", method, elapsed)
            return rpc_response.result

        except httpx.ConnectError as e:
            elapsed = time.time() - start_time
            logger.error("RemoteBackend connect error: %s — %s (%.3fs)", method, e, elapsed)
            raise RemoteConnectionError(
                f"Failed to connect to server: {e}",
                details={"server_url": self._server_url},
                method=method,
            ) from e

        except httpx.TimeoutException as e:
            elapsed = time.time() - start_time
            logger.error("RemoteBackend timeout: %s — %s (%.3fs)", method, e, elapsed)
            raise RemoteTimeoutError(
                f"Request timed out after {elapsed:.1f}s",
                details={
                    "connect_timeout": self._connect_timeout,
                    "read_timeout": self._timeout,
                },
                method=method,
            ) from e

        except httpx.HTTPError as e:
            elapsed = time.time() - start_time
            logger.error("RemoteBackend network error: %s — %s (%.3fs)", method, e, elapsed)
            raise RemoteFilesystemError(
                f"Network error: {e}",
                details={"elapsed": elapsed},
                method=method,
            ) from e

    # === Path Resolution ===

    @staticmethod
    def _to_server_path(context: "OperationContext | None") -> str:
        """Extract server-absolute path from OperationContext.

        The kernel sets ``virtual_path`` (the full absolute nexus path) and
        ``backend_path`` (mount-stripped relative path) on the context before
        calling backend methods.  We prefer ``virtual_path`` because it is
        already absolute; fall back to ``backend_path`` with ``/`` prepended.
        """
        if context is not None:
            if context.virtual_path:
                return context.virtual_path
            if context.backend_path:
                bp = context.backend_path
                return bp if bp.startswith("/") else "/" + bp
        return "/"

    # === CAS Content Operations ===

    def write_content(self, content: bytes, context: OperationContext | None = None) -> WriteResult:
        path = self._to_server_path(context)
        result = self._call_rpc("sys_write", {"path": path, "content": content})
        return WriteResult(
            content_hash=result.get("etag", ""),
            size=result.get("size", len(content)),
        )

    def read_content(self, content_hash: str, context: OperationContext | None = None) -> bytes:
        path = self._to_server_path(context)
        result = self._call_rpc("sys_read", {"path": path})
        parsed = self._error_handler._parse_read_response(result)
        if isinstance(parsed, dict):
            # Server returned metadata dict — extract content if present
            return bytes(parsed.get("content", b""))
        return bytes(parsed)

    def delete_content(self, content_hash: str, context: OperationContext | None = None) -> None:
        """No-op: server-side deletion is handled by RemoteMetastore.delete().

        The kernel always calls ``metastore.delete(path)`` after
        ``backend.delete_content()``.  In REMOTE mode, ``RemoteMetastore.delete``
        sends the ``delete`` RPC to the server which runs the full delete
        pipeline (CAS content + metadata).  Doing it here as well would be
        redundant and risks a double-delete race.
        """

    def get_content_size(self, content_hash: str, context: OperationContext | None = None) -> int:
        path = self._to_server_path(context)
        result = self._call_rpc("sys_stat", {"path": path})
        size: int = int(result.get("size", 0)) if isinstance(result, dict) else 0
        return size

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        abs_path = path if path.startswith("/") else "/" + path
        self._call_rpc("sys_mkdir", {"path": abs_path, "parents": parents, "exist_ok": exist_ok})

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        abs_path = path if path.startswith("/") else "/" + path
        self._call_rpc("sys_rmdir", {"path": abs_path, "recursive": recursive})

    # === Query Operations ===

    def content_exists(self, content_hash: str, context: OperationContext | None = None) -> bool:
        path = self._to_server_path(context)
        result = self._call_rpc("sys_access", {"path": path})
        if isinstance(result, dict):
            return bool(result.get("exists", False))
        return bool(result)

    def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
        """List directory contents on the remote server."""
        abs_path = path if path.startswith("/") else "/" + path
        result = self._call_rpc("sys_readdir", {"path": abs_path})
        if isinstance(result, list):
            return [str(item) for item in result]
        if isinstance(result, dict) and "items" in result:
            items: list[Any] = result["items"]
            return [
                str(item.get("path", item.get("name", ""))) if isinstance(item, dict) else str(item)
                for item in items
            ]
        return []

    # === Lifecycle ===

    def connect(self, context: OperationContext | None = None) -> None:
        """Health-check the remote server."""
        try:
            response = self._session.get(
                urljoin(self._server_url, "/api/health"),
                timeout=self._connect_timeout,
            )
            if response.status_code != 200:
                raise RemoteConnectionError(
                    f"Health check failed: HTTP {response.status_code}",
                    details={"server_url": self._server_url},
                )
        except httpx.HTTPError as e:
            raise RemoteConnectionError(
                f"Cannot reach server: {e}",
                details={"server_url": self._server_url},
            ) from e

    def disconnect(self, context: OperationContext | None = None) -> None:
        """Close the httpx session."""
        self._session.close()

    def close(self) -> None:
        self._session.close()
