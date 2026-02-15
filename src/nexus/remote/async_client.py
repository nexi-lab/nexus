"""Async Remote Nexus filesystem client.

Implements NexusFilesystem asynchronously by proxying RPC calls to a Nexus
server over HTTP using httpx.AsyncClient. Uses __getattr__-based dispatch
for trivial methods, with explicit async overrides for complex methods.

Issue #1289: Protocol + RPC Proxy pattern.

Example:
    async with AsyncRemoteNexusFS("http://localhost:2026", api_key="sk-xxx") as nx:
        content = await nx.read("/workspace/file.txt")
        contents = await asyncio.gather(*[nx.read(p) for p in paths])
"""

from __future__ import annotations

import builtins
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast
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
from nexus.core.filesystem import NexusFilesystem
from nexus.remote.base_client import BaseRemoteNexusFS
from nexus.remote.rpc_proxy import RPCProxyBase

# TYPE_CHECKING trick: mypy sees NexusFilesystem in MRO for type compatibility,
# but at runtime we use virtual subclass registration so abstract methods don't
# shadow __getattr__-based dispatch.
if TYPE_CHECKING:
    _AsyncNexusFSBase = NexusFilesystem
else:
    _AsyncNexusFSBase = object
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

logger = logging.getLogger(__name__)


