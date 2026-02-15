"""Remote Nexus filesystem client (sync).

Implements NexusFilesystem by proxying RPC calls to a Nexus server over HTTP.
Uses __getattr__-based dispatch for ~170 trivial methods, with explicit
overrides for ~30 methods requiring negative cache, content encoding,
response decoding, dynamic timeouts, or other complex logic.

Issue #1289: Protocol + RPC Proxy pattern (~83% LOC reduction).

Example:
    nx = RemoteNexusFS("http://localhost:2026", api_key="sk-xxx")
    content = nx.read("/workspace/file.txt")
    files = nx.list("/workspace")
"""

from __future__ import annotations

import builtins
import logging
import time
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin

from nexus.constants import DEFAULT_OAUTH_REDIRECT_URI

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.services.llm_service import LLMService

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
# RemoteMemory — wraps _call_rpc for memory operations
# ============================================================


class RemoteMemory:
    """Remote Memory API client.

    Provides the same interface as core.memory_api.Memory but makes RPC calls
    to a remote Nexus server instead of direct database access.
    """

    def __init__(self, remote_fs: RemoteNexusFS):
        self.remote_fs = remote_fs

    # --- Trajectory Methods ---

    def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
        _parent_trajectory_id: str | None = None,
        _metadata: dict[str, Any] | None = None,
        _path: str | None = None,
    ) -> str:
        params: dict[str, Any] = {"task_description": task_description}
        if task_type is not None:
            params["task_type"] = task_type
        result = self.remote_fs._call_rpc("start_trajectory", params)
        return result["trajectory_id"]  # type: ignore[no-any-return]

    def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        _metadata: dict[str, Any] | None = None,
    ) -> None:
        params: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "step_type": step_type,
            "description": description,
        }
        if result is not None:
            params["result"] = result
        self.remote_fs._call_rpc("log_trajectory_step", params)

    def log_trajectory_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.log_step(trajectory_id, step_type, description, result, metadata)

    def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        _metrics: dict[str, Any] | None = None,
    ) -> str:
        params: dict[str, Any] = {"trajectory_id": trajectory_id, "status": status}
        if success_score is not None:
            params["success_score"] = success_score
        if error_message is not None:
            params["error_message"] = error_message
        result = self.remote_fs._call_rpc("complete_trajectory", params)
        return result["trajectory_id"]  # type: ignore[no-any-return]

    def query_trajectories(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if status is not None:
            params["status"] = status
        if limit != 50:
            params["limit"] = limit
        result = self.remote_fs._call_rpc("query_trajectories", params)
        return result.get("trajectories", [])  # type: ignore[no-any-return]

    # --- Playbook Methods ---

    def get_playbook(self, playbook_name: str = "default") -> dict[str, Any] | None:
        return self.remote_fs._call_rpc("get_playbook", {"playbook_name": playbook_name})  # type: ignore[no-any-return]

    def query_playbooks(
        self,
        agent_id: str | None = None,
        scope: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if scope is not None:
            params["scope"] = scope
        if limit != 50:
            params["limit"] = limit
        result = self.remote_fs._call_rpc("query_playbooks", params)
        return result.get("playbooks", [])  # type: ignore[no-any-return]

    def process_relearning(self, limit: int = 10) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if limit != 10:
            params["limit"] = limit
        result = self.remote_fs._call_rpc("process_relearning", params)
        return result.get("results", [])  # type: ignore[no-any-return]

    def curate_playbook(
        self,
        reflection_memory_ids: list[str],
        playbook_name: str = "default",
        merge_threshold: float = 0.7,
    ) -> dict[str, Any]:
        return self.remote_fs._call_rpc(  # type: ignore[no-any-return]
            "curate_playbook",
            {
                "reflection_memory_ids": reflection_memory_ids,
                "playbook_name": playbook_name,
                "merge_threshold": merge_threshold,
            },
        )

    def batch_reflect(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        min_trajectories: int = 10,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"min_trajectories": min_trajectories}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if since is not None:
            params["since"] = since
        if task_type is not None:
            params["task_type"] = task_type
        return self.remote_fs._call_rpc("batch_reflect", params)  # type: ignore[no-any-return]

    # --- Memory Storage Methods ---

    def store(
        self,
        content: str,
        memory_type: str = "fact",
        scope: str = "agent",
        importance: float = 0.5,
        namespace: str | None = None,
        path_key: str | None = None,
        state: str = "active",
        tags: list[str] | None = None,
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
        result = self.remote_fs._call_rpc("store_memory", params)
        return result["memory_id"]  # type: ignore[no-any-return]

    def list(
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
        result = self.remote_fs._call_rpc("list_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    def retrieve(
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
        result = self.remote_fs._call_rpc("retrieve_memory", params)
        return result.get("memory")  # type: ignore[no-any-return]

    def query(
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
        result = self.remote_fs._call_rpc("query_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    def search(
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
        result = self.remote_fs._call_rpc("query_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    def delete(self, memory_id: str) -> bool:
        result = self.remote_fs._call_rpc("delete_memory", {"memory_id": memory_id})
        return result["deleted"]  # type: ignore[no-any-return]

    def approve(self, memory_id: str) -> bool:
        result = self.remote_fs._call_rpc("approve_memory", {"memory_id": memory_id})
        return result["approved"]  # type: ignore[no-any-return]

    def deactivate(self, memory_id: str) -> bool:
        result = self.remote_fs._call_rpc("deactivate_memory", {"memory_id": memory_id})
        return result["deactivated"]  # type: ignore[no-any-return]

    def approve_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        return self.remote_fs._call_rpc("approve_memory_batch", {"memory_ids": memory_ids})  # type: ignore[no-any-return]

    def deactivate_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        return self.remote_fs._call_rpc("deactivate_memory_batch", {"memory_ids": memory_ids})  # type: ignore[no-any-return]

    def delete_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        return self.remote_fs._call_rpc("delete_memory_batch", {"memory_ids": memory_ids})  # type: ignore[no-any-return]


# ============================================================
# RemoteNexusFS — Sync RPC Proxy Client
# ============================================================


class RemoteNexusFS(RPCProxyBase, BaseRemoteNexusFS):
    """Remote Nexus filesystem client.

    Implements NexusFilesystem interface by making RPC calls to a remote server.
    Trivial methods (~170) are auto-dispatched via __getattr__; complex methods
    (~30) are explicit overrides below.

    Registered as a virtual subclass of NexusFilesystem (via ABC.register)
    so isinstance() checks work, while avoiding ABC method definitions in MRO
    that would shadow __getattr__-based dispatch.
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
        """Stream a byte range of file content using server-side read_range().

        Args:
            path: Virtual path to stream
            start: Start byte offset (inclusive)
            end: End byte offset (inclusive)
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Unused in remote client (handled server-side)

        Yields:
            bytes: Chunks of file content within the requested range
        """
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
    # Operations with dynamic timeouts
    # ============================================================

    def sandbox_connect(
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
        # Auto-provide Nexus URL and API key from client
        params["nexus_url"] = nexus_url or self.server_url
        params["nexus_api_key"] = nexus_api_key or self.api_key
        if agent_id is not None:
            params["agent_id"] = agent_id
        if context is not None:
            params["context"] = context
        return cast(dict, self._call_rpc("sandbox_connect", params, read_timeout=60))

    def sandbox_run(
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
        return cast(dict, self._call_rpc("sandbox_run", params, read_timeout=timeout + 10))

    def sandbox_pause(self, sandbox_id: str, context: dict | None = None) -> dict:
        """Pause sandbox to save costs.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata

        Example:
            >>> result = nx.sandbox_pause("sb_123")
            >>> print(result['status'])  # 'paused'
        """
        params: dict[str, Any] = {"sandbox_id": sandbox_id}
        if context is not None:
            params["context"] = context
        result = self._call_rpc("sandbox_pause", params)
        return result  # type: ignore[no-any-return]

    def sandbox_resume(self, sandbox_id: str, context: dict | None = None) -> dict:
        """Resume a paused sandbox.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata

        Example:
            >>> result = nx.sandbox_resume("sb_123")
            >>> print(result['status'])  # 'active'
        """
        params: dict[str, Any] = {"sandbox_id": sandbox_id}
        if context is not None:
            params["context"] = context
        result = self._call_rpc("sandbox_resume", params)
        return result  # type: ignore[no-any-return]

    def sandbox_stop(self, sandbox_id: str, context: dict | None = None) -> dict:
        """Stop and destroy sandbox.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Updated sandbox metadata

        Example:
            >>> result = nx.sandbox_stop("sb_123")
            >>> print(result['status'])  # 'stopped'
        """
        params: dict[str, Any] = {"sandbox_id": sandbox_id}
        if context is not None:
            params["context"] = context
        result = self._call_rpc("sandbox_stop", params)
        return result  # type: ignore[no-any-return]

    def sandbox_list(
        self,
        context: dict | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        """List user's sandboxes.

        Args:
            context: Operation context
            verify_status: Verify actual sandbox status with provider (default: False)
            user_id: Filter by user_id (admin only)
            zone_id: Filter by zone_id (admin only)
            agent_id: Filter by agent_id
            status: Filter by status (e.g., 'active', 'stopped', 'paused')

        Returns:
            Dict with list of sandboxes

        Example:
            >>> result = nx.sandbox_list()
            >>> for sb in result['sandboxes']:
            ...     print(f"{sb['name']}: {sb['status']}")
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
        if status is not None:
            params["status"] = status
        result = self._call_rpc("sandbox_list", params)
        return result  # type: ignore[no-any-return]

    def sandbox_status(self, sandbox_id: str, context: dict | None = None) -> dict:
        """Get sandbox status and metadata.

        Args:
            sandbox_id: Sandbox ID
            context: Operation context

        Returns:
            Sandbox metadata dict

        Example:
            >>> result = nx.sandbox_status("sb_123")
            >>> print(f"Uptime: {result['uptime_seconds']}s")
        """
        params: dict[str, Any] = {"sandbox_id": sandbox_id}
        if context is not None:
            params["context"] = context
        result = self._call_rpc("sandbox_status", params)
        return result  # type: ignore[no-any-return]

    def sandbox_get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: dict | None = None,
    ) -> dict:
        """Get existing sandbox or create new one.

        Args:
            name: Sandbox name
            ttl_minutes: Idle timeout in minutes
            provider: Provider name ("docker" or "e2b")
            template_id: Provider template ID
            verify_status: Verify sandbox status with provider
            context: Operation context

        Returns:
            Sandbox metadata dict

        Example:
            >>> result = nx.sandbox_get_or_create("alice,agent1")
            >>> print(f"Sandbox: {result['sandbox_id']}")
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
        return cast(dict, self._call_rpc("sandbox_get_or_create", params))

    def sandbox_disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Disconnect and unmount Nexus from a user-managed sandbox (Issue #371).

        Args:
            sandbox_id: External sandbox ID
            provider: Sandbox provider ("e2b", etc.). Default: "e2b"
            sandbox_api_key: Provider API key for authentication
            context: Operation context

        Returns:
            Dict with disconnection details (sandbox_id, provider, unmounted_at)

        Example:
            >>> result = nx.sandbox_disconnect(
            ...     sandbox_id="sb_xxx",
            ...     sandbox_api_key="your_e2b_key"
            ... )
            >>> print(f"Unmounted at: {result['unmounted_at']}")
        """
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "provider": provider,
        }
        if sandbox_api_key is not None:
            params["sandbox_api_key"] = sandbox_api_key
        if context is not None:
            params["context"] = context
        result = self._call_rpc("sandbox_disconnect", params)
        return result  # type: ignore[no-any-return]

    def sandbox_validate(
        self,
        sandbox_id: str,
        workspace_path: str = "/workspace",
        context: dict | None = None,
    ) -> dict:
        """Run validation pipeline in a sandbox.

        Args:
            sandbox_id: Sandbox ID
            workspace_path: Workspace root path in sandbox
            context: Operation context

        Returns:
            Dict with validations list
        """
        params: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "workspace_path": workspace_path,
        }
        if context is not None:
            params["context"] = context
        result = self._call_rpc("sandbox_validate", params)
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Skills Management Operations
    # ============================================================

    def skills_create(
        self,
        name: str,
        description: str,
        template: str = "basic",
        tier: str = "agent",
        author: str | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Create a new skill from template."""
        params: dict[str, Any] = {
            "name": name,
            "description": description,
            "template": template,
            "tier": tier,
        }
        if author is not None:
            params["author"] = author
        result = self._call_rpc("skills_create", params)
        return result  # type: ignore[no-any-return]

    def skills_create_from_content(
        self,
        name: str,
        description: str,
        content: str,
        tier: str = "agent",
        author: str | None = None,
        source_url: str | None = None,
        metadata: dict[str, Any] | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Create a skill from custom content."""
        params: dict[str, Any] = {
            "name": name,
            "description": description,
            "content": content,
            "tier": tier,
        }
        if author is not None:
            params["author"] = author
        if source_url is not None:
            params["source_url"] = source_url
        if metadata is not None:
            params["metadata"] = metadata
        result = self._call_rpc("skills_create_from_content", params)
        return result  # type: ignore[no-any-return]

    def skills_create_from_file(
        self,
        source: str,
        file_data: str | None = None,
        name: str | None = None,
        description: str | None = None,
        tier: str = "agent",
        use_ai: bool = False,
        use_ocr: bool = False,
        extract_tables: bool = False,
        extract_images: bool = False,
        _author: str | None = None,  # Unused: plugin manages authorship
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Create a skill from file or URL (auto-detects type).

        Args:
            source: File path or URL
            file_data: Base64 encoded file data (for remote calls)
            name: Skill name (auto-generated if not provided)
            description: Skill description
            tier: Target tier (agent, zone, system)
            use_ai: Enable AI enhancement
            use_ocr: Enable OCR for scanned PDFs
            extract_tables: Extract tables from documents
            extract_images: Extract images from documents
            _author: Author name (unused: plugin manages authorship)
        """
        params: dict[str, Any] = {
            "source": source,
            "tier": tier,
            "use_ai": use_ai,
            "use_ocr": use_ocr,
            "extract_tables": extract_tables,
            "extract_images": extract_images,
        }
        if file_data is not None:
            params["file_data"] = file_data
        if name is not None:
            params["name"] = name
        if description is not None:
            params["description"] = description
        if _author is not None:
            params["_author"] = _author
        result = self._call_rpc("skills_create_from_file", params)
        return result  # type: ignore[no-any-return]

    def skills_list(
        self,
        tier: str | None = None,
        include_metadata: bool = True,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """List all skills."""
        params: dict[str, Any] = {"include_metadata": include_metadata}
        if tier is not None:
            params["tier"] = tier
        result = self._call_rpc("skills_list", params)
        return result  # type: ignore[no-any-return]

    def skills_info(
        self,
        skill_name: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Get detailed skill information."""
        result = self._call_rpc("skills_info", {"skill_name": skill_name})
        return result  # type: ignore[no-any-return]

    def skills_fork(
        self,
        source_name: str,
        target_name: str,
        tier: str = "agent",
        author: str | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Fork an existing skill."""
        params: dict[str, Any] = {
            "source_name": source_name,
            "target_name": target_name,
            "tier": tier,
        }
        if author is not None:
            params["author"] = author
        result = self._call_rpc("skills_fork", params)
        return result  # type: ignore[no-any-return]

    def skills_publish(
        self,
        skill_name: str,
        source_tier: str = "agent",
        target_tier: str = "zone",
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Publish skill to another tier."""
        params: dict[str, Any] = {
            "skill_name": skill_name,
            "source_tier": source_tier,
            "target_tier": target_tier,
        }
        result = self._call_rpc("skills_publish", params)
        return result  # type: ignore[no-any-return]

    def skills_search(
        self,
        query: str,
        tier: str | None = None,
        limit: int = 10,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Search skills by description."""
        params: dict[str, Any] = {"query": query, "limit": limit}
        if tier is not None:
            params["tier"] = tier
        result = self._call_rpc("skills_search", params)
        return result  # type: ignore[no-any-return]

    def skills_submit_approval(
        self,
        skill_name: str,
        submitted_by: str,
        reviewers: builtins.list[str] | None = None,
        comments: str | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Submit a skill for approval."""
        params: dict[str, Any] = {
            "skill_name": skill_name,
            "submitted_by": submitted_by,
        }
        if reviewers is not None:
            params["reviewers"] = reviewers
        if comments is not None:
            params["comments"] = comments
        result = self._call_rpc("skills_submit_approval", params)
        return result  # type: ignore[no-any-return]

    def skills_approve(
        self,
        approval_id: str,
        reviewed_by: str,
        reviewer_type: str = "user",
        comments: str | None = None,
        zone_id: str | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Approve a skill for publication."""
        params: dict[str, Any] = {
            "approval_id": approval_id,
            "reviewed_by": reviewed_by,
            "reviewer_type": reviewer_type,
        }
        if comments is not None:
            params["comments"] = comments
        if zone_id is not None:
            params["zone_id"] = zone_id
        result = self._call_rpc("skills_approve", params)
        return result  # type: ignore[no-any-return]

    def skills_reject(
        self,
        approval_id: str,
        reviewed_by: str,
        reviewer_type: str = "user",
        comments: str | None = None,
        zone_id: str | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Reject a skill for publication."""
        params: dict[str, Any] = {
            "approval_id": approval_id,
            "reviewed_by": reviewed_by,
            "reviewer_type": reviewer_type,
        }
        if comments is not None:
            params["comments"] = comments
        if zone_id is not None:
            params["zone_id"] = zone_id
        result = self._call_rpc("skills_reject", params)
        return result  # type: ignore[no-any-return]

    def skills_list_approvals(
        self,
        status: str | None = None,
        skill_name: str | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """List skill approval requests."""
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if skill_name is not None:
            params["skill_name"] = skill_name
        result = self._call_rpc("skills_list_approvals", params)
        return result  # type: ignore[no-any-return]

    def skills_import(
        self,
        zip_data: str,
        tier: str = "user",
        allow_overwrite: bool = False,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Import skill from ZIP package.

        Args:
            zip_data: Base64-encoded ZIP file data
            tier: Target tier ('user', 'agent', 'zone', 'system')
            allow_overwrite: Allow overwriting existing skills
            _context: Operation context (optional)

        Returns:
            Dict with imported_skills, skill_paths, tier
        """
        params: dict[str, Any] = {
            "zip_data": zip_data,
            "tier": tier,
            "allow_overwrite": allow_overwrite,
        }
        result = self._call_rpc("skills_import", params)
        return result  # type: ignore[no-any-return]

    def skills_validate_zip(
        self,
        zip_data: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Validate skill ZIP package without importing.

        Args:
            zip_data: Base64-encoded ZIP file data
            _context: Operation context (optional)

        Returns:
            Dict with valid, skills_found, errors, warnings
        """
        params: dict[str, Any] = {
            "zip_data": zip_data,
        }
        result = self._call_rpc("skills_validate_zip", params)
        return result  # type: ignore[no-any-return]

    def skills_export(
        self,
        skill_name: str,
        format: str = "generic",
        include_dependencies: bool = False,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Export skill to ZIP package.

        Args:
            skill_name: Name of skill to export
            format: Export format ('generic' or 'claude')
            include_dependencies: Include dependent skills
            _context: Operation context (optional)

        Returns:
            Dict with skill_name, zip_data (base64), size_bytes, format
        """
        params: dict[str, Any] = {
            "skill_name": skill_name,
            "format": format,
            "include_dependencies": include_dependencies,
        }
        result = self._call_rpc("skills_export", params)
        return result  # type: ignore[no-any-return]

    def skills_share(
        self,
        skill_path: str,
        share_with: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Share a skill with users, groups, or make public.

        Args:
            skill_path: Path to the skill (e.g., /zone/acme/user/alice/skill/code-review/)
            share_with: Target to share with:
                - "public" - Make skill visible to everyone
                - "zone" - Share with all users in current zone
                - "group:<name>" - Share with a group
                - "user:<id>" - Share with a specific user
                - "agent:<id>" - Share with a specific agent
            _context: Operation context (optional)

        Returns:
            Dict with success, tuple_id, skill_path, share_with
        """
        params: dict[str, Any] = {
            "skill_path": skill_path,
            "share_with": share_with,
        }
        result = self._call_rpc("skills_share", params)
        return result  # type: ignore[no-any-return]

    def skills_unshare(
        self,
        skill_path: str,
        unshare_from: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Revoke sharing permission on a skill.

        Args:
            skill_path: Path to the skill
            unshare_from: Target to unshare from (same format as share_with)
            _context: Operation context (optional)

        Returns:
            Dict with success, skill_path, unshare_from
        """
        params: dict[str, Any] = {
            "skill_path": skill_path,
            "unshare_from": unshare_from,
        }
        result = self._call_rpc("skills_unshare", params)
        return result  # type: ignore[no-any-return]

    def skills_discover(
        self,
        filter: str = "all",
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Discover skills the user has permission to see.

        Args:
            filter: Filter mode:
                - "all" - All skills user can see
                - "public" - Only public skills
                - "subscribed" - Only skills in user's library
                - "owned" - Only skills owned by user
            _context: Operation context (optional)

        Returns:
            Dict with skills list and count
        """
        params: dict[str, Any] = {
            "filter": filter,
        }
        result = self._call_rpc("skills_discover", params)
        return result  # type: ignore[no-any-return]

    def skills_subscribe(
        self,
        skill_path: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Subscribe to a skill (add to user's library).

        Args:
            skill_path: Path to the skill to subscribe to
            _context: Operation context (optional)

        Returns:
            Dict with success, skill_path, already_subscribed
        """
        params: dict[str, Any] = {
            "skill_path": skill_path,
        }
        result = self._call_rpc("skills_subscribe", params)
        return result  # type: ignore[no-any-return]

    def skills_unsubscribe(
        self,
        skill_path: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Unsubscribe from a skill (remove from user's library).

        Args:
            skill_path: Path to the skill to unsubscribe from
            _context: Operation context (optional)

        Returns:
            Dict with success, skill_path, was_subscribed
        """
        params: dict[str, Any] = {
            "skill_path": skill_path,
        }
        result = self._call_rpc("skills_unsubscribe", params)
        return result  # type: ignore[no-any-return]

    def skills_get_prompt_context(
        self,
        max_skills: int = 50,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Get skill metadata for system prompt injection.

        Args:
            max_skills: Maximum number of skills to include (default: 50)
            _context: Operation context (optional)

        Returns:
            Dict with xml, skills, count, token_estimate
        """
        params: dict[str, Any] = {
            "max_skills": max_skills,
        }
        result = self._call_rpc("skills_get_prompt_context", params)
        return result  # type: ignore[no-any-return]

    def skills_load(
        self,
        skill_path: str,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Load full skill content on-demand.

        Args:
            skill_path: Path to the skill to load
            _context: Operation context (optional)

        Returns:
            Dict with name, path, owner, description, content, metadata
        """
        params: dict[str, Any] = {
            "skill_path": skill_path,
        }
        result = self._call_rpc("skills_load", params)
        return result  # type: ignore[no-any-return]

    # ============================================================
    # OAuth Operations
    # ============================================================

    def oauth_list_providers(
        self,
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all available OAuth providers from configuration.

        Args:
            context: Operation context (optional)

        Returns:
            List of provider dictionaries containing:
                - name: Provider identifier (e.g., "google-drive", "gmail")
                - display_name: Human-readable name (e.g., "Google Drive", "Gmail")
                - scopes: List of OAuth scopes required
                - requires_pkce: Whether provider requires PKCE
                - metadata: Additional provider-specific metadata
        """
        params: dict[str, Any] = {}
        if context is not None:
            params["context"] = context
        result = self._call_rpc("oauth_list_providers", params)
        return result  # type: ignore[no-any-return]

    def oauth_get_auth_url(
        self,
        provider: str,
        redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
        scopes: builtins.list[str] | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Get OAuth authorization URL for any provider.

        Args:
            provider: OAuth provider name (e.g., "google-drive", "gmail")
            redirect_uri: OAuth redirect URI (default: "http://localhost:3000/oauth/callback")
            scopes: Optional list of scopes to request
            context: Operation context (optional)

        Returns:
            Dictionary containing:
                - url: Authorization URL to redirect user to
                - state: CSRF state token for validation
                - pkce_data: Optional PKCE data (if provider requires PKCE)
                    - code_verifier: PKCE verifier
                    - code_challenge: PKCE challenge
                    - code_challenge_method: Challenge method (usually "S256")
        """
        params: dict[str, Any] = {
            "provider": provider,
            "redirect_uri": redirect_uri,
        }
        if scopes is not None:
            params["scopes"] = scopes
        if context is not None:
            params["context"] = context
        result = self._call_rpc("oauth_get_auth_url", params)
        return result  # type: ignore[no-any-return]

    def oauth_exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str | None = None,
        state: str | None = None,
        redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
        code_verifier: str | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Exchange OAuth authorization code for tokens and store credentials.

        Args:
            provider: OAuth provider name (e.g., "google")
            code: Authorization code from OAuth callback
            user_email: User email address for credential storage (optional, fetched from provider if not provided)
            state: CSRF state token (optional, for validation)
            redirect_uri: OAuth redirect URI (must match authorization request)
            code_verifier: PKCE code verifier (required for some providers like X/Twitter)
            context: Operation context (optional)

        Returns:
            Dictionary containing:
                - credential_id: Unique credential identifier
                - user_email: User email (from provider if not provided)
                - expires_at: Token expiration timestamp (ISO format)
                - success: True if successful

        Raises:
            RuntimeError: If OAuth credentials not configured
            ValueError: If code exchange fails
        """
        params: dict[str, Any] = {
            "provider": provider,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if user_email is not None:
            params["user_email"] = user_email
        if state is not None:
            params["state"] = state
        if code_verifier is not None:
            params["code_verifier"] = code_verifier
        if context is not None:
            params["context"] = context
        result = self._call_rpc("oauth_exchange_code", params)
        return result  # type: ignore[no-any-return]

    def oauth_list_credentials(
        self,
        provider: str | None = None,
        include_revoked: bool = False,
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all OAuth credentials for the current user.

        Args:
            provider: Optional provider filter (e.g., "google")
            include_revoked: Include revoked credentials (default: False)
            context: Operation context (optional)

        Returns:
            List of credential dictionaries containing:
                - credential_id: Unique identifier
                - provider: OAuth provider name
                - user_email: User email
                - scopes: List of granted scopes
                - expires_at: Token expiration timestamp (ISO format)
                - created_at: Creation timestamp (ISO format)
                - last_used_at: Last usage timestamp (ISO format)
                - revoked: Whether credential is revoked
        """
        params: dict[str, Any] = {"include_revoked": include_revoked}
        if provider is not None:
            params["provider"] = provider
        if context is not None:
            params["context"] = context
        result = self._call_rpc("oauth_list_credentials", params)
        return result  # type: ignore[no-any-return]

    def oauth_revoke_credential(
        self,
        provider: str,
        user_email: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Revoke an OAuth credential.

        Args:
            provider: OAuth provider name (e.g., "google")
            user_email: User email address
            context: Operation context (optional)

        Returns:
            Dictionary containing:
                - success: True if revoked successfully
                - credential_id: Revoked credential ID

        Raises:
            ValueError: If credential not found
        """
        params: dict[str, Any] = {
            "provider": provider,
            "user_email": user_email,
        }
        if context is not None:
            params["context"] = context
        result = self._call_rpc("oauth_revoke_credential", params)
        return result  # type: ignore[no-any-return]

    def oauth_test_credential(
        self,
        provider: str,
        user_email: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Test if an OAuth credential is valid and can be refreshed.

        Args:
            provider: OAuth provider name (e.g., "google")
            user_email: User email address
            context: Operation context (optional)

        Returns:
            Dictionary containing:
                - valid: True if credential is valid
                - refreshed: True if token was refreshed
                - expires_at: Token expiration timestamp (ISO format)
                - error: Error message if invalid

        Raises:
            ValueError: If credential not found
        """
        params: dict[str, Any] = {
            "provider": provider,
            "user_email": user_email,
        }
        if context is not None:
            params["context"] = context
        result = self._call_rpc("oauth_test_credential", params)
        return result  # type: ignore[no-any-return]

    # ============================================================
    # MCP/Klavis Integration
    # ============================================================

    def mcp_connect(
        self,
        provider: str,
        redirect_url: str | None = None,
        user_email: str | None = None,
        reuse_nexus_token: bool = True,
        context: Any = None,
    ) -> dict[str, Any]:
        """Connect to a Klavis MCP server with OAuth support.

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
        result = self._call_rpc("mcp_connect", params)
        return result  # type: ignore[no-any-return]

    def mcp_get_oauth_url(
        self,
        provider: str,
        redirect_url: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Get OAuth URL for a Klavis MCP provider.

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
        result = self._call_rpc("mcp_get_oauth_url", params)
        return result  # type: ignore[no-any-return]

    def mcp_list_mounts(
        self,
        tier: str | None = None,
        include_unmounted: bool = True,
    ) -> builtins.list[dict[str, Any]]:
        """List MCP server mounts.

        Args:
            tier: Filter by tier (user/zone/system)
            include_unmounted: Include unmounted configurations (default: True)

        Returns:
            List of MCP mount info dicts with:
                - name: Mount name
                - description: Mount description
                - transport: Transport type (stdio/sse/klavis)
                - mounted: Whether currently mounted
                - tool_count: Number of discovered tools
                - last_sync: Last sync timestamp (ISO format)
                - tools_path: Path to tools directory

        Examples:
            >>> mounts = nx.mcp_list_mounts()
            >>> for m in mounts:
            ...     print(f"{m['name']}: {m['tool_count']} tools")
        """
        params: dict[str, Any] = {"include_unmounted": include_unmounted}
        if tier is not None:
            params["tier"] = tier
        result = self._call_rpc("mcp_list_mounts", params)
        return result  # type: ignore[no-any-return]

    def mcp_list_tools(self, name: str) -> builtins.list[dict[str, Any]]:
        """List tools from a specific MCP mount.

        Args:
            name: MCP mount name (from mcp_list_mounts)

        Returns:
            List of tool info dicts with:
                - name: Tool name
                - description: Tool description
                - input_schema: JSON schema for tool input

        Examples:
            >>> tools = nx.mcp_list_tools("github")
            >>> for t in tools:
            ...     print(f"{t['name']}: {t['description']}")
        """
        result = self._call_rpc("mcp_list_tools", {"name": name})
        return result  # type: ignore[no-any-return]

    def mcp_mount(
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
            name: Mount name (unique identifier)
            transport: Transport type (stdio/sse/klavis). Auto-detected if not specified.
            command: Command to run MCP server (for stdio transport)
            url: URL of remote MCP server (for sse transport)
            args: Command arguments (for stdio transport)
            env: Environment variables
            headers: HTTP headers (for sse transport)
            description: Mount description
            tier: Target tier (user/zone/system, default: system)

        Returns:
            Dict with mount info:
                - name: Mount name
                - transport: Transport type
                - mounted: Whether successfully mounted
                - tool_count: Number of tools (after sync)

        Examples:
            >>> # Mount local MCP server
            >>> result = nx.mcp_mount(
            ...     name="github",
            ...     command="npx -y @modelcontextprotocol/server-github",
            ...     env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxx"}
            ... )
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
        result = self._call_rpc("mcp_mount", params)
        return result  # type: ignore[no-any-return]

    def mcp_unmount(self, name: str) -> dict[str, Any]:
        """Unmount an MCP server.

        Args:
            name: MCP mount name

        Returns:
            Dict with:
                - success: Whether unmount succeeded
                - name: Mount name

        Examples:
            >>> result = nx.mcp_unmount("github")
            >>> print(result["success"])
        """
        result = self._call_rpc("mcp_unmount", {"name": name})
        return result  # type: ignore[no-any-return]

    def mcp_sync(self, name: str) -> dict[str, Any]:
        """Sync/refresh tools from an MCP server.

        Re-discovers available tools from the mounted MCP server
        and updates the local tool definitions.

        Args:
            name: MCP mount name

        Returns:
            Dict with:
                - name: Mount name
                - tool_count: Number of tools discovered

        Examples:
            >>> result = nx.mcp_sync("github")
            >>> print(f"Synced {result['tool_count']} tools")
        """
        result = self._call_rpc("mcp_sync", {"name": name})
        return result  # type: ignore[no-any-return]

    def backfill_directory_index(
        self,
        prefix: str = "/",
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Backfill sparse directory index from existing files.

        Use this to populate the index for directories that existed before
        the sparse index feature was added. This improves list() performance
        from O(n) LIKE queries to O(1) index lookups.

        Args:
            prefix: Path prefix to backfill (default: "/" for all)
            zone_id: Optional zone filter

        Returns:
            Dict with 'created' count of new index entries
        """
        result = self._call_rpc(
            "backfill_directory_index",
            {"prefix": prefix, "zone_id": zone_id},
        )
        return result  # type: ignore[no-any-return]

    # =========================================================================
    # Share Link Methods
    # =========================================================================

    def create_share_link(
        self,
        path: str,
        permission_level: str = "viewer",
        expires_in_hours: int | None = None,
        max_access_count: int | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Create a share link for a file or directory.

        Args:
            path: Path to share
            permission_level: Access level ("viewer", "editor", "owner")
            expires_in_hours: Optional expiration time in hours
            max_access_count: Optional maximum access count
            password: Optional password protection

        Returns:
            Dict with link_id, share_url, and link details
        """
        result = self._call_rpc(
            "create_share_link",
            {
                "path": path,
                "permission_level": permission_level,
                "expires_in_hours": expires_in_hours,
                "max_access_count": max_access_count,
                "password": password,
            },
        )
        return result  # type: ignore[no-any-return]

    def get_share_link(
        self,
        link_id: str,
    ) -> dict[str, Any]:
        """Get details of a share link.

        Args:
            link_id: The share link ID

        Returns:
            Share link details
        """
        result = self._call_rpc("get_share_link", {"link_id": link_id})
        return result  # type: ignore[no-any-return]

    def list_share_links(
        self,
        path: str | None = None,
        include_revoked: bool = False,
        include_expired: bool = False,
    ) -> dict[str, Any]:
        """List share links created by the current user.

        Args:
            path: Optional filter by path
            include_revoked: Include revoked links
            include_expired: Include expired links

        Returns:
            Dict with count and list of share links
        """
        result = self._call_rpc(
            "list_share_links",
            {
                "path": path,
                "include_revoked": include_revoked,
                "include_expired": include_expired,
            },
        )
        return result  # type: ignore[no-any-return]

    def revoke_share_link(
        self,
        link_id: str,
    ) -> dict[str, Any]:
        """Revoke a share link.

        Args:
            link_id: The share link ID to revoke

        Returns:
            Dict with revoked status
        """
        result = self._call_rpc("revoke_share_link", {"link_id": link_id})
        return result  # type: ignore[no-any-return]

    def access_share_link(
        self,
        link_id: str,
        password: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        """Access a shared resource via share link.

        Args:
            link_id: The share link ID
            password: Password if link is protected
            ip_address: Client IP for logging
            user_agent: Client user agent for logging

        Returns:
            Dict with access_granted, path, permission_level, etc.
        """
        result = self._call_rpc(
            "access_share_link",
            {
                "link_id": link_id,
                "password": password,
                "ip_address": ip_address,
                "user_agent": user_agent,
            },
        )
        return result  # type: ignore[no-any-return]

    def get_share_link_access_logs(
        self,
        link_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Get access logs for a share link.

        Args:
            link_id: The share link ID
            limit: Maximum number of logs to return

        Returns:
            Dict with count and list of access logs
        """
        result = self._call_rpc(
            "get_share_link_access_logs",
            {"link_id": link_id, "limit": limit},
        )
        return result  # type: ignore[no-any-return]

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
    # LLM Methods (delegate to LLMService, not RPC)
    # ============================================================

    @property
    def _llm_service(self) -> LLMService:
        if not hasattr(self, "_llm_service_instance"):
            from nexus.services.llm_service import LLMService as _LLMService

            self._llm_service_instance = _LLMService(nexus_fs=self)
        return self._llm_service_instance

    async def llm_read(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> str:
        return await self._llm_service.llm_read(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
            use_search=use_search,
            search_mode=search_mode,
            provider=provider,
        )

    async def llm_read_detailed(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> Any:
        return await self._llm_service.llm_read_detailed(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
            use_search=use_search,
            search_mode=search_mode,
            provider=provider,
        )

    async def llm_read_stream(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> Any:
        return self._llm_service.llm_read_stream(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
            use_search=use_search,
            search_mode=search_mode,
            provider=provider,
        )

    def create_llm_reader(
        self,
        provider: Any = None,
        model: str | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        max_context_tokens: int = 3000,
    ) -> Any:
        return self._llm_service.create_llm_reader(
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=system_prompt,
            max_context_tokens=max_context_tokens,
        )

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
