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

if TYPE_CHECKING:
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

    def exists(self, path: str) -> bool:
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

    def delete(self, path: str) -> None:
        self._call_rpc("delete", {"path": path})
        self._negative_cache_invalidate(path)

    def delete_bulk(
        self,
        paths: builtins.list[str],
        recursive: bool = False,
    ) -> dict[str, dict]:
        result = self._call_rpc("delete_bulk", {"paths": paths, "recursive": recursive})
        if paths:
            self._negative_cache_invalidate_bulk(paths)
        return result  # type: ignore[no-any-return]

    def rename(self, old_path: str, new_path: str) -> None:
        self._call_rpc("rename", {"old_path": old_path, "new_path": new_path})
        self._negative_cache_invalidate(old_path)

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
