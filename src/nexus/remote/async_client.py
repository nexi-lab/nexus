"""Async Remote Nexus filesystem client.

This module implements an async NexusFilesystem client that communicates with
a remote Nexus RPC server over HTTP using httpx AsyncClient.

Example:
    # Connect to remote Nexus server (async)
    nx = AsyncRemoteNexusFS("http://localhost:2026", api_key="your-api-key")

    # Use with async/await
    content = await nx.read("/workspace/file.txt")
    await nx.write("/workspace/file.txt", b"Hello, World!")
    files = await nx.list("/workspace")

    # Parallel reads (main benefit of async)
    import asyncio
    paths = ["/file1.txt", "/file2.txt", "/file3.txt"]
    contents = await asyncio.gather(*[nx.read(p) for p in paths])
"""

from __future__ import annotations

import builtins
import logging
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nexus.core.exceptions import (
    NexusFileNotFoundError,
)
from nexus.remote.base_client import BaseRemoteNexusFS
from nexus.server.protocol import (
    RPCRequest,
    RPCResponse,
    decode_rpc_message,
    encode_rpc_message,
)

from .client import (
    RemoteConnectionError,
    RemoteFilesystemError,
    RemoteTimeoutError,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class AsyncRemoteNexusFS(BaseRemoteNexusFS):
    """Async remote Nexus filesystem client.

    This client uses httpx.AsyncClient for non-blocking HTTP calls,
    enabling parallel file operations and integration with async frameworks.

    Example:
        >>> nx = AsyncRemoteNexusFS("http://localhost:2026", api_key="sk-xxx")
        >>> content = await nx.read("/workspace/file.txt")
        >>> await nx.write("/workspace/new.txt", b"Hello!")

        # Parallel reads
        >>> paths = ["/a.txt", "/b.txt", "/c.txt"]
        >>> contents = await asyncio.gather(*[nx.read(p) for p in paths])
    """

    def __init__(
        self,
        server_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        connect_timeout: float = 5.0,
        pool_connections: int = 10,
        pool_maxsize: int = 10,
        negative_cache_capacity: int = 100_000,
        negative_cache_fp_rate: float = 0.01,
    ):
        """Initialize async remote filesystem client.

        Args:
            server_url: Nexus server URL (e.g., "http://localhost:2026")
            api_key: Optional API key for authentication
            timeout: Read timeout in seconds (default: 30s)
            connect_timeout: Connection timeout in seconds (default: 5s)
            pool_connections: Number of connection pool connections (default: 10)
            pool_maxsize: Maximum connection pool size (default: 10)
            negative_cache_capacity: Max entries for negative cache Bloom filter (default: 100,000)
            negative_cache_fp_rate: False positive rate for Bloom filter (default: 0.01 = 1%)
        """
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.connect_timeout = connect_timeout

        # Zone/agent identity (populated from auth info)
        self._zone_id: str | None = None
        self._agent_id: str | None = None

        # Configure connection limits
        limits = httpx.Limits(
            max_connections=pool_maxsize,
            max_keepalive_connections=pool_connections,
        )

        # Configure timeouts
        timeout_config = httpx.Timeout(
            connect=connect_timeout,
            read=timeout,
            write=timeout,
            pool=timeout,
        )

        # Build headers
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Create async httpx client with HTTP/2 for multiplexing
        self._client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout_config,
            headers=headers,
            http2=True,
        )

        self._initialized = False

        # Initialize negative cache Bloom filter for reducing round-trips
        # on non-existent files (issue #858)
        self._negative_cache_capacity = negative_cache_capacity
        self._negative_cache_fp_rate = negative_cache_fp_rate
        self._negative_bloom: Any = None
        self._init_negative_cache()

    async def __aenter__(self) -> AsyncRemoteNexusFS:
        """Async context manager entry."""
        await self._ensure_initialized()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def _ensure_initialized(self) -> None:
        """Ensure client is initialized (fetch auth info if needed)."""
        if not self._initialized and self.api_key:
            try:
                await self._fetch_auth_info()
            except Exception as e:
                logger.warning(f"Failed to fetch auth info: {e}")
            self._initialized = True

    async def _fetch_auth_info(self) -> None:
        """Fetch authenticated user info from server."""
        try:
            response = await self._client.get(
                urljoin(self.server_url, "/api/auth/whoami"),
                timeout=self.connect_timeout,
            )

            if response.status_code == 200:
                auth_info = response.json()
                self._parse_auth_info(auth_info)
            else:
                logger.warning(f"Failed to fetch auth info: HTTP {response.status_code}")
        except Exception as e:
            logger.debug(f"Could not fetch auth info: {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(
            (httpx.ConnectError, httpx.TimeoutException, RemoteConnectionError)
        ),
        reraise=True,
    )
    async def _call_rpc(
        self, method: str, params: dict[str, Any] | None = None, read_timeout: float | None = None
    ) -> Any:
        """Make async RPC call to server with automatic retry logic.

        Args:
            method: Method name
            params: Method parameters
            read_timeout: Optional custom read timeout

        Returns:
            Method result

        Raises:
            NexusError: On RPC error
            RemoteConnectionError: On connection failure
            RemoteTimeoutError: On timeout
        """
        await self._ensure_initialized()

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

        start_time = time.time()
        logger.debug(f"API call: {method} with params: {params}")

        try:
            # Build headers
            headers = {
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
            }

            if self._agent_id:
                headers["X-Agent-ID"] = self._agent_id

            if self._zone_id:
                headers["X-Nexus-Zone-ID"] = self._zone_id

            # Configure timeout for this request
            actual_read_timeout = read_timeout if read_timeout is not None else self.timeout
            request_timeout = httpx.Timeout(
                connect=self.connect_timeout,
                read=actual_read_timeout,
                write=actual_read_timeout,
                pool=actual_read_timeout,
            )

            network_start = time.time()
            response = await self._client.post(
                url,
                content=body,
                headers=headers,
                timeout=request_timeout,
            )
            network_time = time.time() - network_start

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
            decode_start = time.time()
            response_dict = decode_rpc_message(response.content)
            rpc_response = RPCResponse(
                jsonrpc=response_dict.get("jsonrpc", "2.0"),
                id=response_dict.get("id"),
                result=response_dict.get("result"),
                error=response_dict.get("error"),
            )
            decode_time = time.time() - decode_start

            # Check for RPC error
            if rpc_response.error:
                logger.error(
                    f"API call RPC error: {method} - {rpc_response.error.get('message')} ({elapsed:.3f}s)"
                )
                self._handle_rpc_error(rpc_response.error)

            # Log detailed timing for grep operations
            if method == "grep":
                logger.warning(
                    f"[CLIENT-PERF] {method}: total={elapsed * 1000:.0f}ms "
                    f"(network={network_time * 1000:.0f}ms, decode={decode_time * 1000:.0f}ms, "
                    f"response_size={len(response.content)} bytes)"
                )

            logger.info(f"API call completed: {method} ({elapsed:.3f}s)")
            return rpc_response.result

        except httpx.ConnectError as e:
            elapsed = time.time() - start_time
            logger.error(f"API call connection error: {method} - {e} ({elapsed:.3f}s)")
            raise RemoteConnectionError(
                f"Failed to connect to server: {e}",
                details={"server_url": self.server_url},
                method=method,
            ) from e

        except httpx.TimeoutException as e:
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

        except httpx.HTTPError as e:
            elapsed = time.time() - start_time
            logger.error(f"API call network error: {method} - {e} ({elapsed:.3f}s)")
            raise RemoteFilesystemError(
                f"Network error: {e}",
                details={"elapsed": elapsed},
                method=method,
            ) from e

    # ============================================================
    # Core File Operations (Async)
    # ============================================================

    async def read(
        self,
        path: str,
        context: Any = None,  # noqa: ARG002
        return_metadata: bool = False,
        parsed: bool = False,
    ) -> bytes | dict[str, Any]:
        """Read file content as bytes (async).

        Args:
            path: Virtual path to read
            context: Unused in remote client
            return_metadata: If True, return dict with content and metadata
            parsed: If True, return parsed text content instead of raw bytes.
                   Uses the best available parse provider (Unstructured, LlamaParse, MarkItDown).
                   First checks for cached parsed_text, then parses on-demand if needed.

        Returns:
            File content as bytes, or dict with metadata if requested.
            If parsed=True, returns parsed markdown text as bytes.
        """
        # Check negative cache first (issue #858)
        if self._negative_cache_check(path):
            raise NexusFileNotFoundError(path)

        params = {"path": path, "return_metadata": return_metadata, "parsed": parsed}
        try:
            result = await self._call_rpc("read", params)
        except NexusFileNotFoundError:
            self._negative_cache_add(path)
            raise

        return self._parse_read_response(result, return_metadata)

    async def read_bulk(
        self,
        paths: list[str],
        context: Any = None,  # noqa: ARG002
        return_metadata: bool = False,
        skip_errors: bool = True,
    ) -> dict[str, bytes | dict[str, Any] | None]:
        """Read multiple files in a single RPC call (async).

        Args:
            paths: List of virtual paths to read
            context: Unused in remote client
            return_metadata: If True, return dicts with content and metadata
            skip_errors: If True, skip files that can't be read

        Returns:
            Dict mapping paths to file content
        """
        result = await self._call_rpc(
            "read_bulk",
            {"paths": paths, "return_metadata": return_metadata, "skip_errors": skip_errors},
        )
        return result  # type: ignore[no-any-return]

    async def write(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,  # noqa: ARG002
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Write content to a file (async).

        Args:
            path: Virtual path to write
            content: File content as bytes or str
            context: Unused in remote client
            if_match: Optional etag for optimistic concurrency
            if_none_match: If True, create-only mode
            force: If True, skip version check

        Returns:
            Dict with metadata (etag, version, modified_at, size)
        """
        if isinstance(content, str):
            content = content.encode("utf-8")

        result = await self._call_rpc(
            "write",
            {
                "path": path,
                "content": content,
                "if_match": if_match,
                "if_none_match": if_none_match,
                "force": force,
            },
        )

        # Invalidate negative cache after successful write (issue #858)
        self._negative_cache_invalidate(path)

        return result  # type: ignore[no-any-return]

    async def write_stream(
        self,
        path: str,
        chunks: Iterator[bytes],
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Write file content from an iterator of chunks (async).

        This is a memory-efficient alternative to write() for large files.
        Note: For remote client, chunks are collected and sent as single request.

        Args:
            path: Virtual path to write
            chunks: Iterator yielding byte chunks
            context: Unused in remote client (handled server-side)

        Returns:
            Dict with metadata (etag, version, modified_at, size)
        """
        # Collect chunks for RPC call (streaming over RPC not yet supported)
        content = b"".join(chunks)

        result = await self._call_rpc(
            "write_stream",
            {
                "path": path,
                "chunks": content,  # Send as single blob
            },
        )

        # Invalidate negative cache after successful write (issue #858)
        self._negative_cache_invalidate(path)

        return result  # type: ignore[no-any-return]

    # ============================================================
    # Delta Sync Operations (Issue #869)
    # ============================================================

    async def delta_read(
        self,
        path: str,
        client_content: bytes | None = None,
        client_hash: str | None = None,
        max_delta_ratio: float = 0.8,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Read file with delta optimization for bandwidth reduction (async).

        If client provides their cached content and hash, server returns
        only the binary diff (delta) instead of the full file content.
        This reduces bandwidth by 50-90% for files with small changes.

        Args:
            path: Virtual path to read
            client_content: Client's cached file content (required for delta)
            client_hash: Hash of client's cached content (required for delta)
            max_delta_ratio: Max delta/original size ratio before full file (default: 0.8)
            context: Unused in remote client (handled server-side)

        Returns:
            Dict with one of:
            - {"unchanged": True, "server_hash": ...} if file unchanged
            - {"delta": bytes, "server_hash": ..., "is_full": False} if delta returned
            - {"content": bytes, "server_hash": ..., "is_full": True} if full file returned
        """
        params: dict[str, Any] = {
            "path": path,
            "max_delta_ratio": max_delta_ratio,
        }

        if client_hash is not None:
            params["client_hash"] = client_hash

        if client_content is not None:
            params["client_content"] = client_content

        result = await self._call_rpc("delta_read", params)

        return self._decode_delta_read_response(result)

    async def delta_write(
        self,
        path: str,
        delta: bytes,
        base_hash: str,
        if_match: str | None = None,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Write file using delta patch for bandwidth reduction (async).

        Instead of sending the full file, client sends only the binary diff
        (delta) from the current server version. Server applies the patch.

        Args:
            path: Virtual path to write
            delta: Binary delta patch (bsdiff4 format)
            base_hash: Hash of the current server content (used for conflict detection)
            if_match: Optional etag for optimistic concurrency control
            context: Unused in remote client (handled server-side)

        Returns:
            Dict with:
            - {"bytes_written": int, "new_hash": str, "patch_applied": True} on success
            - {"error": "conflict", "reason": str, ...} on conflict
        """
        params: dict[str, Any] = {
            "path": path,
            "delta": delta,
            "base_hash": base_hash,
        }

        if if_match is not None:
            params["if_match"] = if_match

        result = await self._call_rpc("delta_write", params)

        # Invalidate negative cache after successful write (issue #858)
        if isinstance(result, dict) and result.get("patch_applied"):
            self._negative_cache_invalidate(path)

        return result  # type: ignore[no-any-return]

    async def delete(
        self,
        path: str,
        context: Any = None,  # noqa: ARG002
        if_match: str | None = None,
    ) -> bool:
        """Delete a file (async).

        Args:
            path: Virtual path to delete
            context: Unused in remote client
            if_match: Optional etag for optimistic concurrency

        Returns:
            True if deleted
        """
        params: dict[str, Any] = {"path": path}
        if if_match is not None:
            params["if_match"] = if_match
        result = await self._call_rpc("delete", params)

        # Invalidate negative cache after delete (issue #858)
        self._negative_cache_invalidate(path)

        return result  # type: ignore[no-any-return]

    async def delete_bulk(
        self,
        paths: builtins.list[str],
        recursive: bool = False,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, dict]:
        """Delete multiple files or directories in a single operation (async).

        Each path is processed independently - failures on one don't affect others.

        Args:
            paths: List of paths to delete
            recursive: If True, delete non-empty directories (like rm -rf)
            context: Unused in remote client

        Returns:
            Dictionary mapping each path to its result:
                {"success": True} or {"success": False, "error": "error message"}

        Example:
            >>> results = await nx.delete_bulk(['/a.txt', '/b.txt', '/folder'])
            >>> for path, result in results.items():
            ...     if result['success']:
            ...         print(f"Deleted {path}")
        """
        result = await self._call_rpc("delete_bulk", {"paths": paths, "recursive": recursive})

        # Invalidate negative cache after bulk delete (issue #858)
        if paths:
            self._negative_cache_invalidate_bulk(paths)

        return result  # type: ignore[no-any-return]

    async def exists(
        self,
        path: str,
        context: Any = None,  # noqa: ARG002
    ) -> bool:
        """Check if file exists (async).

        Args:
            path: Virtual path to check
            context: Unused in remote client

        Returns:
            True if file exists
        """
        # Check negative cache first (issue #858)
        if self._negative_cache_check(path):
            return False

        result = await self._call_rpc("exists", {"path": path})
        exists = result["exists"]

        # Add to negative cache if file doesn't exist
        if not exists:
            self._negative_cache_add(path)

        return exists  # type: ignore[no-any-return]

    async def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        show_parsed: bool = True,
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List files in directory (async).

        Args:
            path: Directory path to list
            recursive: If True, list recursively (default: True)
            details: If True, return detailed metadata dicts
            prefix: Filter by path prefix
            show_parsed: Include parsed file attributes
            context: Unused in remote client

        Returns:
            List of file paths (str) or metadata dicts if details=True
        """
        result = await self._call_rpc(
            "list",
            {
                "path": path,
                "recursive": recursive,
                "details": details,
                "prefix": prefix,
                "show_parsed": show_parsed,
            },
        )
        return result["files"]  # type: ignore[no-any-return]

    async def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Create directory (async).

        Args:
            path: Directory path to create
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists
            context: Unused in remote client

        Returns:
            Directory metadata dict
        """
        result = await self._call_rpc(
            "mkdir", {"path": path, "parents": parents, "exist_ok": exist_ok}
        )
        return result  # type: ignore[no-any-return]

    async def glob(
        self,
        pattern: str,
        path: str = "/",
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[str]:
        """Find files matching glob pattern (async).

        Args:
            pattern: Glob pattern (e.g., "*.txt", "**/*.py")
            path: Base path to search from
            context: Unused in remote client

        Returns:
            List of matching file paths
        """
        result = await self._call_rpc("glob", {"pattern": pattern, "path": path})
        return result["matches"]  # type: ignore[no-any-return]

    async def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex patterns (async).

        Args:
            pattern: Regex pattern to search for
            path: Base path to search from
            file_pattern: Optional glob pattern to filter files (e.g., "*.py")
            ignore_case: If True, case-insensitive search
            max_results: Maximum number of results (default: 1000)
            search_mode: Search mode - "auto", "fast", "thorough"
            context: Unused in remote client

        Returns:
            List of match dicts with path, line_number, line, match
        """
        result = await self._call_rpc(
            "grep",
            {
                "pattern": pattern,
                "path": path,
                "file_pattern": file_pattern,
                "ignore_case": ignore_case,
                "max_results": max_results,
                "search_mode": search_mode,
            },
        )
        return result["results"]  # type: ignore[no-any-return]

    async def rename(
        self,
        old_path: str,
        new_path: str,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Rename/move a file (async).

        Args:
            old_path: Current file path
            new_path: New file path
            context: Unused in remote client

        Returns:
            New file metadata dict
        """
        result = await self._call_rpc("rename", {"old_path": old_path, "new_path": new_path})

        # Invalidate negative cache for old path (issue #858)
        self._negative_cache_invalidate(old_path)

        return result  # type: ignore[no-any-return]

    async def rename_bulk(
        self,
        renames: builtins.list[tuple[str, str]],
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, dict]:
        """Rename/move multiple files in a single operation (async).

        Each rename is processed independently - failures on one don't affect others.
        This is a metadata-only operation (instant, regardless of file size).

        Args:
            renames: List of (old_path, new_path) tuples
            context: Unused in remote client

        Returns:
            Dictionary mapping each old_path to its result:
                {"success": True, "new_path": "..."} or {"success": False, "error": "..."}

        Example:
            >>> results = await nx.rename_bulk([
            ...     ('/old1.txt', '/new1.txt'),
            ...     ('/old2.txt', '/new2.txt'),
            ... ])
        """
        result = await self._call_rpc("rename_bulk", {"renames": renames})

        # Invalidate negative cache after bulk rename (issue #858)
        if renames:
            self._negative_cache_invalidate_bulk([old_path for old_path, _ in renames])

        return result  # type: ignore[no-any-return]

    async def append(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,  # noqa: ARG002
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Append content to an existing file or create new file (async).

        Args:
            path: Virtual path to append to
            content: Content to append as bytes or str (str will be UTF-8 encoded)
            context: Unused in remote client
            if_match: Optional etag for optimistic concurrency control
            force: If True, skip version check

        Returns:
            Dict with metadata (etag, version, modified_at, size)

        Raises:
            ConflictError: If if_match doesn't match current etag

        Examples:
            >>> # Append to a log file
            >>> await nx.append("/logs/app.log", "New log entry\\n")

            >>> # Build JSONL file incrementally
            >>> import json
            >>> for record in records:
            ...     line = json.dumps(record) + "\\n"
            ...     await nx.append("/data/events.jsonl", line)
        """
        if isinstance(content, str):
            content = content.encode("utf-8")

        result = await self._call_rpc(
            "append",
            {
                "path": path,
                "content": content,
                "if_match": if_match,
                "force": force,
            },
        )

        # Invalidate negative cache after successful append (issue #858)
        self._negative_cache_invalidate(path)

        return result  # type: ignore[no-any-return]

    async def stat(
        self,
        path: str,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Get file metadata without reading content (async).

        This is useful for getting file size before streaming, or checking
        file properties without the overhead of reading large files.

        Args:
            path: Virtual path to stat
            context: Unused in remote client

        Returns:
            Dict with file metadata:
                - size: File size in bytes
                - etag: Content hash
                - version: Version number
                - modified_at: Last modification timestamp
                - is_directory: Whether path is a directory

        Raises:
            NexusFileNotFoundError: If file doesn't exist
        """
        # Check negative cache first (issue #858)
        if self._negative_cache_check(path):
            raise NexusFileNotFoundError(path)

        try:
            result = await self._call_rpc("stat", {"path": path})
        except NexusFileNotFoundError:
            self._negative_cache_add(path)
            raise

        return result  # type: ignore[no-any-return]

    async def read_range(
        self,
        path: str,
        start: int,
        end: int,
        context: Any = None,  # noqa: ARG002
    ) -> bytes:
        """Read a specific byte range from a file (async).

        This method enables memory-efficient streaming by fetching file content
        in chunks without loading the entire file into memory.

        Args:
            path: Virtual path to read
            start: Start byte offset (inclusive, 0-indexed)
            end: End byte offset (exclusive)
            context: Unused in remote client

        Returns:
            bytes: Content from start to end (exclusive)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            ValueError: If start/end are invalid
        """
        result = await self._call_rpc(
            "read_range",
            {"path": path, "start": start, "end": end},
        )
        return self._decode_bytes_field(result)

    async def stream(
        self,
        path: str,
        chunk_size: int = 8192,
        context: Any = None,  # noqa: ARG002
    ) -> Any:
        """Stream file content in chunks using server-side range reads (async generator).

        This method fetches file content in chunks using read_range() RPC calls,
        avoiding loading the entire file into memory at once.

        Args:
            path: Virtual path to stream
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Unused in remote client

        Yields:
            bytes: Chunks of file content

        Examples:
            >>> async for chunk in nx.stream("/large/file.bin"):
            ...     process(chunk)
        """
        # Get file size using stat() - does NOT read file content
        info = await self.stat(path)
        file_size = info.get("size") or 0

        # Stream using read_range() calls
        offset = 0
        while offset < file_size:
            end = min(offset + chunk_size, file_size)
            chunk = await self.read_range(path, offset, end)
            if not chunk:
                break
            yield chunk
            offset += len(chunk)

    async def write_batch(
        self,
        files: builtins.list[tuple[str, bytes]],
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[dict[str, Any]]:
        """Write multiple files in a single transaction (async).

        Args:
            files: List of (path, content) tuples to write
            context: Unused in remote client

        Returns:
            List of metadata dicts for each file

        Examples:
            >>> files = [
            ...     ("/file1.txt", b"content1"),
            ...     ("/file2.txt", b"content2"),
            ... ]
            >>> results = await nx.write_batch(files)
        """
        result = await self._call_rpc(
            "write_batch",
            {"files": files},
        )
        return result  # type: ignore[no-any-return]

    async def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: Any = None,  # noqa: ARG002
    ) -> None:
        """Remove a directory (async).

        Args:
            path: Directory path to remove
            recursive: If True, remove directory and all contents
            context: Unused in remote client
        """
        await self._call_rpc("rmdir", {"path": path, "recursive": recursive})

    async def is_directory(
        self,
        path: str,
        context: Any = None,  # noqa: ARG002
    ) -> bool:
        """Check if path is a directory (async).

        Args:
            path: Path to check
            context: Unused in remote client

        Returns:
            True if path is a directory
        """
        result = await self._call_rpc("is_directory", {"path": path})
        return result["is_directory"]  # type: ignore[no-any-return]

    async def get_metadata(
        self,
        path: str,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Get file metadata (async).

        This method retrieves metadata without reading the entire file content.

        Args:
            path: Virtual file path
            context: Unused in remote client

        Returns:
            Metadata dict with keys: path, owner, group, mode, is_directory
            Returns None if file doesn't exist or server has no metadata

        Examples:
            >>> metadata = await nx.get_metadata("/workspace/file.txt")
            >>> print(f"Mode: {metadata['mode']:o}")  # e.g., 0o644
        """
        result = await self._call_rpc("get_metadata", {"path": path})
        return result.get("metadata")  # type: ignore[no-any-return]

    # ============================================================
    # Version Tracking Operations (Async)
    # ============================================================

    async def get_version(
        self,
        path: str,
        version: int,
        context: Any = None,  # noqa: ARG002
    ) -> bytes:
        """Get a specific version of a file (async).

        Args:
            path: Virtual file path
            version: Version number to retrieve
            context: Unused in remote client

        Returns:
            File content as bytes for the specified version
        """
        result = await self._call_rpc("get_version", {"path": path, "version": version})
        return result  # type: ignore[no-any-return]

    async def list_versions(
        self,
        path: str,
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[dict[str, Any]]:
        """List all versions of a file (async).

        Args:
            path: Virtual file path
            context: Unused in remote client

        Returns:
            List of version metadata dicts
        """
        result = await self._call_rpc("list_versions", {"path": path})
        return result  # type: ignore[no-any-return]

    async def rollback(
        self,
        path: str,
        version: int,
        context: Any = None,  # noqa: ARG002
    ) -> None:
        """Rollback file to a previous version (async).

        Args:
            path: Virtual file path
            version: Version number to rollback to
            context: Unused in remote client
        """
        await self._call_rpc("rollback", {"path": path, "version": version})

    async def diff_versions(
        self,
        path: str,
        v1: int,
        v2: int,
        mode: str = "metadata",
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any] | str:
        """Compare two versions of a file (async).

        Args:
            path: Virtual file path
            v1: First version number
            v2: Second version number
            mode: Diff mode - "metadata" or "content"
            context: Unused in remote client

        Returns:
            Diff result (dict for metadata, str for content)
        """
        result = await self._call_rpc(
            "diff_versions", {"path": path, "v1": v1, "v2": v2, "mode": mode}
        )
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Mount Operations (Async)
    # ============================================================

    async def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        context: Any = None,  # noqa: ARG002
    ) -> str:
        """Add a dynamic backend mount to the filesystem (async).

        Args:
            mount_point: Virtual path where backend is mounted (e.g., "/personal/alice")
            backend_type: Backend type - "local", "gcs", "google_drive", etc.
            backend_config: Backend-specific configuration dict
            priority: Mount priority - higher values take precedence (default: 0)
            readonly: Whether mount is read-only (default: False)
            context: Unused in remote client

        Returns:
            Mount ID (unique identifier for this mount)

        Examples:
            >>> mount_id = await nx.add_mount(
            ...     mount_point="/personal/alice",
            ...     backend_type="gcs",
            ...     backend_config={"bucket": "alice-bucket", "project_id": "my-project"},
            ...     priority=10
            ... )
        """
        result = await self._call_rpc(
            "add_mount",
            {
                "mount_point": mount_point,
                "backend_type": backend_type,
                "backend_config": backend_config,
                "priority": priority,
                "readonly": readonly,
            },
        )
        return result  # type: ignore[no-any-return]

    async def remove_mount(
        self,
        mount_point: str,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Remove a backend mount from the filesystem (async).

        Args:
            mount_point: Virtual path of mount to remove (e.g., "/personal/alice")
            context: Unused in remote client

        Returns:
            Dictionary with removal details:
            - removed: bool - Whether mount was removed
            - directory_deleted: bool - Whether mount point directory was deleted
            - permissions_cleaned: int - Number of permission tuples removed
            - errors: list[str] - Any errors encountered
        """
        result = await self._call_rpc("remove_mount", {"mount_point": mount_point})
        return result  # type: ignore[no-any-return]

    async def list_connectors(
        self,
        category: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all available connector types (async).

        Args:
            category: Optional filter by category (storage, api, oauth, database)

        Returns:
            List of connector info dictionaries
        """
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        result = await self._call_rpc("list_connectors", params)
        return result  # type: ignore[no-any-return]

    async def list_mounts(
        self,
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[dict[str, Any]]:
        """List all active backend mounts (async).

        Args:
            context: Unused in remote client

        Returns:
            List of mount info dictionaries, each containing:
                - mount_point: Virtual path (str)
                - priority: Mount priority (int)
                - readonly: Read-only flag (bool)
                - backend_type: Backend type name (str)
        """
        result = await self._call_rpc("list_mounts", {})
        return result  # type: ignore[no-any-return]

    async def sync_mount(
        self,
        mount_point: str | None = None,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: builtins.list[str] | None = None,
        exclude_patterns: builtins.list[str] | None = None,
        generate_embeddings: bool = False,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Sync metadata and content from connector backend(s) to Nexus database (async).

        Args:
            mount_point: Virtual path of mount to sync. If None, syncs ALL connector mounts.
            path: Specific path within mount to sync. If None, syncs entire mount.
            recursive: If True, sync all subdirectories recursively (default: True)
            dry_run: If True, only report what would be synced (default: False)
            sync_content: If True, also sync content to cache (default: True)
            include_patterns: Glob patterns to include (e.g., ["*.py", "*.md"])
            exclude_patterns: Glob patterns to exclude (e.g., ["*.pyc", ".git/*"])
            generate_embeddings: If True, generate embeddings for semantic search
            context: Unused in remote client

        Returns:
            Dictionary with sync results:
                - files_scanned: Number of files scanned in backend
                - files_created: Number of new files added to database
                - files_updated: Number of existing files updated
                - files_deleted: Number of files deleted from database
                - cache_synced: Number of files synced to content cache
                - cache_bytes: Total bytes synced to cache
                - embeddings_generated: Number of embeddings generated
                - errors: List of error messages (if any)
        """
        params: dict[str, Any] = {
            "recursive": recursive,
            "dry_run": dry_run,
            "sync_content": sync_content,
            "generate_embeddings": generate_embeddings,
        }

        if mount_point is not None:
            params["mount_point"] = mount_point

        if path is not None:
            params["path"] = path

        if include_patterns is not None:
            params["include_patterns"] = include_patterns

        if exclude_patterns is not None:
            params["exclude_patterns"] = exclude_patterns

        result = await self._call_rpc("sync_mount", params)
        return result  # type: ignore[no-any-return]

    async def sync_mount_async(
        self,
        mount_point: str,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: builtins.list[str] | None = None,
        exclude_patterns: builtins.list[str] | None = None,
        generate_embeddings: bool = False,
    ) -> dict[str, Any]:
        """Start an async sync job for a mount point (Issue #609).

        Unlike sync_mount() which blocks until completion, this method returns
        immediately with a job_id that can be used to monitor progress.

        Args:
            mount_point: Virtual path of mount to sync (required)
            path: Specific path within mount to sync
            recursive: If True, sync all subdirectories recursively
            dry_run: If True, only report what would be synced
            sync_content: If True, also sync content to cache
            include_patterns: Glob patterns to include
            exclude_patterns: Glob patterns to exclude
            generate_embeddings: If True, generate embeddings

        Returns:
            Dictionary with job info:
                - job_id: UUID of the sync job
                - status: Initial status ("pending")
                - mount_point: Mount being synced
        """
        params: dict[str, Any] = {
            "mount_point": mount_point,
            "recursive": recursive,
            "dry_run": dry_run,
            "sync_content": sync_content,
            "generate_embeddings": generate_embeddings,
        }

        if path is not None:
            params["path"] = path
        if include_patterns is not None:
            params["include_patterns"] = include_patterns
        if exclude_patterns is not None:
            params["exclude_patterns"] = exclude_patterns

        result = await self._call_rpc("sync_mount_async", params)
        return result  # type: ignore[no-any-return]

    async def get_sync_job(self, job_id: str) -> dict[str, Any] | None:
        """Get the status and progress of a sync job (async).

        Args:
            job_id: UUID of the sync job

        Returns:
            Job details dict or None if not found
        """
        result = await self._call_rpc("get_sync_job", {"job_id": job_id})
        return result  # type: ignore[no-any-return]

    async def cancel_sync_job(self, job_id: str) -> dict[str, Any]:
        """Cancel a running sync job (async).

        Args:
            job_id: UUID of the sync job to cancel

        Returns:
            Dictionary with result:
                - success: True if cancellation was requested
                - job_id: The job ID
                - message: Status message
        """
        result = await self._call_rpc("cancel_sync_job", {"job_id": job_id})
        return result  # type: ignore[no-any-return]

    async def list_sync_jobs(
        self,
        mount_point: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """List sync jobs with optional filters (async).

        Args:
            mount_point: Filter by mount point
            status: Filter by status (pending, running, completed, failed, cancelled)
            limit: Maximum number of jobs to return

        Returns:
            List of job dicts, ordered by created_at descending
        """
        params: dict[str, Any] = {"limit": limit}
        if mount_point is not None:
            params["mount_point"] = mount_point
        if status is not None:
            params["status"] = status

        result = await self._call_rpc("list_sync_jobs", params)
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Memory Registration (Async)
    # ============================================================

    async def register_memory(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        ttl: Any | None = None,
    ) -> dict[str, Any]:
        """Register a directory as a memory (async).

        Args:
            path: Absolute path to memory directory
            name: Optional friendly name for the memory
            description: Human-readable description
            created_by: User/agent who created it
            tags: Tags for categorization (reserved for future use)
            metadata: Additional user-defined metadata
            session_id: If provided, memory is session-scoped (temporary)
            ttl: Time-to-live for auto-expiry

        Returns:
            Memory configuration dict
        """
        _ = tags  # Reserved for future use

        result = await self._call_rpc(
            "register_memory",
            {
                "path": path,
                "name": name,
                "description": description,
                "created_by": created_by,
                "metadata": metadata,
                "session_id": session_id,
                "ttl": ttl,
            },
        )
        return result  # type: ignore[no-any-return]

    async def unregister_memory(self, path: str) -> bool:
        """Unregister a memory (does NOT delete files) (async).

        Args:
            path: Memory path to unregister

        Returns:
            True if unregistered, False if not found
        """
        result = await self._call_rpc("unregister_memory", {"path": path})
        return result  # type: ignore[no-any-return]

    async def list_registered_memories(self) -> builtins.list[dict[str, Any]]:
        """List all registered memory paths (async).

        Returns:
            List of memory configuration dicts
        """
        result = await self._call_rpc("list_registered_memories", {})
        return result  # type: ignore[no-any-return]

    async def get_memory_info(self, path: str) -> dict[str, Any] | None:
        """Get information about a registered memory (async).

        Args:
            path: Memory path

        Returns:
            Memory configuration dict or None if not found
        """
        result = await self._call_rpc("get_memory_info", {"path": path})
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Agent Management (Async)
    # ============================================================

    async def register_agent(
        self,
        agent_id: str,
        name: str,
        description: str | None = None,
        generate_api_key: bool = False,
    ) -> dict[str, Any]:
        """Register an AI agent (async).

        Args:
            agent_id: Unique agent identifier
            name: Human-readable name
            description: Optional description
            generate_api_key: If True, create API key for agent (not recommended)

        Returns:
            Agent info dict with agent_id, user_id, name, etc.
        """
        params: dict[str, Any] = {
            "agent_id": agent_id,
            "name": name,
            "description": description,
            "generate_api_key": generate_api_key,
        }
        result = await self._call_rpc("register_agent", params)
        return result  # type: ignore[no-any-return]

    async def update_agent(
        self,
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """Update an existing agent's configuration (async, v0.5.1).

        Updates the agent's config.yaml file and optionally updates entity registry metadata.
        Does NOT regenerate API keys or change permissions.

        Args:
            agent_id: Agent identifier to update
            name: Optional new name
            description: Optional new description
            metadata: Optional metadata to update (platform, endpoint_url, agent_id, etc.)

        Returns:
            Updated agent info dict

        Example:
            >>> # Update agent metadata
            >>> agent = await nx.update_agent(
            ...     "alice,DataAnalyst",
            ...     name="Data Analyst Pro",
            ...     description="Enhanced data analysis agent",
            ...     metadata={
            ...         "platform": "langgraph",
            ...         "endpoint_url": "https://agent.example.com",
            ...         "agent_id": "analyst"
            ...     }
            ... )
        """
        params: dict[str, Any] = {
            "agent_id": agent_id,
        }

        if name is not None:
            params["name"] = name
        if description is not None:
            params["description"] = description
        if metadata is not None:
            params["metadata"] = metadata

        result = await self._call_rpc("update_agent", params)
        return result  # type: ignore[no-any-return]

    async def list_agents(self) -> builtins.list[dict[str, Any]]:
        """List all registered agents (async).

        Returns:
            List of agent info dicts
        """
        result = await self._call_rpc("list_agents", {})
        return result  # type: ignore[no-any-return]

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        """Get agent information (async).

        Args:
            agent_id: Agent identifier

        Returns:
            Agent info dict or None if not found
        """
        result = await self._call_rpc("get_agent", {"agent_id": agent_id})
        return result  # type: ignore[no-any-return]

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete an agent (async).

        Args:
            agent_id: Agent identifier to delete

        Returns:
            True if deleted, False if not found
        """
        result = await self._call_rpc("delete_agent", {"agent_id": agent_id})
        return result  # type: ignore[no-any-return]

    # =========================================================================
    # Cross-Zone Sharing (Async)
    # =========================================================================

    async def share_with_user(
        self,
        resource: tuple[str, str],
        user_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        user_zone_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        """Share a resource with a specific user (same or different zone) - async.

        This enables cross-zone sharing - users from different organizations
        can be granted access to specific resources.

        Args:
            resource: Resource to share (e.g., ("file", "/path/to/doc.txt"))
            user_id: User to share with (e.g., "bob@partner-company.com")
            relation: Permission level - "viewer" (read) or "editor" (read/write)
            zone_id: Resource owner's zone ID (defaults to current zone)
            user_zone_id: Recipient user's zone ID (for cross-zone shares)
            expires_at: Optional expiration datetime for the share

        Returns:
            Share ID (tuple_id) that can be used to revoke the share

        Examples:
            >>> share_id = await nx.share_with_user(
            ...     resource=("file", "/project/doc.txt"),
            ...     user_id="bob@partner.com",
            ...     user_zone_id="partner-zone",
            ...     relation="viewer"
            ... )
        """
        result = await self._call_rpc(
            "share_with_user",
            {
                "resource": resource,
                "user_id": user_id,
                "relation": relation,
                "zone_id": zone_id,
                "user_zone_id": user_zone_id,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )
        return result  # type: ignore[no-any-return]

    async def revoke_share(self, resource: tuple[str, str], user_id: str) -> bool:
        """Revoke a share for a specific user on a resource (async).

        Args:
            resource: Resource to unshare (e.g., ("file", "/path/to/doc.txt"))
            user_id: User to revoke access from

        Returns:
            True if share was revoked, False if no share existed
        """
        result = await self._call_rpc(
            "revoke_share",
            {"resource": resource, "user_id": user_id},
        )
        return result  # type: ignore[no-any-return]

    async def revoke_share_by_id(self, share_id: str) -> bool:
        """Revoke a share using its ID (async).

        Args:
            share_id: The share ID returned by share_with_user()

        Returns:
            True if share was revoked, False if share didn't exist
        """
        result = await self._call_rpc("revoke_share_by_id", {"share_id": share_id})
        return result  # type: ignore[no-any-return]

    async def list_outgoing_shares(
        self,
        resource: tuple[str, str] | None = None,
        zone_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[dict[str, Any]]:
        """List shares created by the current zone (resources shared with others) - async.

        Args:
            resource: Filter by specific resource (optional)
            zone_id: Zone ID to list shares for (defaults to current zone)
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of share info dictionaries
        """
        result = await self._call_rpc(
            "list_outgoing_shares",
            {
                "resource": resource,
                "zone_id": zone_id,
                "limit": limit,
                "offset": offset,
            },
        )
        return result  # type: ignore[no-any-return]

    async def list_incoming_shares(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[dict[str, Any]]:
        """List shares received by a user (resources shared with me) - async.

        This includes cross-zone shares from other organizations.

        Args:
            user_id: User ID to list incoming shares for
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of share info dictionaries
        """
        result = await self._call_rpc(
            "list_incoming_shares",
            {"user_id": user_id, "limit": limit, "offset": offset},
        )
        return result  # type: ignore[no-any-return]

    # =========================================================================
    # Sandbox API (E2B cloud sandboxes)
    # =========================================================================

    async def sandbox_run(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run code in a sandbox (async).

        Args:
            sandbox_id: Sandbox ID
            language: Programming language ("python", "javascript", "bash")
            code: Code to execute
            timeout: Execution timeout in seconds (default: 300)
            nexus_url: Nexus server URL to inject into code as NEXUS_URL env var
            nexus_api_key: Nexus API key to inject into code as NEXUS_API_KEY env var
            context: Operation context

        Returns:
            Dict with stdout, stderr, exit_code, execution_time
        """
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "language": language,
            "code": code,
            "timeout": timeout,
        }
        if nexus_url is not None:
            params["nexus_url"] = nexus_url
        if nexus_api_key is not None:
            params["nexus_api_key"] = nexus_api_key
        if context is not None:
            params["context"] = context
        # Use execution timeout + 10 seconds buffer for HTTP read timeout
        read_timeout = timeout + 10
        result = await self._call_rpc("sandbox_run", params, read_timeout=read_timeout)
        return result  # type: ignore[no-any-return]

    async def sandbox_pause(
        self,
        sandbox_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pause sandbox to save costs (async).

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata
        """
        params: dict[str, Any] = {"sandbox_id": sandbox_id}
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("sandbox_pause", params)
        return result  # type: ignore[no-any-return]

    async def sandbox_resume(
        self,
        sandbox_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resume a paused sandbox (async).

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata
        """
        params: dict[str, Any] = {"sandbox_id": sandbox_id}
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("sandbox_resume", params)
        return result  # type: ignore[no-any-return]

    async def sandbox_stop(
        self,
        sandbox_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stop and destroy sandbox (async).

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata
        """
        params: dict[str, Any] = {"sandbox_id": sandbox_id}
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("sandbox_stop", params)
        return result  # type: ignore[no-any-return]

    async def sandbox_list(
        self,
        context: dict[str, Any] | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """List user's sandboxes (async).

        Args:
            context: Operation context
            verify_status: If True, verify status with provider
            user_id: Filter by user_id (admin only)
            zone_id: Filter by zone_id (admin only)
            agent_id: Filter by agent_id

        Returns:
            Dict with list of sandboxes
        """
        params: dict[str, Any] = {"verify_status": verify_status}
        if context is not None:
            params["context"] = context
        if user_id is not None:
            params["user_id"] = user_id
        if zone_id is not None:
            params["zone_id"] = zone_id
        if agent_id is not None:
            params["agent_id"] = agent_id
        result = await self._call_rpc("sandbox_list", params)
        return result  # type: ignore[no-any-return]

    async def sandbox_status(
        self,
        sandbox_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get sandbox status and metadata (async).

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Sandbox metadata dict
        """
        params: dict[str, Any] = {"sandbox_id": sandbox_id}
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("sandbox_status", params)
        return result  # type: ignore[no-any-return]

    async def sandbox_get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get existing sandbox or create new one (async).

        Args:
            name: Sandbox name (unique per user)
            ttl_minutes: Idle timeout in minutes (default: 10)
            provider: Sandbox provider
            template_id: Provider-specific template ID
            verify_status: If True, verify existing sandbox is active
            context: Operation context

        Returns:
            Sandbox metadata dict
        """
        params: dict[str, Any] = {
            "name": name,
            "ttl_minutes": ttl_minutes,
            "verify_status": verify_status,
        }
        if provider is not None:
            params["provider"] = provider
        if template_id is not None:
            params["template_id"] = template_id
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("sandbox_get_or_create", params)
        return result  # type: ignore[no-any-return]

    async def sandbox_connect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Connect and mount Nexus to a sandbox (async).

        Args:
            sandbox_id: External sandbox ID
            provider: Sandbox provider (default: "e2b")
            sandbox_api_key: Provider API key (uses E2B_API_KEY env if None)
            mount_path: Path to mount Nexus in sandbox
            nexus_url: Nexus server URL (uses client's URL if None)
            nexus_api_key: Nexus API key (uses client's key if None)
            agent_id: Agent ID for version attribution (issue #418).
                When set, file modifications will be attributed to this agent.
            context: Operation context

        Returns:
            Connection result with mount status
        """
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "provider": provider,
            "mount_path": mount_path,
        }
        if sandbox_api_key is not None:
            params["sandbox_api_key"] = sandbox_api_key

        # Use client's URL/key if not provided
        if nexus_url is None:
            nexus_url = self.server_url
        if nexus_api_key is None:
            nexus_api_key = self.api_key

        params["nexus_url"] = nexus_url
        params["nexus_api_key"] = nexus_api_key

        if agent_id is not None:
            params["agent_id"] = agent_id
        if context is not None:
            params["context"] = context
        # Mounting can take 10-30s (dependency checks, FUSE startup)
        result = await self._call_rpc("sandbox_connect", params, read_timeout=60)
        return result  # type: ignore[no-any-return]

    async def sandbox_disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Disconnect and unmount Nexus from a sandbox (async).

        Args:
            sandbox_id: External sandbox ID
            provider: Sandbox provider (default: "e2b")
            sandbox_api_key: Provider API key
            context: Operation context

        Returns:
            Disconnection result
        """
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "provider": provider,
        }
        if sandbox_api_key is not None:
            params["sandbox_api_key"] = sandbox_api_key
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("sandbox_disconnect", params)
        return result  # type: ignore[no-any-return]

    # ============================================================
    # OAuth Operations
    # ============================================================

    async def oauth_list_providers(self) -> builtins.list[dict[str, Any]]:
        """List available OAuth providers (async)."""
        result = await self._call_rpc("oauth_list_providers", {})
        return result.get("providers", [])  # type: ignore[no-any-return]

    async def oauth_get_auth_url(
        self,
        provider: str,
        redirect_uri: str,
        scopes: builtins.list[str] | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Get OAuth authorization URL (async)."""
        params: dict[str, Any] = {
            "provider": provider,
            "redirect_uri": redirect_uri,
        }
        if scopes is not None:
            params["scopes"] = scopes
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("oauth_get_auth_url", params)
        return result  # type: ignore[no-any-return]

    async def oauth_exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str,
        redirect_uri: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Exchange OAuth code for tokens (async)."""
        params: dict[str, Any] = {
            "provider": provider,
            "code": code,
            "user_email": user_email,
            "redirect_uri": redirect_uri,
        }
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("oauth_exchange_code", params)
        return result  # type: ignore[no-any-return]

    async def oauth_list_credentials(
        self,
        provider: str | None = None,
        user_email: str | None = None,
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        """List OAuth credentials (async)."""
        params: dict[str, Any] = {}
        if provider is not None:
            params["provider"] = provider
        if user_email is not None:
            params["user_email"] = user_email
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("oauth_list_credentials", params)
        return result.get("credentials", [])  # type: ignore[no-any-return]

    async def oauth_revoke_credential(
        self,
        provider: str,
        user_email: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Revoke OAuth credential (async)."""
        params: dict[str, Any] = {
            "provider": provider,
            "user_email": user_email,
        }
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("oauth_revoke_credential", params)
        return result  # type: ignore[no-any-return]

    async def oauth_test_credential(
        self,
        provider: str,
        user_email: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Test if an OAuth credential is valid (async)."""
        params: dict[str, Any] = {
            "provider": provider,
            "user_email": user_email,
        }
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("oauth_test_credential", params)
        return result  # type: ignore[no-any-return]

    # ============================================================
    # ReBAC Permission Checking
    # ============================================================

    async def rebac_check_batch(
        self,
        checks: builtins.list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> builtins.list[bool]:
        """Batch permission checks for efficiency (async).

        Performs multiple permission checks in a single call, using Rust-accelerated
        bulk checking with shared memoization cache. More efficient than individual
        checks when checking multiple permissions.

        Args:
            checks: List of (subject, permission, object) tuples to check
                - subject: (type, id) tuple e.g. ("agent", "alice")
                - permission: permission name e.g. "read", "write"
                - object: (type, id) tuple e.g. ("file", "/workspace/doc.txt")

        Returns:
            List of boolean results in the same order as input

        Examples:
            >>> # Check multiple permissions at once
            >>> results = await nx.rebac_check_batch([
            ...     (("agent", "alice"), "read", ("file", "/workspace/doc1.txt")),
            ...     (("agent", "alice"), "read", ("file", "/workspace/doc2.txt")),
            ...     (("agent", "bob"), "write", ("file", "/workspace/doc3.txt")),
            ... ])
            >>> # Returns: [True, False, True]
            >>>
            >>> # Efficient bulk check for same user on many files
            >>> checks = [
            ...     (("agent", "alice"), "read", ("file", f"/workspace/file{i}.txt"))
            ...     for i in range(100)
            ... ]
            >>> results = await nx.rebac_check_batch(checks)
        """
        result = await self._call_rpc("rebac_check_batch", {"checks": checks})
        return result  # type: ignore[no-any-return]

    # ============================================================
    # MCP/Klavis Integration
    # ============================================================

    async def mcp_connect(
        self,
        provider: str,
        redirect_url: str | None = None,
        user_email: str | None = None,
        reuse_nexus_token: bool = True,
        context: Any = None,
    ) -> dict[str, Any]:
        """Connect to a Klavis MCP server with OAuth support (async).

        This method creates a Klavis MCP instance, handles OAuth if needed,
        discovers tools, and generates SKILL.md in the user's folder.

        Args:
            provider: MCP provider name (e.g., "google_drive", "gmail", "slack")
            redirect_url: OAuth redirect URL (required if OAuth needed)
            user_email: User email for OAuth (optional, uses context if not provided)
            reuse_nexus_token: If True, try to reuse existing Nexus OAuth token
            context: Operation context (optional)

        Returns:
            Dictionary containing:
                - status: "connected" | "oauth_required" | "error"
                - instance_id: Klavis instance ID (if created)
                - oauth_url: OAuth URL (if OAuth required)
                - tools: List of available tools (if connected)
                - skill_path: Path to generated SKILL.md
                - error: Error message (if error)
        """
        params: dict[str, Any] = {
            "provider": provider,
            "reuse_nexus_token": reuse_nexus_token,
        }
        if redirect_url is not None:
            params["redirect_url"] = redirect_url
        if user_email is not None:
            params["user_email"] = user_email
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("mcp_connect", params)
        return result  # type: ignore[no-any-return]

    async def mcp_get_oauth_url(
        self,
        provider: str,
        redirect_url: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Get OAuth URL for a Klavis MCP provider (async).

        Args:
            provider: MCP provider name (e.g., "google_drive", "gmail")
            redirect_url: OAuth callback URL
            context: Operation context (optional)

        Returns:
            Dictionary containing:
                - oauth_url: URL to redirect user for OAuth
                - instance_id: Klavis instance ID for tracking
        """
        params: dict[str, Any] = {
            "provider": provider,
            "redirect_url": redirect_url,
        }
        if context is not None:
            params["context"] = context
        result = await self._call_rpc("mcp_get_oauth_url", params)
        return result  # type: ignore[no-any-return]

    async def mcp_list_mounts(
        self,
        tier: str | None = None,
        include_unmounted: bool = True,
    ) -> builtins.list[dict[str, Any]]:
        """List MCP server mounts.

        Args:
            tier: Filter by tier (user/zone/system)
            include_unmounted: Include unmounted configurations (default: True)

        Returns:
            List of MCP mount info dicts
        """
        params: dict[str, Any] = {"include_unmounted": include_unmounted}
        if tier is not None:
            params["tier"] = tier
        result = await self._call_rpc("mcp_list_mounts", params)
        return result  # type: ignore[no-any-return]

    async def mcp_list_tools(self, name: str) -> builtins.list[dict[str, Any]]:
        """List tools from a specific MCP mount.

        Args:
            name: MCP mount name

        Returns:
            List of tool info dicts
        """
        result = await self._call_rpc("mcp_list_tools", {"name": name})
        return result  # type: ignore[no-any-return]

    async def mcp_mount(
        self,
        name: str,
        transport: str | None = None,
        command: str | None = None,
        url: str | None = None,
        args: builtins.list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        description: str | None = None,
        tier: str = "system",
    ) -> dict[str, Any]:
        """Mount an MCP server.

        Args:
            name: Mount name
            transport: Transport type (stdio/sse/klavis)
            command: Command for stdio transport
            url: URL for sse transport
            args: Command arguments
            env: Environment variables
            headers: HTTP headers
            description: Mount description
            tier: Target tier

        Returns:
            Dict with mount info
        """
        params: dict[str, Any] = {"name": name, "tier": tier}
        if transport is not None:
            params["transport"] = transport
        if command is not None:
            params["command"] = command
        if url is not None:
            params["url"] = url
        if args is not None:
            params["args"] = args
        if env is not None:
            params["env"] = env
        if headers is not None:
            params["headers"] = headers
        if description is not None:
            params["description"] = description
        result = await self._call_rpc("mcp_mount", params)
        return result  # type: ignore[no-any-return]

    async def mcp_unmount(self, name: str) -> dict[str, Any]:
        """Unmount an MCP server.

        Args:
            name: MCP mount name

        Returns:
            Dict with success status
        """
        result = await self._call_rpc("mcp_unmount", {"name": name})
        return result  # type: ignore[no-any-return]

    async def mcp_sync(self, name: str) -> dict[str, Any]:
        """Sync tools from an MCP server.

        Args:
            name: MCP mount name

        Returns:
            Dict with tool count
        """
        result = await self._call_rpc("mcp_sync", {"name": name})
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Event Operations (Issue #1106 Block 2)
    # ============================================================

    async def wait_for_changes(
        self,
        path: str,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        """Wait for file system changes on a path.

        Long-polling RPC call that waits for changes to occur on the specified
        path. Uses distributed event bus (Redis Pub/Sub) on the server side.

        Args:
            path: Virtual path to watch (file or directory with trailing /)
            timeout: Maximum time to wait in seconds (default: 30.0)

        Returns:
            Dict with change info if change detected:
                - type: "file_write", "file_delete", "file_rename", etc.
                - path: Path that changed
                - old_path: Previous path (for rename events only)
            None if timeout reached

        Example:
            >>> # Watch for new files in inbox
            >>> change = await client.wait_for_changes("/inbox/", timeout=60)
            >>> if change:
            ...     print(f"Detected {change['type']} on {change['path']}")
        """
        result = await self._call_rpc(
            "wait_for_changes",
            {"path": path, "timeout": timeout},
            read_timeout=timeout + 5.0,  # Buffer for network latency
        )
        return result if result else None

    async def lock(
        self,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0,
    ) -> str | None:
        """Acquire a distributed lock on a path.

        Uses Redis-based distributed locking on the server side.
        For long-running operations, use extend_lock() to keep the lock alive.

        Args:
            path: Virtual path to lock
            timeout: Maximum time to wait for lock in seconds (default: 30.0)
            ttl: Lock TTL in seconds - auto-expires after this (default: 30.0)

        Returns:
            Lock ID if acquired (use this to unlock/extend later)
            None if timeout reached

        Example:
            >>> lock_id = await client.lock("/shared/config.json", timeout=5.0)
            >>> if lock_id:
            ...     try:
            ...         content = await client.read("/shared/config.json")
            ...         await client.write("/shared/config.json", modified_content)
            ...     finally:
            ...         await client.unlock(lock_id, "/shared/config.json")
        """
        result = await self._call_rpc(
            "lock",
            {"path": path, "timeout": timeout, "ttl": ttl},
            read_timeout=timeout + 5.0,
        )
        return result.get("lock_id") if result else None

    async def extend_lock(
        self,
        lock_id: str,
        path: str,
        ttl: float = 30.0,
    ) -> bool:
        """Extend a lock's TTL (heartbeat for long-running operations).

        Use this to keep distributed locks alive during long operations.
        Call periodically (e.g., every TTL/2 seconds) to prevent lock expiry.

        Args:
            lock_id: Lock ID returned from lock()
            path: Path that was locked
            ttl: New TTL in seconds (default: 30.0)

        Returns:
            True if lock was extended
            False if lock was not found or not owned

        Example:
            >>> # Heartbeat pattern for long operations
            >>> async def heartbeat():
            ...     while working:
            ...         success = await client.extend_lock(lock_id, "/meeting/floor")
            ...         if not success:
            ...             raise RuntimeError("Lost lock!")
            ...         await asyncio.sleep(15)  # Extend every 15s for 30s TTL
        """
        result = await self._call_rpc(
            "extend_lock",
            {"lock_id": lock_id, "path": path, "ttl": ttl},
        )
        return bool(result.get("extended", False)) if result else False

    async def unlock(
        self,
        lock_id: str,
        path: str,
    ) -> bool:
        """Release a distributed lock.

        Args:
            lock_id: Lock ID returned from lock()
            path: Path that was locked

        Returns:
            True if lock was released
            False if lock_id was not found or not owned

        Example:
            >>> lock_id = await client.lock("/shared/config.json")
            >>> # ... do work ...
            >>> success = await client.unlock(lock_id, "/shared/config.json")
        """
        result = await self._call_rpc(
            "unlock",
            {"lock_id": lock_id, "path": path},
        )
        return bool(result.get("released", False)) if result else False


class AsyncRemoteMemory:
    """Async Remote Memory API client.

    Provides the same interface as core.memory_api.Memory but makes async RPC calls
    to a remote Nexus server instead of direct database access.

    Example:
        >>> nx = AsyncRemoteNexusFS("http://localhost:2026", api_key="sk-xxx")
        >>> memory = AsyncRemoteMemory(nx)
        >>> memory_id = await memory.store("User prefers dark mode", memory_type="preference")
        >>> memories = await memory.search("dark mode")
    """

    def __init__(self, remote_fs: AsyncRemoteNexusFS):
        """Initialize async remote memory client.

        Args:
            remote_fs: AsyncRemoteNexusFS instance to use for RPC calls
        """
        self.remote_fs = remote_fs

    async def store(
        self,
        content: str,
        memory_type: str = "fact",
        scope: str = "agent",
        importance: float = 0.5,
        namespace: str | None = None,
        path_key: str | None = None,
        state: str = "active",
        tags: builtins.list[str] | None = None,
    ) -> str:
        """Store a memory (async).

        Args:
            content: Memory content
            memory_type: Type of memory
            scope: Memory scope
            importance: Importance score
            namespace: Hierarchical namespace
            path_key: Optional key for upsert mode
            state: Memory state ('inactive', 'active')
            tags: Optional tags

        Returns:
            memory_id: ID of stored memory
        """
        params: dict[str, Any] = {
            "content": content,
            "memory_type": memory_type,
            "scope": scope,
            "importance": importance,
        }
        if namespace is not None:
            params["namespace"] = namespace
        if path_key is not None:
            params["path_key"] = path_key
        if state != "active":
            params["state"] = state
        if tags is not None:
            params["tags"] = tags
        result = await self.remote_fs._call_rpc("store_memory", params)
        return result["memory_id"]  # type: ignore[no-any-return]

    async def list(
        self,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,
        namespace_prefix: str | None = None,
        state: str | None = "active",
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """List memories (async).

        Args:
            scope: Filter by scope
            memory_type: Filter by type
            namespace: Filter by exact namespace
            namespace_prefix: Filter by namespace prefix
            state: Filter by state ('inactive', 'active', 'all')
            limit: Maximum results

        Returns:
            List of memories
        """
        params: dict[str, Any] = {"limit": limit}
        if scope is not None:
            params["scope"] = scope
        if namespace is not None:
            params["namespace"] = namespace
        if namespace_prefix is not None:
            params["namespace_prefix"] = namespace_prefix
        if memory_type is not None:
            params["memory_type"] = memory_type
        if state is not None:
            params["state"] = state
        result = await self.remote_fs._call_rpc("list_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    async def retrieve(
        self,
        namespace: str | None = None,
        path_key: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a memory by namespace path (async).

        Args:
            namespace: Memory namespace
            path_key: Path key within namespace
            path: Combined path (alternative to namespace+path_key)

        Returns:
            Memory dict or None if not found
        """
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path
        else:
            if namespace is not None:
                params["namespace"] = namespace
            if path_key is not None:
                params["path_key"] = path_key
        result = await self.remote_fs._call_rpc("retrieve_memory", params)
        return result.get("memory")  # type: ignore[no-any-return]

    async def query(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        state: str | None = "active",
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """Query memories (async).

        Args:
            memory_type: Filter by type
            scope: Filter by scope
            state: Filter by state ('inactive', 'active', 'all')
            limit: Maximum results

        Returns:
            List of matching memories
        """
        params: dict[str, Any] = {"limit": limit}
        if memory_type is not None:
            params["memory_type"] = memory_type
        if scope is not None:
            params["scope"] = scope
        if state is not None:
            params["state"] = state
        result = await self.remote_fs._call_rpc("query_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    async def search(
        self,
        query: str,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
        search_mode: str = "hybrid",
        embedding_provider: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        """Semantic search over memories (async).

        Args:
            query: Natural language search query
            scope: Filter by scope
            memory_type: Filter by type
            limit: Maximum results
            search_mode: Search mode - "semantic", "keyword", or "hybrid"
            embedding_provider: Embedding provider name

        Returns:
            List of matching memories with relevance scores

        Example:
            >>> results = await memory.search("authentication flow")
            >>> for mem in results:
            ...     print(f"Score: {mem['score']:.2f} - {mem['content'][:50]}...")
        """
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
        }
        if memory_type is not None:
            params["memory_type"] = memory_type
        if scope is not None:
            params["scope"] = scope
        if search_mode != "hybrid":
            params["search_mode"] = search_mode
        if embedding_provider is not None:
            if hasattr(embedding_provider, "__class__"):
                provider_name = embedding_provider.__class__.__name__.lower()
                if "openrouter" in provider_name:
                    params["embedding_provider"] = "openrouter"
                elif "openai" in provider_name:
                    params["embedding_provider"] = "openai"
                elif "voyage" in provider_name:
                    params["embedding_provider"] = "voyage"
            elif isinstance(embedding_provider, str):
                params["embedding_provider"] = embedding_provider

        result = await self.remote_fs._call_rpc("query_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory (async).

        Args:
            memory_id: Memory ID to delete

        Returns:
            True if deleted, False if not found or no permission
        """
        params: dict[str, Any] = {"memory_id": memory_id}
        result = await self.remote_fs._call_rpc("delete_memory", params)
        return result["deleted"]  # type: ignore[no-any-return]


# ============================================================
# Admin API (Async)
# ============================================================


class AsyncAdminAPI:
    """Async Admin API client for managing API keys.

    Requires admin privileges. All methods will fail if the API key
    used to create AsyncRemoteNexusFS is not an admin key.

    Example:
        >>> nx = AsyncRemoteNexusFS("http://localhost:2026", api_key="sk-admin-xxx")
        >>> admin = AsyncAdminAPI(nx)
        >>> result = await admin.create_key(user_id="alice", name="Alice's key")
        >>> print(f"Created key: {result['api_key']}")
    """

    def __init__(self, remote_fs: AsyncRemoteNexusFS):
        """Initialize async admin API client.

        Args:
            remote_fs: AsyncRemoteNexusFS instance to use for RPC calls
        """
        self.remote_fs = remote_fs

    async def create_key(
        self,
        user_id: str,
        name: str,
        zone_id: str = "default",
        is_admin: bool = False,
        expires_days: int | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new API key (async, admin only).

        Args:
            user_id: User ID for the key
            name: Human-readable name for the key
            zone_id: Zone ID (default: "default")
            is_admin: Whether this key has admin privileges
            expires_days: Days until expiry (None = never expires)
            subject_type: Subject type for the key (e.g., "user", "agent")
            subject_id: Subject ID (defaults to user_id)

        Returns:
            Dict with key_id, api_key (raw key - save this!), user_id, name, etc.
        """
        params: dict[str, Any] = {
            "user_id": user_id,
            "name": name,
            "zone_id": zone_id,
            "is_admin": is_admin,
        }
        if expires_days is not None:
            params["expires_days"] = expires_days
        if subject_type is not None:
            params["subject_type"] = subject_type
        if subject_id is not None:
            params["subject_id"] = subject_id

        result = await self.remote_fs._call_rpc("admin_create_key", params)
        return result  # type: ignore[no-any-return]

    async def list_keys(
        self,
        user_id: str | None = None,
        zone_id: str | None = None,
        is_admin: bool | None = None,
        include_expired: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        """List API keys (async, admin only).

        Args:
            user_id: Filter by user ID
            zone_id: Filter by zone ID
            is_admin: Filter by admin status
            include_expired: Include expired keys

        Returns:
            List of key info dicts (without raw key values)
        """
        params: dict[str, Any] = {"include_expired": include_expired}
        if user_id is not None:
            params["user_id"] = user_id
        if zone_id is not None:
            params["zone_id"] = zone_id
        if is_admin is not None:
            params["is_admin"] = is_admin

        result = await self.remote_fs._call_rpc("admin_list_keys", params)
        return result["keys"]  # type: ignore[no-any-return]

    async def get_key(self, key_id: str) -> dict[str, Any] | None:
        """Get API key info (async, admin only).

        Args:
            key_id: Key ID to look up

        Returns:
            Key info dict or None if not found
        """
        result = await self.remote_fs._call_rpc("admin_get_key", {"key_id": key_id})
        return result.get("key")  # type: ignore[no-any-return]

    async def revoke_key(self, key_id: str) -> bool:
        """Revoke an API key (async, admin only).

        Args:
            key_id: Key ID to revoke

        Returns:
            True if revoked, False if not found
        """
        result = await self.remote_fs._call_rpc("admin_revoke_key", {"key_id": key_id})
        return result.get("success", False)  # type: ignore[no-any-return]

    async def update_key(
        self,
        key_id: str,
        name: str | None = None,
        expires_days: int | None = None,
    ) -> dict[str, Any]:
        """Update an API key (async, admin only).

        Args:
            key_id: Key ID to update
            name: New name (optional)
            expires_days: New expiry in days from now (optional)

        Returns:
            Updated key info dict
        """
        params: dict[str, Any] = {"key_id": key_id}
        if name is not None:
            params["name"] = name
        if expires_days is not None:
            params["expires_days"] = expires_days

        result = await self.remote_fs._call_rpc("admin_update_key", params)
        return result  # type: ignore[no-any-return]


# ============================================================
# ACE (Adaptive Concurrency Engine) API (Async)
# ============================================================


class AsyncACE:
    """Async ACE (Adaptive Concurrency Engine) API client.

    Provides trajectory tracking and playbook management for agent learning.

    Example:
        >>> nx = AsyncRemoteNexusFS("http://localhost:2026", api_key="sk-xxx")
        >>> ace = AsyncACE(nx)
        >>> # Start tracking a task
        >>> traj = await ace.start_trajectory("Process customer data")
        >>> await ace.log_step(traj["trajectory_id"], "action", "Loaded 1000 records")
        >>> await ace.complete_trajectory(traj["trajectory_id"], "success", success_score=0.95)
    """

    def __init__(self, remote_fs: AsyncRemoteNexusFS):
        """Initialize async ACE client.

        Args:
            remote_fs: AsyncRemoteNexusFS instance to use for RPC calls
        """
        self.remote_fs = remote_fs

    async def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        """Start tracking a new execution trajectory (async).

        Args:
            task_description: Description of the task being executed
            task_type: Optional task type ('api_call', 'data_processing', etc.)

        Returns:
            Dict with trajectory_id
        """
        params: dict[str, Any] = {"task_description": task_description}
        if task_type is not None:
            params["task_type"] = task_type
        result = await self.remote_fs._call_rpc("ace_start_trajectory", params)
        return result  # type: ignore[no-any-return]

    async def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
    ) -> dict[str, Any]:
        """Log a step in an execution trajectory (async).

        Args:
            trajectory_id: Trajectory ID
            step_type: Type of step ('action', 'decision', 'observation')
            description: Step description
            result: Optional result data

        Returns:
            Success status
        """
        params: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "step_type": step_type,
            "description": description,
        }
        if result is not None:
            params["result"] = result
        result_data = await self.remote_fs._call_rpc("ace_log_trajectory_step", params)
        return result_data  # type: ignore[no-any-return]

    async def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Complete a trajectory with outcome (async).

        Args:
            trajectory_id: Trajectory ID
            status: Status ('success', 'failure', 'partial')
            success_score: Success score (0.0-1.0)
            error_message: Error message if failed

        Returns:
            Dict with trajectory_id
        """
        params: dict[str, Any] = {"trajectory_id": trajectory_id, "status": status}
        if success_score is not None:
            params["success_score"] = success_score
        if error_message is not None:
            params["error_message"] = error_message
        result = await self.remote_fs._call_rpc("ace_complete_trajectory", params)
        return result  # type: ignore[no-any-return]

    async def add_feedback(
        self,
        trajectory_id: str,
        feedback_type: str,
        score: float | None = None,
        source: str | None = None,
        message: str | None = None,
        metrics: dict | None = None,
    ) -> dict[str, Any]:
        """Add feedback to a completed trajectory (async).

        Args:
            trajectory_id: Trajectory ID
            feedback_type: Type of feedback
            score: Revised score (0.0-1.0)
            source: Feedback source
            message: Human-readable message
            metrics: Additional metrics

        Returns:
            Dict with feedback_id
        """
        params: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "feedback_type": feedback_type,
        }
        if score is not None:
            params["score"] = score
        if source is not None:
            params["source"] = source
        if message is not None:
            params["message"] = message
        if metrics is not None:
            params["metrics"] = metrics
        result = await self.remote_fs._call_rpc("ace_add_feedback", params)
        return result  # type: ignore[no-any-return]

    async def get_trajectory_feedback(
        self,
        trajectory_id: str,
    ) -> builtins.list[dict[str, Any]]:
        """Get all feedback for a trajectory (async).

        Args:
            trajectory_id: Trajectory ID

        Returns:
            List of feedback dicts
        """
        result = await self.remote_fs._call_rpc(
            "ace_get_trajectory_feedback", {"trajectory_id": trajectory_id}
        )
        return result  # type: ignore[no-any-return]

    async def get_effective_score(
        self,
        trajectory_id: str,
        strategy: str = "latest",
    ) -> dict[str, Any]:
        """Get effective score for a trajectory (async).

        Args:
            trajectory_id: Trajectory ID
            strategy: Scoring strategy ('latest', 'average', 'weighted')

        Returns:
            Dict with effective_score
        """
        result = await self.remote_fs._call_rpc(
            "ace_get_effective_score",
            {"trajectory_id": trajectory_id, "strategy": strategy},
        )
        return result  # type: ignore[no-any-return]

    async def mark_for_relearning(
        self,
        trajectory_id: str,
        reason: str,
        priority: int = 5,
    ) -> dict[str, Any]:
        """Mark trajectory for re-learning (async).

        Args:
            trajectory_id: Trajectory ID
            reason: Reason for re-learning
            priority: Priority (1-10)

        Returns:
            Success status
        """
        result = await self.remote_fs._call_rpc(
            "ace_mark_for_relearning",
            {"trajectory_id": trajectory_id, "reason": reason, "priority": priority},
        )
        return result  # type: ignore[no-any-return]

    async def query_trajectories(
        self,
        task_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """Query execution trajectories (async).

        Args:
            task_type: Filter by task type
            status: Filter by status
            limit: Maximum results

        Returns:
            List of trajectory summaries
        """
        params: dict[str, Any] = {"limit": limit}
        if task_type is not None:
            params["task_type"] = task_type
        if status is not None:
            params["status"] = status
        result = await self.remote_fs._call_rpc("ace_query_trajectories", params)
        return result  # type: ignore[no-any-return]

    async def create_playbook(
        self,
        name: str,
        description: str | None = None,
        scope: str = "agent",
    ) -> dict[str, Any]:
        """Create a new playbook (async).

        Args:
            name: Playbook name
            description: Optional description
            scope: Scope level ('agent', 'user', 'zone', 'global')

        Returns:
            Dict with playbook_id
        """
        params: dict[str, Any] = {"name": name, "scope": scope}
        if description is not None:
            params["description"] = description
        result = await self.remote_fs._call_rpc("ace_create_playbook", params)
        return result  # type: ignore[no-any-return]

    async def get_playbook(self, playbook_id: str) -> dict[str, Any] | None:
        """Get playbook details (async).

        Args:
            playbook_id: Playbook ID

        Returns:
            Playbook dict or None
        """
        result = await self.remote_fs._call_rpc("ace_get_playbook", {"playbook_id": playbook_id})
        return result  # type: ignore[no-any-return]

    async def query_playbooks(
        self,
        scope: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """Query playbooks (async).

        Args:
            scope: Filter by scope
            limit: Maximum results

        Returns:
            List of playbook summaries
        """
        params: dict[str, Any] = {"limit": limit}
        if scope is not None:
            params["scope"] = scope
        result = await self.remote_fs._call_rpc("ace_query_playbooks", params)
        return result  # type: ignore[no-any-return]
