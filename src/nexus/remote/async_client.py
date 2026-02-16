"""Async Remote Nexus filesystem client.

Implements NexusFilesystem asynchronously by proxying RPC calls to a Nexus
server over HTTP using httpx.AsyncClient. Uses __getattr__-based dispatch
for trivial methods, with explicit async overrides for complex methods.

Domain-specific methods (skills, sandbox, OAuth, MCP, share links, memory,
admin, ACE, LLM) are extracted into domain clients under nexus.remote.domain/
and exposed via @cached_property facade accessors (Issue #1603).

Issue #1289: Protocol + RPC Proxy pattern.

Example:
    async with AsyncRemoteNexusFS("http://localhost:2026", api_key="sk-xxx") as nx:
        content = await nx.read("/workspace/file.txt")
        contents = await asyncio.gather(*[nx.read(p) for p in paths])

        # Domain client access (new):
        await nx.skills.create("my-skill", "A skill", template="basic")
        await nx.sandbox.run("sb_123", "python", "print('hello')")

        # Backwards-compatible flat access (still works):
        await nx.skills_create("my-skill", "A skill", template="basic")
"""

from __future__ import annotations

import builtins
import logging
import time
import uuid
from collections.abc import AsyncIterator
from functools import cached_property
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

if TYPE_CHECKING:
    from nexus.remote.domain.ace import AsyncACEClient
    from nexus.remote.domain.admin import AsyncAdminClient
    from nexus.remote.domain.llm import AsyncLLMClient
    from nexus.remote.domain.mcp import AsyncMCPClient
    from nexus.remote.domain.memory import AsyncMemoryClient
    from nexus.remote.domain.oauth import AsyncOAuthClient
    from nexus.remote.domain.sandbox import AsyncSandboxClient
    from nexus.remote.domain.share_links import AsyncShareLinksClient
    from nexus.remote.domain.skills import AsyncSkillsClient

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
from nexus.server.protocol import (
    RPCRequest,
    RPCResponse,
    decode_rpc_message,
    encode_rpc_message,
)

from .client import (
    _DOMAIN_METHOD_MAP,
    RemoteConnectionError,
    RemoteFilesystemError,
    RemoteTimeoutError,
)

logger = logging.getLogger(__name__)


# ============================================================
# Async domain method map — includes async-only domains
# ============================================================

_ASYNC_DOMAIN_METHOD_MAP: dict[str, tuple[str, str]] = {
    **_DOMAIN_METHOD_MAP,
    # Admin (5 methods, async-only)
    "admin_create_key": ("admin", "create_key"),
    "admin_list_keys": ("admin", "list_keys"),
    "admin_get_key": ("admin", "get_key"),
    "admin_revoke_key": ("admin", "revoke_key"),
    "admin_update_key": ("admin", "update_key"),
    # ACE (11 methods, async-only)
    "ace_start_trajectory": ("ace", "start_trajectory"),
    "ace_log_step": ("ace", "log_step"),
    "ace_complete_trajectory": ("ace", "complete_trajectory"),
    "ace_add_feedback": ("ace", "add_feedback"),
    "ace_get_trajectory_feedback": ("ace", "get_trajectory_feedback"),
    "ace_get_effective_score": ("ace", "get_effective_score"),
    "ace_mark_for_relearning": ("ace", "mark_for_relearning"),
    "ace_query_trajectories": ("ace", "query_trajectories"),
    "ace_create_playbook": ("ace", "create_playbook"),
    "ace_get_playbook": ("ace", "get_playbook"),
    "ace_query_playbooks": ("ace", "query_playbooks"),
}