class AsyncRemoteNexusFS(RPCProxyBase, BaseRemoteNexusFS, _AsyncNexusFSBase):
    """Async remote Nexus filesystem client.

    Uses httpx.AsyncClient for non-blocking HTTP calls. Trivial methods
    are auto-dispatched via __getattr__; complex methods are async overrides.
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
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.connect_timeout = connect_timeout

        self._zone_id: str | None = None
        self._agent_id: str | None = None

        # Pre-build default timeout config
        self._default_timeout = httpx.Timeout(
            connect=connect_timeout,
            read=timeout,
            write=timeout,
            pool=timeout,
        )

        limits = httpx.Limits(
            max_connections=pool_maxsize,
            max_keepalive_connections=pool_connections,
        )

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self._client = httpx.AsyncClient(
            limits=limits,
            timeout=self._default_timeout,
            headers=headers,
            http2=True,
        )

        self._initialized = False

        self._negative_cache_capacity = negative_cache_capacity
        self._negative_cache_fp_rate = negative_cache_fp_rate
        self._negative_bloom: Any = None
        self._init_negative_cache()

    async def __aenter__(self) -> AsyncRemoteNexusFS:
        await self._ensure_initialized()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _ensure_initialized(self) -> None:
        if not self._initialized and self.api_key:
            try:
                await self._fetch_auth_info()
            except Exception as e:
                logger.warning(f"Failed to fetch auth info: {e}")
            self._initialized = True

    async def _fetch_auth_info(self) -> None:
        try:
            response = await self._client.get(
                urljoin(self.server_url, "/api/auth/whoami"),
                timeout=self.connect_timeout,
            )
            if response.status_code == 200:
                self._parse_auth_info(response.json())
            else:
                logger.warning(f"Failed to fetch auth info: HTTP {response.status_code}")
        except Exception as e:
            logger.debug(f"Could not fetch auth info: {e}")
            raise

    # ============================================================
    # RPC Transport (async)
    # ============================================================

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(
            (httpx.ConnectError, httpx.TimeoutException, RemoteConnectionError)
        ),
        reraise=True,
    )
    async def _call_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        """Make async RPC call to server with automatic retry logic."""
        await self._ensure_initialized()

        request = RPCRequest(
            jsonrpc="2.0",
            id=str(uuid.uuid4()),
            method=method,
            params=params,
        )
        body = encode_rpc_message(request.to_dict())
        url = urljoin(self.server_url, f"/api/nfs/{method}")

        start_time = time.time()
        logger.debug(f"API call: {method} with params: {params}")

        try:
            headers: dict[str, str] = {
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
            }
            if self._agent_id:
                headers["X-Agent-ID"] = self._agent_id
            if self._zone_id:
                headers["X-Nexus-Zone-ID"] = self._zone_id

            if read_timeout is not None:
                request_timeout = httpx.Timeout(
                    connect=self.connect_timeout,
                    read=read_timeout,
                    write=read_timeout,
                    pool=read_timeout,
                )
            else:
                request_timeout = self._default_timeout

            response = await self._client.post(
                url,
                content=body,
                headers=headers,
                timeout=request_timeout,
            )
            elapsed = time.time() - start_time

            if response.status_code != 200:
                logger.error(
                    f"API call failed: {method} - HTTP {response.status_code} ({elapsed:.3f}s)"
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
                    f"API call RPC error: {method} - "
                    f"{rpc_response.error.get('message')} ({elapsed:.3f}s)"
                )
                self._handle_rpc_error(rpc_response.error)

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
    # __getattr__ override for async dispatch
    # ============================================================

    def __getattr__(self, name: str) -> Any:
        """Dynamic dispatch — wraps sync proxy methods as async coroutines."""
        from nexus.remote.method_registry import METHOD_REGISTRY
        from nexus.remote.rpc_proxy import _INTERNAL_ATTRS

        if name.startswith("_") or name in _INTERNAL_ATTRS:
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

        spec = METHOD_REGISTRY.get(name)

        if spec is not None and spec.deprecated_message is not None:

            async def _deprecated(*_args: Any, **_kwargs: Any) -> None:
                raise NotImplementedError(spec.deprecated_message)

            return _deprecated

        async def _async_proxy(*args: Any, **kwargs: Any) -> Any:
            return await self._dispatch_rpc(name, spec, args, kwargs)

        _async_proxy.__name__ = name
        _async_proxy.__qualname__ = f"{type(self).__name__}.{name}"
        return _async_proxy

    async def _dispatch_rpc(
        self,
        name: str,
        spec: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Async dispatch — awaits _call_rpc and applies response_key."""
        from nexus.remote.method_registry import MethodSpec

        param_names = self._get_param_names(name)
        params: dict[str, Any] = {}
        for i, arg in enumerate(args):
            if i < len(param_names):
                params[param_names[i]] = arg
        params.update(kwargs)

        params.pop("context", None)
        params.pop("_context", None)

        rpc_name = spec.rpc_name if isinstance(spec, MethodSpec) and spec.rpc_name else name
        read_timeout = spec.custom_timeout if isinstance(spec, MethodSpec) else None

        result = await self._call_rpc(rpc_name, params or None, read_timeout=read_timeout)

        # Apply response_key extraction
        if isinstance(spec, MethodSpec) and spec.response_key and isinstance(result, dict):
            return result.get(spec.response_key, result)

        return result

    # ============================================================
    # Core File Operations (hand-written — async + negative cache)
    # ============================================================

    async def read(
        self,
        path: str,
        context: Any = None,  # noqa: ARG002
        return_metadata: bool = False,
        parsed: bool = False,
    ) -> bytes | dict[str, Any]:
        if self._negative_cache_check(path):
            raise NexusFileNotFoundError(path)
        params: dict[str, Any] = {
            "path": path,
            "return_metadata": return_metadata,
            "parsed": parsed,
        }
        try:
            result = await self._call_rpc("read", params)
        except NexusFileNotFoundError:
            self._negative_cache_add(path)
            raise
        return self._parse_read_response(result, return_metadata)

    async def stat(self, path: str, context: Any = None) -> dict[str, Any]:  # noqa: ARG002
        if self._negative_cache_check(path):
            raise NexusFileNotFoundError(path)
        try:
            result = await self._call_rpc("stat", {"path": path})
        except NexusFileNotFoundError:
            self._negative_cache_add(path)
            raise
        return result  # type: ignore[no-any-return]

    async def exists(self, path: str) -> bool:
        if self._negative_cache_check(path):
            return False
        result = await self._call_rpc("exists", {"path": path})
        file_exists = result["exists"]
        if not file_exists:
            self._negative_cache_add(path)
        return file_exists  # type: ignore[no-any-return]

    async def write(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,  # noqa: ARG002
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
        lock: bool = False,
        lock_timeout: float = 30.0,
    ) -> dict[str, Any]:
        if isinstance(content, str):
            content = content.encode("utf-8")
        params: dict[str, Any] = {
            "path": path,
            "content": content,
            "if_match": if_match,
            "if_none_match": if_none_match,
            "force": force,
        }
        if lock:
            params["lock"] = True
        if lock_timeout != 30.0:
            params["lock_timeout"] = lock_timeout
        result = await self._call_rpc("write", params)
        self._negative_cache_invalidate(path)
        return result  # type: ignore[no-any-return]

    async def write_stream(
        self,
        path: str,
        chunks: Any,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        # Collect chunks (sync or async iterator)
        if hasattr(chunks, "__aiter__"):
            content = b"".join([chunk async for chunk in chunks])
        else:
            content = b"".join(chunks)
        result = await self._call_rpc("write_stream", {"path": path, "chunks": content})
        self._negative_cache_invalidate(path)
        return result  # type: ignore[no-any-return]

    async def write_batch(
        self,
        files: builtins.list[tuple[str, bytes]],
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[dict[str, Any]]:
        result = await self._call_rpc("write_batch", {"files": files})
        if files:
            self._negative_cache_invalidate_bulk([p for p, _ in files])
        return result  # type: ignore[no-any-return]

    async def append(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,  # noqa: ARG002
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if isinstance(content, str):
            content = content.encode("utf-8")
        result = await self._call_rpc(
            "append",
            {"path": path, "content": content, "if_match": if_match, "force": force},
        )
        self._negative_cache_invalidate(path)
        return result  # type: ignore[no-any-return]

    async def edit(
        self,
        path: str,
        edits: builtins.list[tuple[str, str]] | builtins.list[dict[str, Any]] | builtins.list[Any],
        context: Any = None,  # noqa: ARG002
        if_match: str | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]:
        serialized_edits: builtins.list[dict[str, Any]] = []
        for edit_op in edits:
            if isinstance(edit_op, (tuple, builtins.list)) and len(edit_op) >= 2:
                serialized_edits.append({"old_str": edit_op[0], "new_str": edit_op[1]})
            elif isinstance(edit_op, dict):
                serialized_edits.append(edit_op)
            elif hasattr(edit_op, "old_str") and hasattr(edit_op, "new_str"):
                serialized_edits.append(
                    {
                        "old_str": edit_op.old_str,
                        "new_str": edit_op.new_str,
                        "hint_line": getattr(edit_op, "hint_line", None),
                        "allow_multiple": getattr(edit_op, "allow_multiple", False),
                    }
                )
            else:
                serialized_edits.append({"old_str": str(edit_op), "new_str": ""})

        result = await self._call_rpc(
            "edit",
            {
                "path": path,
                "edits": serialized_edits,
                "if_match": if_match,
                "fuzzy_threshold": fuzzy_threshold,
                "preview": preview,
            },
        )
        if not preview and result.get("success"):
            self._negative_cache_invalidate(path)
        return result  # type: ignore[no-any-return]

    async def delta_read(
        self,
        path: str,
        client_content: bytes | None = None,
        client_hash: str | None = None,
        max_delta_ratio: float = 0.8,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"path": path, "max_delta_ratio": max_delta_ratio}
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
        result = await self._call_rpc(
            "delta_write",
            {"path": path, "delta": delta, "base_hash": base_hash, "if_match": if_match},
        )
        self._negative_cache_invalidate(path)
        return result  # type: ignore[no-any-return]

    async def delete(self, path: str) -> None:
        await self._call_rpc("delete", {"path": path})
        self._negative_cache_invalidate(path)

    async def delete_bulk(
        self,
        paths: builtins.list[str],
        recursive: bool = False,
    ) -> dict[str, dict]:
        result = await self._call_rpc("delete_bulk", {"paths": paths, "recursive": recursive})
        if paths:
            self._negative_cache_invalidate_bulk(paths)
        return result  # type: ignore[no-any-return]

    async def rename(self, old_path: str, new_path: str) -> None:
        await self._call_rpc("rename", {"old_path": old_path, "new_path": new_path})
        self._negative_cache_invalidate(old_path)

    async def rename_bulk(
        self,
        renames: builtins.list[tuple[str, str]],
    ) -> dict[str, dict]:
        result = await self._call_rpc("rename_bulk", {"renames": renames})
        if renames:
            self._negative_cache_invalidate_bulk([old for old, _ in renames])
        return result  # type: ignore[no-any-return]

    async def read_range(
        self,
        path: str,
        start: int,
        end: int,
        context: Any = None,  # noqa: ARG002
    ) -> bytes:
        result = await self._call_rpc("read_range", {"path": path, "start": start, "end": end})
        return self._decode_bytes_field(result)

    async def stream(
        self,
        path: str,
        chunk_size: int = 8192,
        context: Any = None,  # noqa: ARG002
    ) -> AsyncIterator[bytes]:
        info = await self.stat(path)
        file_size = info.get("size") or 0
        offset = 0
        while offset < file_size:
            end = min(offset + chunk_size, file_size)
            chunk = await self.read_range(path, offset, end)
            if not chunk:
                break
            yield chunk
            offset += len(chunk)

    # ============================================================
    # Operations with custom response extraction
    # ============================================================

    async def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        show_parsed: bool = True,
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
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

    async def glob(
        self,
        pattern: str,
        path: str = "/",
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[str]:
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

    async def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> builtins.list[tuple[str, str]]:
        result = await self._call_rpc("rebac_expand", {"permission": permission, "object": object})
        return [tuple(item) for item in result]

    async def rebac_expand_with_privacy(
        self,
        permission: str,
        object: tuple[str, str],
        respect_consent: bool = True,
        requester: tuple[str, str] | None = None,
    ) -> builtins.list[tuple[str, str]]:
        result = await self._call_rpc(
            "rebac_expand_with_privacy",
            {
                "permission": permission,
                "object": object,
                "respect_consent": respect_consent,
                "requester": requester,
            },
        )
        return [tuple(item) for item in result]

    # ============================================================
    # Operations with dynamic timeouts
    # ============================================================

    async def sandbox_connect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        context: dict | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "provider": provider,
            "mount_path": mount_path,
        }
        if sandbox_api_key is not None:
            params["sandbox_api_key"] = sandbox_api_key
        params["nexus_url"] = nexus_url or self.server_url
        params["nexus_api_key"] = nexus_api_key or self.api_key
        if agent_id is not None:
            params["agent_id"] = agent_id
        if context is not None:
            params["context"] = context
        return cast(dict, await self._call_rpc("sandbox_connect", params, read_timeout=60))

    async def sandbox_run(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        context: dict | None = None,
        as_script: bool = False,
    ) -> dict:
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
        if as_script:
            params["as_script"] = as_script
        return cast(dict, await self._call_rpc("sandbox_run", params, read_timeout=timeout + 10))

    async def wait_for_changes(
        self,
        path: str,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        result = await self._call_rpc(
            "wait_for_changes",
            {"path": path, "timeout": timeout},
            read_timeout=timeout + 5.0,
        )
        return result if result else None

    async def lock(
        self,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0,
    ) -> str | None:
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
        result = await self._call_rpc("extend_lock", {"lock_id": lock_id, "path": path, "ttl": ttl})
        return bool(result.get("extended", False)) if result else False

    async def unlock(self, lock_id: str, path: str) -> bool:
        result = await self._call_rpc("unlock", {"lock_id": lock_id, "path": path})
        return bool(result.get("released", False)) if result else False


# Register as virtual subclass of NexusFilesystem so isinstance() works at runtime
# without putting abstract methods in MRO (which would shadow __getattr__ dispatch).
NexusFilesystem.register(AsyncRemoteNexusFS)


# ============================================================
# Helper Classes (AsyncRemoteMemory, AsyncAdminAPI, AsyncACE)
# ============================================================


class AsyncRemoteMemory:
    """Async Remote Memory API client."""

    def __init__(self, remote_fs: AsyncRemoteNexusFS):
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
        params: dict[str, Any] = {"query": query, "limit": limit}
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
        result = await self.remote_fs._call_rpc("delete_memory", {"memory_id": memory_id})
        return result["deleted"]  # type: ignore[no-any-return]


class AsyncAdminAPI:
    """Async Admin API client for managing API keys."""

    def __init__(self, remote_fs: AsyncRemoteNexusFS):
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
        return await self.remote_fs._call_rpc("admin_create_key", params)  # type: ignore[no-any-return]

    async def list_keys(
        self,
        user_id: str | None = None,
        zone_id: str | None = None,
        is_admin: bool | None = None,
        include_expired: bool = False,
    ) -> builtins.list[dict[str, Any]]:
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
        result = await self.remote_fs._call_rpc("admin_get_key", {"key_id": key_id})
        return result.get("key")  # type: ignore[no-any-return]

    async def revoke_key(self, key_id: str) -> bool:
        result = await self.remote_fs._call_rpc("admin_revoke_key", {"key_id": key_id})
        return result.get("success", False)  # type: ignore[no-any-return]

    async def update_key(
        self,
        key_id: str,
        name: str | None = None,
        expires_days: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"key_id": key_id}
        if name is not None:
            params["name"] = name
        if expires_days is not None:
            params["expires_days"] = expires_days
        return await self.remote_fs._call_rpc("admin_update_key", params)  # type: ignore[no-any-return]


class AsyncACE:
    """Async ACE (Adaptive Concurrency Engine) API client."""

    def __init__(self, remote_fs: AsyncRemoteNexusFS):
        self.remote_fs = remote_fs

    async def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"task_description": task_description}
        if task_type is not None:
            params["task_type"] = task_type
        return await self.remote_fs._call_rpc("ace_start_trajectory", params)  # type: ignore[no-any-return]

    async def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "step_type": step_type,
            "description": description,
        }
        if result is not None:
            params["result"] = result
        return await self.remote_fs._call_rpc("ace_log_trajectory_step", params)  # type: ignore[no-any-return]

    async def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"trajectory_id": trajectory_id, "status": status}
        if success_score is not None:
            params["success_score"] = success_score
        if error_message is not None:
            params["error_message"] = error_message
        return await self.remote_fs._call_rpc("ace_complete_trajectory", params)  # type: ignore[no-any-return]

    async def add_feedback(
        self,
        trajectory_id: str,
        feedback_type: str,
        score: float | None = None,
        source: str | None = None,
        message: str | None = None,
        metrics: dict | None = None,
    ) -> dict[str, Any]:
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
        return await self.remote_fs._call_rpc("ace_add_feedback", params)  # type: ignore[no-any-return]

    async def get_trajectory_feedback(
        self,
        trajectory_id: str,
    ) -> builtins.list[dict[str, Any]]:
        return await self.remote_fs._call_rpc(  # type: ignore[no-any-return]
            "ace_get_trajectory_feedback", {"trajectory_id": trajectory_id}
        )

    async def get_effective_score(
        self,
        trajectory_id: str,
        strategy: str = "latest",
    ) -> dict[str, Any]:
        return await self.remote_fs._call_rpc(  # type: ignore[no-any-return]
            "ace_get_effective_score",
            {"trajectory_id": trajectory_id, "strategy": strategy},
        )

    async def mark_for_relearning(
        self,
        trajectory_id: str,
        reason: str,
        priority: int = 5,
    ) -> dict[str, Any]:
        return await self.remote_fs._call_rpc(  # type: ignore[no-any-return]
            "ace_mark_for_relearning",
            {"trajectory_id": trajectory_id, "reason": reason, "priority": priority},
        )

    async def query_trajectories(
        self,
        task_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if task_type is not None:
            params["task_type"] = task_type
        if status is not None:
            params["status"] = status
        return await self.remote_fs._call_rpc("ace_query_trajectories", params)  # type: ignore[no-any-return]

    async def create_playbook(
        self,
        name: str,
        description: str | None = None,
        scope: str = "agent",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "scope": scope}
        if description is not None:
            params["description"] = description
        return await self.remote_fs._call_rpc("ace_create_playbook", params)  # type: ignore[no-any-return]

    async def get_playbook(self, playbook_id: str) -> dict[str, Any] | None:
        return await self.remote_fs._call_rpc(  # type: ignore[no-any-return]
            "ace_get_playbook", {"playbook_id": playbook_id}
        )

    async def query_playbooks(
        self,
        scope: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if scope is not None:
            params["scope"] = scope
        return await self.remote_fs._call_rpc("ace_query_playbooks", params)  # type: ignore[no-any-return]
