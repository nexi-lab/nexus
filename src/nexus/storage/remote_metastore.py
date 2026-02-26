"""RemoteMetastore — MetastoreABC proxy for REMOTE deployment profile.

Proxies metadata operations to a Nexus server over HTTP/JSON-RPC.
Shares the same transport pattern as ``RemoteBackend``.

Server is the single source of truth (SSOT) for metadata — this class
is a stateless proxy, **not** a cache.  No local state, no invalidation.

Issue #844: Converge RemoteNexusFS → NexusFS(profile=REMOTE).

Follow-up (Task #1133): Evaluate consolidating HTTP RPC with gRPC
transport (currently used by RaftClient for federation).
Follow-up (Task #1134): Evaluate using RaftMetadataStore.remote()
once gRPC metadata endpoint is promoted.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any
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
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.rpc_types import RPCRequest, RPCResponse
from nexus.core.metastore import MetastoreABC
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.remote.base_client import BaseRemoteNexusFS

logger = logging.getLogger(__name__)


def _dict_to_file_metadata(d: dict[str, Any]) -> FileMetadata:
    """Convert a server response dict to a FileMetadata dataclass."""
    created_at = d.get("created_at")
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    modified_at = d.get("modified_at")
    if isinstance(modified_at, str):
        modified_at = datetime.fromisoformat(modified_at)

    return FileMetadata(
        path=d.get("path", ""),
        backend_name=d.get("backend_name", ""),
        physical_path=d.get("physical_path", ""),
        size=d.get("size", 0),
        etag=d.get("etag"),
        mime_type=d.get("mime_type"),
        created_at=created_at if isinstance(created_at, datetime) else None,
        modified_at=modified_at if isinstance(modified_at, datetime) else None,
        version=d.get("version", 1),
        zone_id=d.get("zone_id"),
        created_by=d.get("created_by"),
        owner_id=d.get("owner_id"),
        entry_type=d.get("entry_type", 0),
        target_zone_id=d.get("target_zone_id"),
        i_links_count=d.get("i_links_count", 0),
    )


class RemoteMetastore(MetastoreABC):
    """MetastoreABC implementation that proxies to a remote Nexus server.

    Uses HTTP/JSON-RPC over httpx with HTTP/2 and automatic retry.
    All metadata queries are forwarded to the server — no local state.

    Args:
        server_url: Base URL of the Nexus server.
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

        self._error_handler = BaseRemoteNexusFS()

    # === RPC Transport ===

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(
            (httpx.ConnectError, httpx.TimeoutException, RemoteConnectionError)
        ),
        reraise=True,
    )
    def _call_rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
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

        try:
            headers: dict[str, str] = {
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
            }

            response = self._session.post(
                url,
                content=body,
                headers=headers,
                timeout=self._default_timeout,
            )
            elapsed = time.time() - start_time

            if response.status_code != 200:
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
                    "RemoteMetastore RPC error: %s — %s (%.3fs)",
                    method,
                    rpc_response.error.get("message"),
                    elapsed,
                )
                self._error_handler._handle_rpc_error(rpc_response.error)

            return rpc_response.result

        except httpx.ConnectError as e:
            raise RemoteConnectionError(
                f"Failed to connect to server: {e}",
                details={"server_url": self._server_url},
                method=method,
            ) from e

        except httpx.TimeoutException as e:
            elapsed = time.time() - start_time
            raise RemoteTimeoutError(
                f"Request timed out after {elapsed:.1f}s",
                details={"connect_timeout": self._connect_timeout, "read_timeout": self._timeout},
                method=method,
            ) from e

        except httpx.HTTPError as e:
            elapsed = time.time() - start_time
            raise RemoteFilesystemError(
                f"Network error: {e}",
                details={"elapsed": elapsed},
                method=method,
            ) from e

    # === MetastoreABC Implementation ===

    def get(self, path: str) -> FileMetadata | None:
        """Get metadata for a file by proxying ``stat`` to the server."""
        try:
            result = self._call_rpc("sys_stat", {"path": path})
        except Exception:
            return None
        if result is None:
            return None
        if isinstance(result, dict):
            return _dict_to_file_metadata(result)
        return None

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        """Store metadata by proxying ``set_metadata`` to the server.

        The *consistency* hint is forwarded so the server can honour it.
        Non-fatal: in REMOTE mode the server already owns metadata —
        failures here (e.g. during init) are logged but not raised.
        """
        try:
            self._call_rpc(
                "sys_setattr",
                {"path": metadata.path, "metadata": metadata.to_dict(), "consistency": consistency},
            )
        except Exception as exc:
            logger.debug("RemoteMetastore.put(%s) failed (non-fatal): %s", metadata.path, exc)
        return None

    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        """Delete metadata by proxying ``delete`` to the server."""
        result = self._call_rpc("sys_unlink", {"path": path, "consistency": consistency})
        if isinstance(result, dict):
            return result
        return {"path": path}

    def exists(self, path: str) -> bool:
        """Check if metadata exists by proxying ``exists`` to the server."""
        result = self._call_rpc("sys_access", {"path": path})
        if isinstance(result, dict):
            return bool(result.get("exists", False))
        return bool(result)

    def list(self, prefix: str = "", recursive: bool = True, **kwargs: Any) -> list[FileMetadata]:
        """List files by proxying ``list`` to the server."""
        params: dict[str, Any] = {"path": prefix, "recursive": recursive}
        if kwargs:
            params.update(kwargs)
        result = self._call_rpc("sys_readdir", params)
        if not result:
            return []

        items: list[Any] = []
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict) and "items" in result:
            items = result["items"]

        metadata_list: list[FileMetadata] = []
        for item in items:
            if isinstance(item, dict) and "path" in item:
                metadata_list.append(_dict_to_file_metadata(item))
            elif isinstance(item, str):
                metadata_list.append(
                    FileMetadata(
                        path=item,
                        backend_name="remote",
                        physical_path=item,
                        size=0,
                    )
                )
        return metadata_list

    def close(self) -> None:
        """Close the httpx session."""
        self._session.close()