class AsyncRemoteNexusFS(RPCProxyBase, BaseRemoteNexusFS):
    """Async remote Nexus filesystem client.

    Uses httpx.AsyncClient for non-blocking HTTP calls. Trivial methods
    are auto-dispatched via __getattr__; complex methods are async overrides.

    Domain-specific operations are exposed via cached_property accessors:
    - self.skills — AsyncSkillsClient (22 methods)
    - self.sandbox — AsyncSandboxClient (10 methods)
    - self.oauth — AsyncOAuthClient (6 methods)
    - self.mcp — AsyncMCPClient (8+1 methods)
    - self.share_links — AsyncShareLinksClient (6 methods)
    - self.memory — AsyncMemoryClient (21 methods)
    - self.admin — AsyncAdminClient (5 methods)
    - self.ace — AsyncACEClient (11 methods)
    - self.llm — AsyncLLMClient (4 methods)

    Backwards compatibility: flat names like `skills_create(...)` still work
    via __getattr__ delegation to domain clients.
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
    # Domain Client Facade (cached_property accessors)
    # ============================================================

    @cached_property
    def skills(self) -> AsyncSkillsClient:
        from nexus.remote.domain.skills import AsyncSkillsClient as _AsyncSkillsClient

        return _AsyncSkillsClient(self._call_rpc)

    @cached_property
    def sandbox(self) -> AsyncSandboxClient:
        from nexus.remote.domain.sandbox import AsyncSandboxClient as _AsyncSandboxClient

        return _AsyncSandboxClient(
            self._call_rpc,
            lambda: self.server_url,
            lambda: self.api_key,
        )

    @cached_property
    def oauth(self) -> AsyncOAuthClient:
        from nexus.remote.domain.oauth import AsyncOAuthClient as _AsyncOAuthClient

        return _AsyncOAuthClient(self._call_rpc)

    @cached_property
    def mcp(self) -> AsyncMCPClient:
        from nexus.remote.domain.mcp import AsyncMCPClient as _AsyncMCPClient

        return _AsyncMCPClient(self._call_rpc)

    @cached_property
    def share_links(self) -> AsyncShareLinksClient:
        from nexus.remote.domain.share_links import (
            AsyncShareLinksClient as _AsyncShareLinksClient,
        )

        return _AsyncShareLinksClient(self._call_rpc)

    @cached_property
    def memory(self) -> AsyncMemoryClient:
        from nexus.remote.domain.memory import AsyncMemoryClient as _AsyncMemoryClient

        return _AsyncMemoryClient(self._call_rpc)

    @cached_property
    def admin(self) -> AsyncAdminClient:
        from nexus.remote.domain.admin import AsyncAdminClient as _AsyncAdminClient

        return _AsyncAdminClient(self._call_rpc)

    @cached_property
    def ace(self) -> AsyncACEClient:
        from nexus.remote.domain.ace import AsyncACEClient as _AsyncACEClient

        return _AsyncACEClient(self._call_rpc)

    @cached_property
    def llm(self) -> AsyncLLMClient:
        from nexus.remote.domain.llm import AsyncLLMClient as _AsyncLLMClient

        return _AsyncLLMClient(self._get_llm_service)

    def _get_llm_service(self) -> Any:
        """Lazy accessor for LLMService — set by application code."""
        return getattr(self, "_llm_service", None)

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
    # __getattr__ override — domain delegation + async dispatch
    # ============================================================

    def __getattr__(self, name: str) -> Any:
        """Dynamic dispatch — domain delegation + async proxy for RPC methods."""
        from nexus.remote.method_registry import METHOD_REGISTRY
        from nexus.remote.rpc_proxy import _INTERNAL_ATTRS

        if name.startswith("_") or name in _INTERNAL_ATTRS:
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

        # Check domain method map first (backwards-compat delegation)
        mapping = _ASYNC_DOMAIN_METHOD_MAP.get(name)
        if mapping is not None:
            domain_name, method_name = mapping
            domain = getattr(self, domain_name)
            return getattr(domain, method_name)

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

    async def exists(self, path: str, context: Any = None) -> bool:  # noqa: ARG002
        if self._negative_cache_check(path):
            return False
        result = await self._call_rpc("exists", {"path": path})
        file_exists = result["exists"]
        if not file_exists:
            self._negative_cache_add(path)
        return file_exists  # type: ignore[no-any-return]

    async def get_etag(self, path: str) -> str | None:
        if self._negative_cache_check(path):
            return None
        try:
            result = await self._call_rpc("get_etag", {"path": path})
        except NexusFileNotFoundError:
            self._negative_cache_add(path)
            return None
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            etag = result.get("etag")
            return str(etag) if etag is not None else None
        return None

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

    async def delete(self, path: str, context: Any = None) -> bool:  # noqa: ARG002
        await self._call_rpc("delete", {"path": path})
        self._negative_cache_invalidate(path)
        return True

    async def delete_bulk(
        self,
        paths: builtins.list[str],
        recursive: bool = False,
    ) -> dict[str, dict]:
        result = await self._call_rpc("delete_bulk", {"paths": paths, "recursive": recursive})
        if paths:
            self._negative_cache_invalidate_bulk(paths)
        return result  # type: ignore[no-any-return]

    async def rename(self, old_path: str, new_path: str, context: Any = None) -> dict[str, Any]:  # noqa: ARG002
        result = await self._call_rpc("rename", {"old_path": old_path, "new_path": new_path})
        self._negative_cache_invalidate(old_path)
        return result if isinstance(result, dict) else {}

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

    async def stream_range(
        self,
        path: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: Any = None,  # noqa: ARG002
    ) -> AsyncIterator[bytes]:
        offset = start
        while offset <= end:
            read_end = min(offset + chunk_size, end + 1)
            chunk = await self.read_range(path, offset, read_end)
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
# Backwards-compat wrappers (delegate to domain clients)
# ============================================================


class AsyncRemoteMemory:
    """Async Remote Memory API client (backwards-compatible wrapper).

    Delegates to AsyncMemoryClient domain client.
    """

    def __init__(self, remote_fs: AsyncRemoteNexusFS):
        from nexus.remote.domain.memory import AsyncMemoryClient as _AsyncMemoryClient

        self.remote_fs = remote_fs
        # Use lambda to ensure dynamic resolution of _call_rpc (supports test mocking)
        self._client = _AsyncMemoryClient(lambda *a, **kw: remote_fs._call_rpc(*a, **kw))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def __dir__(self) -> list[str]:
        return list(set(super().__dir__()) | set(dir(self._client)))


class AsyncAdminAPI:
    """Async Admin API client (backwards-compatible wrapper).

    Delegates to AsyncAdminClient domain client.
    """

    def __init__(self, remote_fs: AsyncRemoteNexusFS):
        from nexus.remote.domain.admin import AsyncAdminClient as _AsyncAdminClient

        self.remote_fs = remote_fs
        self._client = _AsyncAdminClient(lambda *a, **kw: remote_fs._call_rpc(*a, **kw))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def __dir__(self) -> list[str]:
        return list(set(super().__dir__()) | set(dir(self._client)))


class AsyncACE:
    """Async ACE client (backwards-compatible wrapper).

    Delegates to AsyncACEClient domain client.
    """

    def __init__(self, remote_fs: AsyncRemoteNexusFS):
        from nexus.remote.domain.ace import AsyncACEClient as _AsyncACEClient

        self.remote_fs = remote_fs
        self._client = _AsyncACEClient(lambda *a, **kw: remote_fs._call_rpc(*a, **kw))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def __dir__(self) -> list[str]:
        return list(set(super().__dir__()) | set(dir(self._client)))
