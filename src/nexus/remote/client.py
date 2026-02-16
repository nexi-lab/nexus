"""Remote Nexus filesystem client (sync).

Implements NexusFilesystem by proxying RPC calls to a Nexus server over HTTP.
Uses __getattr__-based dispatch for ~170 trivial methods, with explicit
overrides for ~30 methods requiring negative cache, content encoding,
response decoding, dynamic timeouts, or other complex logic.

Domain-specific methods (skills, sandbox, OAuth, MCP, share links, memory)
are extracted into domain clients under nexus.remote.domain/ and exposed
via @cached_property facade accessors (Issue #1603).

Issue #1289: Protocol + RPC Proxy pattern (~83% LOC reduction).

Example:
    nx = RemoteNexusFS("http://localhost:2026", api_key="sk-xxx")
    content = nx.read("/workspace/file.txt")
    files = nx.list("/workspace")

    # Domain client access (new):
    nx.skills.create("my-skill", "A skill", template="basic")
    nx.sandbox.run("sb_123", "python", "print('hello')")

    # Backwards-compatible flat access (still works):
    nx.skills_create("my-skill", "A skill", template="basic")
"""

from __future__ import annotations

import builtins
import logging
import time
import uuid
from collections.abc import Iterator
from functools import cached_property
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

if TYPE_CHECKING:
    from nexus.remote.domain.mcp import MCPClient
    from nexus.remote.domain.oauth import OAuthClient
    from nexus.remote.domain.sandbox import SandboxClient
    from nexus.remote.domain.share_links import ShareLinksClient
    from nexus.remote.domain.skills import SkillsClient

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nexus.core.exceptions import (
    NexusError,
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

logger = logging.getLogger(__name__)


# ============================================================
# Error Classes
# ============================================================


class RemoteFilesystemError(NexusError):
    """Enhanced remote filesystem error with detailed information."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        method: str | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.method = method

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


# ============================================================
# Backwards-compat: flat method → domain delegation map
# ============================================================

_DOMAIN_METHOD_MAP: dict[str, tuple[str, str]] = {
    # Skills (22 methods)
    "skills_create": ("skills", "create"),
    "skills_create_from_content": ("skills", "create_from_content"),
    "skills_create_from_file": ("skills", "create_from_file"),
    "skills_list": ("skills", "list"),
    "skills_info": ("skills", "info"),
    "skills_fork": ("skills", "fork"),
    "skills_publish": ("skills", "publish"),
    "skills_search": ("skills", "search"),
    "skills_submit_approval": ("skills", "submit_approval"),
    "skills_approve": ("skills", "approve"),
    "skills_reject": ("skills", "reject"),
    "skills_list_approvals": ("skills", "list_approvals"),
    "skills_import": ("skills", "import_zip"),
    "skills_validate_zip": ("skills", "validate_zip"),
    "skills_export": ("skills", "export"),
    "skills_share": ("skills", "share"),
    "skills_unshare": ("skills", "unshare"),
    "skills_discover": ("skills", "discover"),
    "skills_subscribe": ("skills", "subscribe"),
    "skills_unsubscribe": ("skills", "unsubscribe"),
    "skills_get_prompt_context": ("skills", "get_prompt_context"),
    "skills_load": ("skills", "load"),
    # Sandbox (10 methods)
    "sandbox_connect": ("sandbox", "connect"),
    "sandbox_run": ("sandbox", "run"),
    "sandbox_pause": ("sandbox", "pause"),
    "sandbox_resume": ("sandbox", "resume"),
    "sandbox_stop": ("sandbox", "stop"),
    "sandbox_list": ("sandbox", "list"),
    "sandbox_status": ("sandbox", "status"),
    "sandbox_get_or_create": ("sandbox", "get_or_create"),
    "sandbox_disconnect": ("sandbox", "disconnect"),
    "sandbox_validate": ("sandbox", "validate"),
    # OAuth (6 methods)
    "oauth_list_providers": ("oauth", "list_providers"),
    "oauth_get_auth_url": ("oauth", "get_auth_url"),
    "oauth_exchange_code": ("oauth", "exchange_code"),
    "oauth_list_credentials": ("oauth", "list_credentials"),
    "oauth_revoke_credential": ("oauth", "revoke_credential"),
    "oauth_test_credential": ("oauth", "test_credential"),
    # MCP (8+1 methods)
    "mcp_connect": ("mcp", "connect"),
    "mcp_get_oauth_url": ("mcp", "get_oauth_url"),
    "mcp_list_mounts": ("mcp", "list_mounts"),
    "mcp_list_tools": ("mcp", "list_tools"),
    "mcp_mount": ("mcp", "mount"),
    "mcp_unmount": ("mcp", "unmount"),
    "mcp_sync": ("mcp", "sync"),
    "backfill_directory_index": ("mcp", "backfill_directory_index"),
    # Share Links (6 methods)
    "create_share_link": ("share_links", "create"),
    "get_share_link": ("share_links", "get"),
    "list_share_links": ("share_links", "list"),
    "revoke_share_link": ("share_links", "revoke"),
    "access_share_link": ("share_links", "access"),
    "get_share_link_access_logs": ("share_links", "get_access_logs"),
}


# ============================================================
# RemoteMemory — backwards-compat wrapper (delegates to MemoryClient)
# ============================================================


class RemoteMemory:
    """Remote Memory API client (backwards-compatible wrapper).

    Provides the same interface as core.memory_api.Memory but delegates to
    the MemoryClient domain client under the hood.
    """

    def __init__(self, remote_fs: RemoteNexusFS):
        from nexus.remote.domain.memory import MemoryClient as _MemoryClient

        self.remote_fs = remote_fs
        # Use lambda to ensure dynamic resolution of _call_rpc (supports test mocking)
        self._client = _MemoryClient(lambda *a, **kw: remote_fs._call_rpc(*a, **kw))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def __dir__(self) -> builtins.list[str]:
        return list(set(super().__dir__()) | set(dir(self._client)))


# ============================================================
# RemoteNexusFS — Sync RPC Proxy Client
# ============================================================


class RemoteNexusFS(RPCProxyBase, BaseRemoteNexusFS):
    """Remote Nexus filesystem client.

    Implements NexusFilesystem interface by making RPC calls to a remote server.
    Trivial methods (~170) are auto-dispatched via __getattr__; complex methods
    (~30) are explicit overrides below.

    Domain-specific operations are exposed via cached_property accessors:
    - self.skills — SkillsClient (22 methods)
    - self.sandbox — SandboxClient (10 methods)
    - self.oauth — OAuthClient (6 methods)
    - self.mcp — MCPClient (8+1 methods)
    - self.share_links — ShareLinksClient (6 methods)

    Backwards compatibility: flat names like `skills_create(...)` still work
    via __getattr__ delegation to domain clients.
    """

    def __init__(
        self,
        server_url: str,
        api_key: str | None = None,
        timeout: int = 90,
        connect_timeout: int = 5,
        max_retries: int = 3,
        pool_connections: int = 10,
        pool_maxsize: int = 20,
        negative_cache_capacity: int = 100_000,
        negative_cache_fp_rate: float = 0.01,
    ):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_retries = max_retries

        self._agent_id: str | None = None
        self._zone_id: str | None = None
        self._semantic_search = None
        self._memory_api: RemoteMemory | None = None

        # Pre-build default timeout config
        self._default_timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.timeout,
            write=self.timeout,
            pool=self.timeout,
        )

        limits = httpx.Limits(
            max_connections=pool_maxsize,
            max_keepalive_connections=pool_connections,
        )

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self.session = httpx.Client(
            limits=limits,
            timeout=self._default_timeout,
            headers=headers,
            http2=True,
            trust_env=False,
        )

        if api_key:
            try:
                self._fetch_auth_info()
            except Exception as e:
                logger.warning(f"Failed to fetch auth info: {e}")

        self._negative_cache_capacity = negative_cache_capacity
        self._negative_cache_fp_rate = negative_cache_fp_rate
        self._negative_bloom: Any = None
        self._init_negative_cache()

    def _fetch_auth_info(self) -> None:
        """Fetch authenticated user info from server."""
        try:
            response = self.session.get(
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
    def skills(self) -> SkillsClient:
        from nexus.remote.domain.skills import SkillsClient as _SkillsClient

        return _SkillsClient(self._call_rpc)

    @cached_property
    def sandbox(self) -> SandboxClient:
        from nexus.remote.domain.sandbox import SandboxClient as _SandboxClient

        return _SandboxClient(
            self._call_rpc,
            lambda: self.server_url,
            lambda: self.api_key,
        )

    @cached_property
    def oauth(self) -> OAuthClient:
        from nexus.remote.domain.oauth import OAuthClient as _OAuthClient

        return _OAuthClient(self._call_rpc)

    @cached_property
    def mcp(self) -> MCPClient:
        from nexus.remote.domain.mcp import MCPClient as _MCPClient

        return _MCPClient(self._call_rpc)

    @cached_property
    def share_links(self) -> ShareLinksClient:
        from nexus.remote.domain.share_links import ShareLinksClient as _ShareLinksClient

        return _ShareLinksClient(self._call_rpc)

    # ============================================================
    # RPC Transport
    # ============================================================

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
        url = urljoin(self.server_url, f"/api/nfs/{method}")

        start_time = time.time()
        logger.debug(f"API call: {method} with params: {params}")

        try:
            headers: dict[str, str] = {
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
            }
            if self.agent_id:
                headers["X-Agent-ID"] = self.agent_id
            if self.zone_id:
                headers["X-Nexus-Zone-ID"] = self.zone_id

            # Use custom timeout if provided, otherwise use pre-built default
            if read_timeout is not None:
                request_timeout = httpx.Timeout(
                    connect=self.connect_timeout,
                    read=read_timeout,
                    write=read_timeout,
                    pool=read_timeout,
                )
            else:
                request_timeout = self._default_timeout

            response = self.session.post(
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
    # __getattr__ override — domain delegation + RPC proxy
    # ============================================================

    def __getattr__(self, name: str) -> Any:
        # Check domain method map first (backwards-compat delegation)
        mapping = _DOMAIN_METHOD_MAP.get(name)
        if mapping is not None:
            domain_name, method_name = mapping
            domain = getattr(self, domain_name)
            return getattr(domain, method_name)

        # Fall through to RPCProxyBase.__getattr__ for auto-dispatch
        return RPCProxyBase.__getattr__(self, name)

    # ============================================================
    # Core File Operations (hand-written — negative cache)
    # ============================================================

    def read(
        self,
        path: str,
        context: Any = None,  # noqa: ARG002
        return_metadata: bool = False,
    ) -> bytes | dict[str, Any]:
        if self._negative_cache_check(path):
            raise NexusFileNotFoundError(path)
        try:
            result = self._call_rpc("read", {"path": path, "return_metadata": return_metadata})
        except NexusFileNotFoundError:
            self._negative_cache_add(path)
            raise
        return self._parse_read_response(result, return_metadata)

    def stat(self, path: str, context: Any = None) -> dict[str, Any]:  # noqa: ARG002
        if self._negative_cache_check(path):
            raise NexusFileNotFoundError(path)
        try:
            result = self._call_rpc("stat", {"path": path})
        except NexusFileNotFoundError:
            self._negative_cache_add(path)
            raise
        return result  # type: ignore[no-any-return]

    def exists(self, path: str, context: Any = None) -> bool:  # noqa: ARG002
        if self._negative_cache_check(path):
            return False
        result = self._call_rpc("exists", {"path": path})
        file_exists = result["exists"]
        if not file_exists:
            self._negative_cache_add(path)
        return file_exists  # type: ignore[no-any-return]

    def get_etag(self, path: str) -> str | None:
        if self._negative_cache_check(path):
            return None
        try:
            result = self._call_rpc("get_etag", {"path": path})
        except NexusFileNotFoundError:
            self._negative_cache_add(path)
            return None
        # Server may return etag as a bare string or in a dict
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            etag = result.get("etag")
            return str(etag) if etag is not None else None
        return None

    def read_range(
        self,
        path: str,
        start: int,
        end: int,
        context: Any = None,  # noqa: ARG002
    ) -> bytes:
        result = self._call_rpc("read_range", {"path": path, "start": start, "end": end})
        return self._decode_bytes_field(result)

    def stream(
        self,
        path: str,
        chunk_size: int = 8192,
        context: Any = None,  # noqa: ARG002
    ) -> Any:
        info = self.stat(path)
        file_size = info.get("size") or 0
        offset = 0
        while offset < file_size:
            end = min(offset + chunk_size, file_size)
            chunk = self.read_range(path, offset, end)
            if not chunk:
                break
            yield chunk
            offset += len(chunk)

    def stream_range(
        self,
        path: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: Any = None,  # noqa: ARG002
    ) -> Any:
        offset = start
        while offset <= end:
            read_end = min(offset + chunk_size, end + 1)
            chunk = self.read_range(path, offset, read_end)
            if not chunk:
                break
            yield chunk
            offset += len(chunk)

    def write(
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
        result = self._call_rpc("write", params)
        self._negative_cache_invalidate(path)
        return result  # type: ignore[no-any-return]

    def write_stream(
        self,
        path: str,
        chunks: Iterator[bytes],
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        content = b"".join(chunks)
        result = self._call_rpc("write_stream", {"path": path, "chunks": content})
        self._negative_cache_invalidate(path)
        return result  # type: ignore[no-any-return]

    def write_batch(
        self,
        files: builtins.list[tuple[str, bytes]],
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[dict[str, Any]]:
        result = self._call_rpc("write_batch", {"files": files})
        if files:
            self._negative_cache_invalidate_bulk([p for p, _ in files])
        return result  # type: ignore[no-any-return]

    def append(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,  # noqa: ARG002
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if isinstance(content, str):
            content = content.encode("utf-8")
        result = self._call_rpc(
            "append",
            {"path": path, "content": content, "if_match": if_match, "force": force},
        )
        self._negative_cache_invalidate(path)
        return result  # type: ignore[no-any-return]

    def edit(
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

        result = self._call_rpc(
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

    def delta_read(
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
        result = self._call_rpc("delta_read", params)
        return self._decode_delta_read_response(result)

    def delta_write(
        self,
        path: str,
        delta: bytes,
        base_hash: str,
        if_match: str | None = None,
        context: Any = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        result = self._call_rpc(
            "delta_write",
            {"path": path, "delta": delta, "base_hash": base_hash, "if_match": if_match},
        )
        self._negative_cache_invalidate(path)
        return result  # type: ignore[no-any-return]

    def delete(self, path: str, context: Any = None) -> bool:  # noqa: ARG002
        self._call_rpc("delete", {"path": path})
        self._negative_cache_invalidate(path)
        return True

    def delete_bulk(
        self,
        paths: builtins.list[str],
        recursive: bool = False,
    ) -> dict[str, dict]:
        result = self._call_rpc("delete_bulk", {"paths": paths, "recursive": recursive})
        if paths:
            self._negative_cache_invalidate_bulk(paths)
        return result  # type: ignore[no-any-return]

    def rename(self, old_path: str, new_path: str, context: Any = None) -> dict[str, Any]:  # noqa: ARG002
        result = self._call_rpc("rename", {"old_path": old_path, "new_path": new_path})
        self._negative_cache_invalidate(old_path)
        return result if isinstance(result, dict) else {}

    def rename_bulk(
        self,
        renames: builtins.list[tuple[str, str]],
    ) -> dict[str, dict]:
        result = self._call_rpc("rename_bulk", {"renames": renames})
        if renames:
            self._negative_cache_invalidate_bulk([old for old, _ in renames])
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Operations with custom response extraction
    # ============================================================

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> builtins.list[tuple[str, str]]:
        result = self._call_rpc("rebac_expand", {"permission": permission, "object": object})
        return [tuple(item) for item in result]

    def rebac_expand_with_privacy(
        self,
        permission: str,
        object: tuple[str, str],
        respect_consent: bool = True,
        requester: tuple[str, str] | None = None,
    ) -> builtins.list[tuple[str, str]]:
        result = self._call_rpc(
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
    # Event Operations (Issue #1106 Block 2)
    # ============================================================

    def wait_for_changes(
        self,
        path: str,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        result = self._call_rpc(
            "wait_for_changes",
            {"path": path, "timeout": timeout},
            read_timeout=timeout + 5.0,
        )
        return result if result else None

    def lock(
        self,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0,
    ) -> str | None:
        result = self._call_rpc(
            "lock",
            {"path": path, "timeout": timeout, "ttl": ttl},
            read_timeout=timeout + 5.0,
        )
        return result.get("lock_id") if result else None

    def extend_lock(
        self,
        lock_id: str,
        path: str,
        ttl: float = 30.0,
    ) -> bool:
        result = self._call_rpc("extend_lock", {"lock_id": lock_id, "path": path, "ttl": ttl})
        return bool(result.get("extended", False)) if result else False

    def unlock(self, lock_id: str, path: str) -> bool:
        result = self._call_rpc("unlock", {"lock_id": lock_id, "path": path})
        return bool(result.get("released", False)) if result else False

    # ============================================================
    # Lifecycle
    # ============================================================

    @property
    def memory(self) -> RemoteMemory:
        if self._memory_api is None:
            self._memory_api = RemoteMemory(self)
        return self._memory_api

    def __enter__(self) -> RemoteNexusFS:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()


# Register as virtual subclass of NexusFilesystem so isinstance() works at runtime
# without putting abstract methods in MRO (which would shadow __getattr__ dispatch).
NexusFilesystem.register(RemoteNexusFS)
