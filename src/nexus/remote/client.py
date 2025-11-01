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
from datetime import timedelta
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
from nexus.core.nexus_fs_llm import NexusFSLLMMixin
from nexus.server.protocol import (
    RPCErrorCode,
    RPCRequest,
    RPCResponse,
    decode_rpc_message,
    encode_rpc_message,
)

logger = logging.getLogger(__name__)


class RemoteMemory:
    """Remote Memory API client.

    Provides the same interface as core.memory_api.Memory but makes RPC calls
    to a remote Nexus server instead of direct database access.
    """

    def __init__(self, remote_fs: RemoteNexusFS):
        """Initialize remote memory client.

        Args:
            remote_fs: RemoteNexusFS instance to use for RPC calls
        """
        self.remote_fs = remote_fs

    # ========== Trajectory Methods ==========

    def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
        _parent_trajectory_id: str | None = None,
        _metadata: dict[str, Any] | None = None,
        _path: str | None = None,
    ) -> str:
        """Start tracking a new execution trajectory.

        Args:
            task_description: Description of the task
            task_type: Optional task type
            _parent_trajectory_id: Optional parent trajectory (not supported in RPC yet)
            _metadata: Additional metadata (not supported in RPC yet)
            _path: Optional path context (not supported in RPC yet)

        Returns:
            trajectory_id: ID of created trajectory
        """
        # Note: RPC method doesn't support _parent_trajectory_id, _metadata, _path yet
        params = {"task_description": task_description}
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
        """Log a step in the trajectory.

        Args:
            trajectory_id: Trajectory ID
            step_type: Step type (action/decision/observation)
            description: Step description
            result: Optional result data
            _metadata: Additional metadata (not supported in RPC yet)
        """
        # Note: RPC method doesn't support _metadata yet
        params = {
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
        """Alias for log_step (for compatibility)."""
        self.log_step(trajectory_id, step_type, description, result, metadata)

    def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        _metrics: dict[str, Any] | None = None,
    ) -> str:
        """Complete a trajectory.

        Args:
            trajectory_id: Trajectory ID
            status: Status (success/failure/partial)
            success_score: Success score 0.0-1.0
            error_message: Optional error message
            _metrics: Performance metrics (not supported in RPC yet)

        Returns:
            trajectory_id: The completed trajectory ID
        """
        # Note: RPC method doesn't support _metrics yet
        params: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "status": status,
        }
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
        """Query execution trajectories.

        Args:
            agent_id: Filter by agent ID
            status: Filter by status
            limit: Maximum results

        Returns:
            List of trajectory dictionaries
        """
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if status is not None:
            params["status"] = status
        if limit != 50:
            params["limit"] = limit
        result = self.remote_fs._call_rpc("query_trajectories", params)
        return result.get("trajectories", [])  # type: ignore[no-any-return]

    # ========== Playbook Methods ==========

    def get_playbook(self, playbook_name: str = "default") -> dict[str, Any] | None:
        """Get agent's playbook.

        Args:
            playbook_name: Playbook name

        Returns:
            Playbook dict or None
        """
        params = {"playbook_name": playbook_name}
        result = self.remote_fs._call_rpc("get_playbook", params)
        return result  # type: ignore[no-any-return]

    def query_playbooks(
        self,
        agent_id: str | None = None,
        scope: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query playbooks.

        Args:
            agent_id: Filter by agent ID
            scope: Filter by scope
            limit: Maximum results

        Returns:
            List of playbook dictionaries
        """
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if scope is not None:
            params["scope"] = scope
        if limit != 50:
            params["limit"] = limit
        result = self.remote_fs._call_rpc("query_playbooks", params)
        return result.get("playbooks", [])  # type: ignore[no-any-return]

    def process_relearning(
        self,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Process trajectories flagged for re-learning.

        Args:
            limit: Maximum number of trajectories to process

        Returns:
            List of re-learning results
        """
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
        """Curate playbook from reflections.

        Args:
            reflection_memory_ids: List of reflection memory IDs
            playbook_name: Playbook name
            merge_threshold: Similarity threshold for merging

        Returns:
            Curation results
        """
        params: dict[str, Any] = {
            "reflection_memory_ids": reflection_memory_ids,
            "playbook_name": playbook_name,
            "merge_threshold": merge_threshold,
        }
        result = self.remote_fs._call_rpc("curate_playbook", params)
        return result  # type: ignore[no-any-return]

    # ========== Reflection Methods ==========

    def batch_reflect(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        min_trajectories: int = 10,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        """Batch reflection across trajectories.

        Args:
            agent_id: Filter by agent ID
            since: ISO timestamp filter
            min_trajectories: Minimum trajectories needed
            task_type: Filter by task type

        Returns:
            Reflection results with common patterns
        """
        params: dict[str, Any] = {"min_trajectories": min_trajectories}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if since is not None:
            params["since"] = since
        if task_type is not None:
            params["task_type"] = task_type
        result = self.remote_fs._call_rpc("batch_reflect", params)
        return result  # type: ignore[no-any-return]

    # ========== Memory Storage Methods ==========

    def store(
        self,
        content: str,
        memory_type: str = "fact",
        scope: str = "agent",
        importance: float = 0.5,
        namespace: str | None = None,
        path_key: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Store a memory.

        Args:
            content: Memory content
            memory_type: Type of memory
            scope: Memory scope
            importance: Importance score
            namespace: Hierarchical namespace (v0.8.0)
            path_key: Optional key for upsert mode (v0.8.0)
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
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """List memories.

        Args:
            scope: Filter by scope
            memory_type: Filter by type
            namespace: Filter by exact namespace (v0.8.0)
            namespace_prefix: Filter by namespace prefix (v0.8.0)
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
        result = self.remote_fs._call_rpc("list_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    def retrieve(
        self,
        namespace: str | None = None,
        path_key: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a memory by namespace path.

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
        result = self.remote_fs._call_rpc("retrieve_memory", params)
        return result.get("memory")  # type: ignore[no-any-return]

    def query(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """Query memories.

        Args:
            memory_type: Filter by type
            scope: Filter by scope
            limit: Maximum results

        Returns:
            List of matching memories
        """
        params: dict[str, Any] = {"limit": limit}
        if memory_type is not None:
            params["memory_type"] = memory_type
        if scope is not None:
            params["scope"] = scope
        result = self.remote_fs._call_rpc("query_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    def delete(self, memory_id: str) -> bool:
        """Delete a memory.

        Args:
            memory_id: Memory ID to delete

        Returns:
            True if deleted, False if not found or no permission
        """
        params: dict[str, Any] = {"memory_id": memory_id}
        result = self.remote_fs._call_rpc("delete_memory", params)
        return result["deleted"]  # type: ignore[no-any-return]


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


class RemoteNexusFS(NexusFSLLMMixin, NexusFilesystem):
    """Remote Nexus filesystem client.

    Implements NexusFilesystem interface by making RPC calls to a remote server.
    Includes LLM-powered document reading capabilities via NexusFSLLMMixin.
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

        # Set agent_id and tenant_id (required by NexusFilesystem protocol)
        self._agent_id: str | None = None
        self._tenant_id: str | None = None

        # Initialize semantic search as None (remote clients don't have local search)
        # LLM mixin will check this and fall back to direct file reading
        self._semantic_search = None

        # Initialize memory API as None (lazy initialization)
        self._memory_api: RemoteMemory | None = None

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

            # Fetch authenticated user info to get tenant_id
            try:
                self._fetch_auth_info()
            except Exception as e:
                logger.warning(f"Failed to fetch auth info: {e}")
                # Don't fail initialization, just log warning

    @property
    def agent_id(self) -> str | None:
        """Agent ID for this filesystem instance."""
        return self._agent_id

    @agent_id.setter
    def agent_id(self, value: str | None) -> None:
        """Set agent ID for this filesystem instance."""
        self._agent_id = value

    @property
    def tenant_id(self) -> str | None:
        """Tenant ID for this filesystem instance."""
        return self._tenant_id

    @tenant_id.setter
    def tenant_id(self, value: str | None) -> None:
        """Set tenant ID for this filesystem instance."""
        self._tenant_id = value

    def _fetch_auth_info(self) -> None:
        """Fetch authenticated user info from server.

        This populates self.tenant_id, self.agent_id, and other auth metadata
        from the server's /api/auth/whoami endpoint.
        """
        try:
            response = self.session.get(
                urljoin(self.server_url, "/api/auth/whoami"), timeout=self.connect_timeout
            )

            if response.status_code == 200:
                auth_info = response.json()
                if auth_info.get("authenticated"):
                    self.tenant_id = auth_info.get("tenant_id")
                    # BUGFIX: Only set agent_id if subject_type is "agent"
                    # For users, agent_id should remain None
                    subject_type = auth_info.get("subject_type")
                    if subject_type == "agent":
                        self.agent_id = auth_info.get("subject_id")
                    else:
                        self.agent_id = None
                    logger.info(
                        f"Authenticated as {subject_type}:{auth_info.get('subject_id')} "
                        f"(tenant: {self.tenant_id})"
                    )
                else:
                    logger.debug("Not authenticated (anonymous access)")
            else:
                logger.warning(f"Failed to fetch auth info: HTTP {response.status_code}")
        except Exception as e:
            logger.debug(f"Could not fetch auth info: {e}")
            raise

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
            # Build headers
            headers = {"Content-Type": "application/json"}

            # Add agent identity header if set (for permission checks)
            if self.agent_id:
                headers["X-Agent-ID"] = self.agent_id

            # Add tenant identity header if set
            if self.tenant_id:
                headers["X-Tenant-ID"] = self.tenant_id

            # Use tuple for timeout: (connect_timeout, read_timeout)
            response = self.session.post(
                url,
                data=body,
                headers=headers,
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
            return_metadata: If True, return dict with content and metadata

        Returns:
            If return_metadata=False: File content as bytes
            If return_metadata=True: Dict with content, etag, version, etc.
        """
        result = self._call_rpc("read", {"path": path, "return_metadata": return_metadata})
        return result  # type: ignore[no-any-return]

    def stream(self, path: str, chunk_size: int = 8192, context: Any = None) -> Any:  # noqa: ARG002
        """Stream file content in chunks.

        Note: Streaming over RPC is not efficient. This method reads the entire
        file and yields it in chunks. For true streaming, use direct file access.

        Args:
            path: Virtual path to stream
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Unused in remote client (handled server-side)

        Yields:
            bytes: Chunks of file content
        """
        # Read entire file (RPC doesn't support true streaming)
        content = self.read(path)
        assert isinstance(content, bytes), "Expected bytes from read()"

        # Yield in chunks
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    def write(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,  # noqa: ARG002
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Write content to a file with optional optimistic concurrency control.

        Args:
            path: Virtual path to write
            content: File content as bytes or str (str will be UTF-8 encoded)
            context: Unused in remote client (handled server-side)
            if_match: Optional etag for optimistic concurrency control
            if_none_match: If True, create-only mode
            force: If True, skip version check

        Returns:
            Dict with metadata (etag, version, modified_at, size)

        Raises:
            ConflictError: If if_match doesn't match current etag
        """
        # Auto-convert str to bytes for convenience
        if isinstance(content, str):
            content = content.encode("utf-8")

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

    def write_batch(
        self,
        files: list[tuple[str, bytes]],
        context: Any = None,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """Write multiple files in a single transaction.

        Args:
            files: List of (path, content) tuples to write
            context: Unused in remote client (handled server-side)

        Returns:
            List of metadata dicts for each file
        """
        result = self._call_rpc(
            "write_batch",
            {
                "files": files,
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
        show_parsed: bool = True,
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List files in a directory."""
        # Note: context is provided via authentication headers, not RPC params
        result = self._call_rpc(
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

    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:  # noqa: ARG002
        """Find files matching a glob pattern."""
        # Note: context is provided via authentication headers, not RPC params
        result = self._call_rpc("glob", {"pattern": pattern, "path": path})
        return result["matches"]  # type: ignore[no-any-return]

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",  # noqa: ARG002
        context: Any = None,  # noqa: ARG002
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex patterns."""
        # Note: context is provided via authentication headers, not RPC params
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

    def get_metadata(self, path: str) -> dict[str, Any] | None:
        """Get file metadata (permissions, ownership, etc.).

        This method retrieves metadata for FUSE operations without reading
        the entire file content.

        Args:
            path: Virtual file path

        Returns:
            Metadata dict with keys: path, owner, group, mode, is_directory
            Returns None if file doesn't exist or server has no metadata

        Examples:
            >>> metadata = nx.get_metadata("/workspace/file.txt")
            >>> print(f"Mode: {metadata['mode']:o}")  # e.g., 0o644
        """
        result = self._call_rpc("get_metadata", {"path": path})
        return result.get("metadata")  # type: ignore[no-any-return]

    # ============================================================
    # Version Tracking Operations
    # ============================================================

    def get_version(self, path: str, version: int) -> bytes:
        """Get a specific version of a file."""
        result = self._call_rpc("get_version", {"path": path, "version": version})
        return result  # type: ignore[no-any-return]

    def list_versions(self, path: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a file."""
        result = self._call_rpc("list_versions", {"path": path})
        return result  # type: ignore[no-any-return]

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
    # Workspace Versioning
    # ============================================================

    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        agent_id: str | None = None,
        description: str | None = None,
        tags: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of the current agent's workspace.

        Args:
            workspace_path: Path to registered workspace
            agent_id: Agent identifier (uses default if not provided)
            description: Human-readable description of snapshot
            tags: List of tags for categorization

        Returns:
            Snapshot metadata dict

        Raises:
            ValueError: If agent_id not provided and no default set
            BackendError: If snapshot cannot be created
        """
        result = self._call_rpc(
            "workspace_snapshot",
            {
                "workspace_path": workspace_path,
                "agent_id": agent_id,
                "description": description,
                "tags": tags,
            },
        )
        return result  # type: ignore[no-any-return]

    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_number: Snapshot version number to restore
            workspace_path: Path to registered workspace
            agent_id: Agent identifier (uses default if not provided)

        Returns:
            Restore operation result

        Raises:
            ValueError: If agent_id not provided and no default set
            NexusFileNotFoundError: If snapshot not found
        """
        result = self._call_rpc(
            "workspace_restore",
            {
                "snapshot_number": snapshot_number,
                "workspace_path": workspace_path,
                "agent_id": agent_id,
            },
        )
        return result  # type: ignore[no-any-return]

    def workspace_log(
        self,
        workspace_path: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        """List snapshot history for workspace.

        Args:
            workspace_path: Path to registered workspace
            agent_id: Agent identifier (uses default if not provided)
            limit: Maximum number of snapshots to return

        Returns:
            List of snapshot metadata dicts (most recent first)

        Raises:
            ValueError: If agent_id not provided and no default set
        """
        result = self._call_rpc(
            "workspace_log",
            {"workspace_path": workspace_path, "agent_id": agent_id, "limit": limit},
        )
        return result  # type: ignore[no-any-return]

    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare two workspace snapshots.

        Args:
            snapshot_1: First snapshot number
            snapshot_2: Second snapshot number
            workspace_path: Path to registered workspace
            agent_id: Agent identifier (uses default if not provided)

        Returns:
            Diff dict with added, removed, modified files

        Raises:
            ValueError: If agent_id not provided and no default set
            NexusFileNotFoundError: If either snapshot not found
        """
        result = self._call_rpc(
            "workspace_diff",
            {
                "snapshot_1": snapshot_1,
                "snapshot_2": snapshot_2,
                "workspace_path": workspace_path,
                "agent_id": agent_id,
            },
        )
        return result  # type: ignore[no-any-return]

    # ============================================================
    # DEPRECATED: Legacy Permission Operations
    # ============================================================
    # These methods are no longer supported.
    # Use rebac_create(), rebac_check(), and rebac_delete() instead.

    def chmod(self, path: str, mode: int | str, context: Any = None) -> None:  # noqa: ARG002
        """DEPRECATED: Change file permissions (no longer supported).

        This method has been removed. Use ReBAC permissions instead.

        Migration:
            Use rebac_create() to grant permissions:

            >>> nx.rebac_create(
            ...     subject=("user", "alice"),
            ...     relation="owner",
            ...     object=("file", path)
            ... )

        Raises:
            NotImplementedError: Always

        See:
            - rebac_create(): Create permission relationships
            - rebac_check(): Check permissions
        """
        raise NotImplementedError(
            "chmod() is no longer supported. Use ReBAC instead:\n"
            "  nx.rebac_create(subject=('user', 'alice'), relation='owner', object=('file', path))"
        )

    def chown(self, path: str, owner: str, context: Any = None) -> None:  # noqa: ARG002
        """DEPRECATED: Change file owner (no longer supported).

        This method has been removed. Use ReBAC permissions instead.

        Migration:
            Use rebac_create() to set ownership:

            >>> nx.rebac_create(
            ...     subject=("user", "alice"),
            ...     relation="owner",
            ...     object=("file", path)
            ... )

        Raises:
            NotImplementedError: Always

        See:
            - rebac_create(): Create permission relationships
        """
        raise NotImplementedError(
            "chown() is no longer supported. Use ReBAC instead:\n"
            f"  nx.rebac_create(subject=('user', '{owner}'), relation='owner', object=('file', '{path}'))"
        )

    def chgrp(self, path: str, group: str, context: Any = None) -> None:  # noqa: ARG002
        """DEPRECATED: Change file group (no longer supported).

        This method has been removed. Use ReBAC permissions instead.

        Migration:
            Use rebac_create() to grant group permissions:

            >>> nx.rebac_create(
            ...     subject=("group", "developers"),
            ...     relation="can-write",
            ...     object=("file", path)
            ... )

        Raises:
            NotImplementedError: Always

        See:
            - rebac_create(): Create permission relationships
        """
        raise NotImplementedError(
            "chgrp() is no longer supported. Use ReBAC instead:\n"
            f"  nx.rebac_create(subject=('group', '{group}'), relation='can-write', object=('file', '{path}'))"
        )

    # ============================================================
    # DEPRECATED: ACL Operations
    # ============================================================

    def grant_user(
        self,
        path: str,
        user: str,
        permissions: str,
        context: Any = None,  # noqa: ARG002
    ) -> None:
        """DEPRECATED: Grant ACL permissions (no longer supported).

        This method has been removed. Use ReBAC permissions instead.

        Migration:
            Use rebac_create() to grant permissions:

            >>> nx.rebac_create(
            ...     subject=("user", "bob"),
            ...     relation="can-write",  # or "can-read" for read-only
            ...     object=("file", path)
            ... )

        Raises:
            NotImplementedError: Always

        See:
            - rebac_create(): Create permission relationships
        """
        raise NotImplementedError(
            "grant_user() is no longer supported. Use ReBAC instead:\n"
            f"  nx.rebac_create(subject=('user', '{user}'), relation='can-write', object=('file', '{path}'))"
        )

    def grant_group(
        self,
        path: str,
        group: str,
        permissions: str,
        context: Any = None,  # noqa: ARG002
    ) -> None:
        """DEPRECATED: Grant ACL permissions to group (no longer supported).

        This method has been removed. Use ReBAC permissions instead.

        Migration:
            Use rebac_create() to grant group permissions:

            >>> nx.rebac_create(
            ...     subject=("group", "developers"),
            ...     relation="can-write",
            ...     object=("file", path)
            ... )

        Raises:
            NotImplementedError: Always

        See:
            - rebac_create(): Create permission relationships
        """
        raise NotImplementedError(
            "grant_group() is no longer supported. Use ReBAC instead:\n"
            f"  nx.rebac_create(subject=('group', '{group}'), relation='can-write', object=('file', '{path}'))"
        )

    def deny_user(
        self,
        path: str,
        user: str,
        context: Any = None,  # noqa: ARG002
    ) -> None:
        """DEPRECATED: Deny ACL access (no longer supported).

        This method has been removed. ReBAC uses positive permissions only.

        Migration:
            Instead of denying access, don't grant it. Use rebac_delete() to remove existing permissions:

            >>> tuples = nx.rebac_list_tuples(
            ...     subject=("user", "intern"),
            ...     object=("file", path)
            ... )
            >>> for t in tuples:
            ...     nx.rebac_delete(t['tuple_id'])

        Raises:
            NotImplementedError: Always

        See:
            - rebac_delete(): Remove permission relationships
            - rebac_list_tuples(): Find existing permissions
        """
        raise NotImplementedError(
            "deny_user() is no longer supported. ReBAC uses positive permissions only.\n"
            "Use rebac_list_tuples() and rebac_delete() to remove access."
        )

    def revoke_acl(
        self,
        path: str,
        entry_type: str,
        identifier: str,
        context: Any = None,  # noqa: ARG002
    ) -> None:
        """DEPRECATED: Revoke ACL entry (no longer supported).

        This method has been removed. Use ReBAC permissions instead.

        Migration:
            Use rebac_list_tuples() to find permissions, then rebac_delete() to remove them:

            >>> tuples = nx.rebac_list_tuples(
            ...     subject=("user", "alice"),
            ...     object=("file", path)
            ... )
            >>> for t in tuples:
            ...     nx.rebac_delete(t['tuple_id'])

        Raises:
            NotImplementedError: Always

        See:
            - rebac_list_tuples(): Find permission relationships
            - rebac_delete(): Remove relationships
        """
        raise NotImplementedError(
            "revoke_acl() is no longer supported. Use ReBAC instead:\n"
            f"  tuples = nx.rebac_list_tuples(subject=('{entry_type}', '{identifier}'), object=('file', '{path}'))\n"
            "  for t in tuples: nx.rebac_delete(t['tuple_id'])"
        )

    def get_acl(self, path: str) -> builtins.list[dict[str, str | bool | None]]:
        """DEPRECATED: Get ACL entries (no longer supported).

        This method has been removed. Use ReBAC permissions instead.

        Migration:
            Use rebac_list_tuples() to list permissions:

            >>> tuples = nx.rebac_list_tuples(
            ...     object=("file", path)
            ... )

        Raises:
            NotImplementedError: Always

        See:
            - rebac_list_tuples(): List permission relationships
            - rebac_expand(): Find all subjects with a permission
        """
        raise NotImplementedError(
            "get_acl() is no longer supported. Use ReBAC instead:\n"
            f"  nx.rebac_list_tuples(object=('file', '{path}'))"
        )

    # ============================================================
    # Batch Operations
    # ============================================================

    def batch_get_content_ids(self, paths: builtins.list[str]) -> dict[str, str | None]:
        """Get content IDs (hashes) for multiple paths in a single query.

        Args:
            paths: List of virtual file paths

        Returns:
            Dictionary mapping path to content_hash (or None if file not found)
        """
        result = self._call_rpc("batch_get_content_ids", {"paths": paths})
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Metadata Export/Import
    # ============================================================

    def export_metadata(
        self,
        output_path: str,
        filter: Any = None,
        prefix: str = "",
    ) -> int:
        """Export metadata to JSONL file.

        NOTE: This method is not fully supported in remote mode as it requires
        writing to the server's local filesystem. Consider using direct database
        access for production metadata exports.

        Args:
            output_path: Path to output JSONL file (server-side path)
            filter: Export filter options
            prefix: Path prefix filter for backward compatibility

        Returns:
            Number of files exported
        """
        result = self._call_rpc(
            "export_metadata",
            {"output_path": output_path, "filter": filter, "prefix": prefix},
        )
        return result  # type: ignore[no-any-return]

    def import_metadata(
        self,
        input_path: str,
        options: Any = None,
        overwrite: bool = False,
        skip_existing: bool = True,
    ) -> dict[str, Any]:
        """Import metadata from JSONL file.

        NOTE: This method is not fully supported in remote mode as it requires
        reading from the server's local filesystem. Consider using direct database
        access for production metadata imports.

        Args:
            input_path: Path to input JSONL file (server-side path)
            options: Import options
            overwrite: If True, overwrite existing
            skip_existing: If True, skip existing

        Returns:
            ImportResult dict with counts and collision details
        """
        result = self._call_rpc(
            "import_metadata",
            {
                "input_path": input_path,
                "options": options,
                "overwrite": overwrite,
                "skip_existing": skip_existing,
            },
        )
        return result  # type: ignore[no-any-return]

    # ============================================================
    # ReBAC (Relationship-Based Access Control)
    # ============================================================

    def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: Any = None,
        tenant_id: str | None = None,  # Auto-filled from auth if None
    ) -> str:
        """Create a ReBAC relationship tuple.

        Args:
            subject: (subject_type, subject_id) tuple (e.g., ('agent', 'alice'))
            relation: Relation type (e.g., 'member-of', 'owner-of')
            object: (object_type, object_id) tuple (e.g., ('group', 'developers'))
            expires_at: Optional expiration datetime for temporary relationships
            tenant_id: Optional tenant ID for multi-tenant isolation. If None, uses
                       tenant_id from authenticated user's credentials.

        Returns:
            Tuple ID of created relationship

        Examples:
            >>> nx.rebac_create(
            ...     subject=("agent", "alice"),
            ...     relation="member-of",
            ...     object=("group", "developers")
            ... )
            'uuid-string'
        """
        # Use tenant_id from auth if not specified
        effective_tenant_id = tenant_id if tenant_id is not None else self.tenant_id

        result = self._call_rpc(
            "rebac_create",
            {
                "subject": subject,
                "relation": relation,
                "object": object,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "tenant_id": effective_tenant_id,
            },
        )
        return result  # type: ignore[no-any-return]

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        tenant_id: str | None = None,  # Auto-filled from auth if None
    ) -> bool:
        """Check if subject has permission on object via ReBAC.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write', 'owner')
            object: (object_type, object_id) tuple
            tenant_id: Optional tenant ID for multi-tenant isolation. If None, uses
                       tenant_id from authenticated user's credentials.

        Returns:
            True if permission is granted, False otherwise

        Examples:
            >>> nx.rebac_check(
            ...     subject=("agent", "alice"),
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt"),
            ...     tenant_id="default"
            ... )
            True
        """
        # Use tenant_id from auth if not specified
        effective_tenant_id = tenant_id if tenant_id is not None else self.tenant_id

        result = self._call_rpc(
            "rebac_check",
            {
                "subject": subject,
                "permission": permission,
                "object": object,
                "tenant_id": effective_tenant_id,
            },
        )
        return result  # type: ignore[no-any-return]

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> builtins.list[tuple[str, str]]:
        """Find all subjects with a given permission on an object.

        Args:
            permission: Permission to check (e.g., 'read', 'write', 'owner')
            object: (object_type, object_id) tuple

        Returns:
            List of (subject_type, subject_id) tuples

        Examples:
            >>> nx.rebac_expand(
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt")
            ... )
            [('agent', 'alice'), ('agent', 'bob')]
        """
        result = self._call_rpc("rebac_expand", {"permission": permission, "object": object})
        # Convert list of lists back to list of tuples
        return [tuple(item) for item in result]

    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a ReBAC relationship tuple.

        Args:
            tuple_id: ID of the tuple to delete

        Returns:
            True if tuple was deleted, False if not found

        Examples:
            >>> nx.rebac_delete('uuid-string')
            True
        """
        result = self._call_rpc("rebac_delete", {"tuple_id": tuple_id})
        return result  # type: ignore[no-any-return]

    def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List ReBAC relationship tuples matching filters.

        Args:
            subject: Optional (subject_type, subject_id) filter
            relation: Optional relation type filter
            object: Optional (object_type, object_id) filter

        Returns:
            List of tuple dictionaries

        Examples:
            >>> nx.rebac_list_tuples(subject=("agent", "alice"))
            [{'tuple_id': '...', 'subject_type': 'agent', ...}]
        """
        result = self._call_rpc(
            "rebac_list_tuples",
            {"subject": subject, "relation": relation, "object": object},
        )
        return result  # type: ignore[no-any-return]

    def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        tenant_id: str | None = None,  # Auto-filled from auth if None
    ) -> dict[str, Any]:
        """Explain why a subject has or doesn't have permission on an object.

        This debugging API traces through the permission graph to show exactly
        why a permission check succeeded or failed.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write', 'owner')
            object: (object_type, object_id) tuple
            tenant_id: Optional tenant ID for multi-tenant isolation. If None, uses
                       tenant_id from authenticated user's credentials.

        Returns:
            Dictionary with:
                - result: bool - whether permission is granted
                - cached: bool - whether result came from cache
                - reason: str - human-readable explanation
                - paths: list[dict] - all checked paths through the graph
                - successful_path: dict | None - the path that granted access (if any)

        Examples:
            >>> # Why does alice have read permission?
            >>> explanation = nx.rebac_explain(
            ...     subject=("agent", "alice"),
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt")
            ... )
            >>> print(explanation["reason"])
        """
        # Use tenant_id from auth if not specified
        effective_tenant_id = tenant_id if tenant_id is not None else self.tenant_id

        result = self._call_rpc(
            "rebac_explain",
            {
                "subject": subject,
                "permission": permission,
                "object": object,
                "tenant_id": effective_tenant_id,
            },
        )
        return result  # type: ignore[no-any-return]

    def rebac_check_batch(
        self,
        checks: builtins.list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> builtins.list[bool]:
        """Batch permission checks for efficiency.

        Performs multiple permission checks in a single call, using shared cache lookups
        and optimized database queries. More efficient than individual checks when checking
        multiple permissions.

        Args:
            checks: List of (subject, permission, object) tuples to check

        Returns:
            List of boolean results in the same order as input

        Examples:
            >>> # Check multiple permissions at once
            >>> results = nx.rebac_check_batch([
            ...     (("agent", "alice"), "read", ("file", "/workspace/doc1.txt")),
            ...     (("agent", "alice"), "read", ("file", "/workspace/doc2.txt")),
            ...     (("agent", "bob"), "write", ("file", "/workspace/doc3.txt")),
            ... ])
            >>> # Returns: [True, False, True]
        """
        result = self._call_rpc("rebac_check_batch", {"checks": checks})
        return result  # type: ignore[no-any-return]

    def rebac_expand_with_privacy(
        self,
        permission: str,
        object: tuple[str, str],
        respect_consent: bool = True,
        requester: tuple[str, str] | None = None,
    ) -> builtins.list[tuple[str, str]]:
        """Find subjects with permission, optionally filtering by consent.

        This enables privacy-aware queries where subjects who haven't granted
        consent are filtered from results.

        Args:
            permission: Permission to check
            object: Object to expand on
            respect_consent: Filter results by consent/public_discoverable
            requester: Who is requesting (for consent checks)

        Returns:
            List of subjects, potentially filtered by privacy

        Examples:
            >>> # Standard expand (no privacy filtering)
            >>> viewers = nx.rebac_expand_with_privacy(
            ...     "view",
            ...     ("file", "/doc.txt"),
            ...     respect_consent=False
            ... )
            >>>
            >>> # Privacy-aware expand
            >>> discoverable_viewers = nx.rebac_expand_with_privacy(
            ...     "view",
            ...     ("workspace", "/project"),
            ...     respect_consent=True,
            ...     requester=("user", "alice")
            ... )
        """
        result = self._call_rpc(
            "rebac_expand_with_privacy",
            {
                "permission": permission,
                "object": object,
                "respect_consent": respect_consent,
                "requester": requester,
            },
        )
        # Convert list of lists back to list of tuples
        return [tuple(item) for item in result]

    def get_rebac_option(self, key: str) -> Any:
        """Get a ReBAC configuration option.

        Args:
            key: Configuration key (e.g., "max_depth", "cache_ttl")

        Returns:
            Current value of the configuration option

        Raises:
            ValueError: If key is invalid
            RemoteFilesystemError: If ReBAC is not available

        Examples:
            >>> # Get current max depth
            >>> depth = nx.get_rebac_option("max_depth")
            >>> print(f"Max traversal depth: {depth}")
        """
        result = self._call_rpc("get_rebac_option", {"key": key})
        return result

    def set_rebac_option(self, key: str, value: Any) -> None:
        """Set a ReBAC configuration option.

        Provides public access to ReBAC configuration without using internal APIs.

        Args:
            key: Configuration key (e.g., "max_depth", "cache_ttl")
            value: Configuration value

        Raises:
            ValueError: If key is invalid
            RemoteFilesystemError: If ReBAC is not available

        Examples:
            >>> # Set maximum graph traversal depth
            >>> nx.set_rebac_option("max_depth", 15)
            >>>
            >>> # Set cache TTL
            >>> nx.set_rebac_option("cache_ttl", 600)
        """
        self._call_rpc("set_rebac_option", {"key": key, "value": value})

    # ============================================================
    # Namespace Management
    # ============================================================

    def register_namespace(self, namespace: dict[str, Any]) -> None:
        """Register a namespace schema for ReBAC.

        Provides public API to register namespace configurations without using internal APIs.

        Args:
            namespace: Namespace configuration dictionary with keys:
                - object_type: Type of objects this namespace applies to
                - config: Schema configuration (relations and permissions)

        Raises:
            RemoteFilesystemError: If ReBAC is not available
            ValueError: If namespace configuration is invalid

        Examples:
            >>> # Register file namespace with group inheritance
            >>> nx.register_namespace({
            ...     "object_type": "file",
            ...     "config": {
            ...         "relations": {
            ...             "viewer": {},
            ...             "editor": {}
            ...         },
            ...         "permissions": {
            ...             "read": ["viewer", "editor"],
            ...             "write": ["editor"]
            ...         }
            ...     }
            ... })
        """
        self._call_rpc("register_namespace", {"namespace": namespace})

    def get_namespace(self, object_type: str) -> dict[str, Any] | None:
        """Get namespace schema for an object type.

        Args:
            object_type: Type of objects (e.g., "file", "group")

        Returns:
            Namespace configuration dict or None if not found

        Raises:
            RemoteFilesystemError: If ReBAC is not available

        Examples:
            >>> # Get file namespace
            >>> ns = nx.get_namespace("file")
            >>> if ns:
            ...     print(f"Relations: {ns['config']['relations'].keys()}")
        """
        result = self._call_rpc("get_namespace", {"object_type": object_type})
        return result  # type: ignore[no-any-return]

    def namespace_create(self, object_type: str, config: dict[str, Any]) -> None:
        """Create or update a namespace configuration.

        Args:
            object_type: Type of objects this namespace applies to (e.g., "document", "project")
            config: Namespace configuration with "relations" and "permissions" keys

        Raises:
            RemoteFilesystemError: If ReBAC is not available
            ValueError: If configuration is invalid

        Examples:
            >>> # Create custom document namespace
            >>> nx.namespace_create("document", {
            ...     "relations": {
            ...         "owner": {},
            ...         "editor": {},
            ...         "viewer": {"union": ["editor", "owner"]}
            ...     },
            ...     "permissions": {
            ...         "read": ["viewer", "editor", "owner"],
            ...         "write": ["editor", "owner"]
            ...     }
            ... })
        """
        self._call_rpc("namespace_create", {"object_type": object_type, "config": config})

    def namespace_list(self) -> builtins.list[dict[str, Any]]:
        """List all registered namespace configurations.

        Returns:
            List of namespace dictionaries with metadata and config

        Raises:
            RemoteFilesystemError: If ReBAC is not available

        Examples:
            >>> # List all namespaces
            >>> namespaces = nx.namespace_list()
            >>> for ns in namespaces:
            ...     print(f"{ns['object_type']}: {list(ns['config']['relations'].keys())}")
        """
        result = self._call_rpc("namespace_list", {})
        return result  # type: ignore[no-any-return]

    def namespace_delete(self, object_type: str) -> bool:
        """Delete a namespace configuration.

        Args:
            object_type: Type of objects to remove namespace for

        Returns:
            True if namespace was deleted, False if not found

        Raises:
            RemoteFilesystemError: If ReBAC is not available

        Examples:
            >>> # Delete custom namespace
            >>> nx.namespace_delete("document")
            True
        """
        result = self._call_rpc("namespace_delete", {"object_type": object_type})
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Privacy and Consent
    # ============================================================

    def grant_consent(
        self,
        from_subject: tuple[str, str],
        to_subject: tuple[str, str],
        expires_at: Any = None,
        tenant_id: str | None = None,
    ) -> str:
        """Grant consent for one subject to discover another (privacy/consent management).

        Args:
            from_subject: Who is granting consent (e.g., ("profile", "alice"))
            to_subject: Who can now discover (e.g., ("user", "bob"))
            expires_at: Optional expiration datetime for temporary consent
            tenant_id: Optional tenant ID for multi-tenant isolation

        Returns:
            Tuple ID of the consent relationship

        Examples:
            >>> # Alice grants Bob permanent consent to discover her profile
            >>> consent_id = nx.grant_consent(
            ...     from_subject=("profile", "alice"),
            ...     to_subject=("user", "bob")
            ... )
        """
        result = self._call_rpc(
            "grant_consent",
            {
                "from_subject": from_subject,
                "to_subject": to_subject,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "tenant_id": tenant_id,
            },
        )
        return result  # type: ignore[no-any-return]

    def revoke_consent(self, from_subject: tuple[str, str], to_subject: tuple[str, str]) -> bool:
        """Revoke previously granted consent.

        Args:
            from_subject: Who is revoking consent
            to_subject: Who loses discovery access

        Returns:
            True if consent was revoked, False if no consent existed

        Examples:
            >>> # Revoke Bob's consent to see Alice's profile
            >>> revoked = nx.revoke_consent(
            ...     from_subject=("profile", "alice"),
            ...     to_subject=("user", "bob")
            ... )
        """
        result = self._call_rpc(
            "revoke_consent", {"from_subject": from_subject, "to_subject": to_subject}
        )
        return result  # type: ignore[no-any-return]

    def make_public(self, resource: tuple[str, str], tenant_id: str | None = None) -> str:
        """Make a resource publicly discoverable (anyone can discover it without consent).

        Args:
            resource: Resource to make public (e.g., ("profile", "alice"))
            tenant_id: Optional tenant ID for multi-tenant isolation

        Returns:
            Tuple ID of the public relationship

        Examples:
            >>> # Make Alice's profile publicly discoverable
            >>> public_id = nx.make_public(("profile", "alice"))
        """
        result = self._call_rpc("make_public", {"resource": resource, "tenant_id": tenant_id})
        return result  # type: ignore[no-any-return]

    def make_private(self, resource: tuple[str, str]) -> bool:
        """Remove public discoverability from a resource.

        Args:
            resource: Resource to make private

        Returns:
            True if public access was removed, False if resource wasn't public

        Examples:
            >>> # Make profile private again
            >>> made_private = nx.make_private(("profile", "alice"))
        """
        result = self._call_rpc("make_private", {"resource": resource})
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Mount Management
    # ============================================================

    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
    ) -> str:
        """Add a dynamic backend mount to the filesystem.

        This adds a backend mount at runtime without requiring server restart.
        Useful for user-specific storage, temporary backends, or multi-tenant scenarios.

        Args:
            mount_point: Virtual path where backend is mounted (e.g., "/personal/alice")
            backend_type: Backend type - "local", "gcs", "google_drive", etc.
            backend_config: Backend-specific configuration dict
            priority: Mount priority - higher values take precedence (default: 0)
            readonly: Whether mount is read-only (default: False)

        Returns:
            Mount ID (unique identifier for this mount)

        Raises:
            ValueError: If mount_point already exists or configuration is invalid
            RemoteFilesystemError: If backend type is not supported

        Examples:
            >>> # Add personal GCS mount
            >>> mount_id = nx.add_mount(
            ...     mount_point="/personal/alice",
            ...     backend_type="gcs",
            ...     backend_config={
            ...         "bucket": "alice-personal-bucket",
            ...         "project_id": "my-project"
            ...     },
            ...     priority=10
            ... )
        """
        result = self._call_rpc(
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

    def remove_mount(self, mount_point: str) -> bool:
        """Remove a backend mount from the filesystem.

        Args:
            mount_point: Virtual path of mount to remove (e.g., "/personal/alice")

        Returns:
            True if mount was removed, False if mount not found

        Examples:
            >>> # Remove user's personal mount
            >>> if nx.remove_mount("/personal/alice"):
            ...     print("Mount removed successfully")
        """
        result = self._call_rpc("remove_mount", {"mount_point": mount_point})
        return result  # type: ignore[no-any-return]

    def list_mounts(self) -> builtins.list[dict[str, Any]]:
        """List all active backend mounts.

        Returns:
            List of mount info dictionaries, each containing:
                - mount_point: Virtual path (str)
                - priority: Mount priority (int)
                - readonly: Read-only flag (bool)
                - backend_type: Backend type name (str)

        Examples:
            >>> # List all mounts
            >>> for mount in nx.list_mounts():
            ...     print(f"{mount['mount_point']} (priority={mount['priority']})")
        """
        result = self._call_rpc("list_mounts", {})
        return result  # type: ignore[no-any-return]

    def get_mount(self, mount_point: str) -> dict[str, Any] | None:
        """Get details about a specific mount.

        Args:
            mount_point: Virtual path of mount (e.g., "/personal/alice")

        Returns:
            Mount info dict if found, None otherwise. Dict contains:
                - mount_point: Virtual path (str)
                - priority: Mount priority (int)
                - readonly: Read-only flag (bool)
                - backend_type: Backend type name (str)

        Examples:
            >>> mount = nx.get_mount("/personal/alice")
            >>> if mount:
            ...     print(f"Priority: {mount['priority']}")
        """
        result = self._call_rpc("get_mount", {"mount_point": mount_point})
        return result  # type: ignore[no-any-return]

    def has_mount(self, mount_point: str) -> bool:
        """Check if a mount exists at the given path.

        Args:
            mount_point: Virtual path to check (e.g., "/personal/alice")

        Returns:
            True if mount exists, False otherwise

        Examples:
            >>> if nx.has_mount("/personal/alice"):
            ...     print("Alice's mount is active")
        """
        result = self._call_rpc("has_mount", {"mount_point": mount_point})
        return result  # type: ignore[no-any-return]

    def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        owner_user_id: str | None = None,
        tenant_id: str | None = None,
        description: str | None = None,
    ) -> str:
        """Save a mount configuration to the database for persistence.

        This allows mounts to survive server restarts. The mount must still be
        activated using add_mount() - this only stores the configuration.

        Args:
            mount_point: Virtual path where backend is mounted
            backend_type: Backend type - "local", "gcs", etc.
            backend_config: Backend-specific configuration dict
            priority: Mount priority (default: 0)
            readonly: Whether mount is read-only (default: False)
            owner_user_id: User who owns this mount (optional)
            tenant_id: Tenant ID for multi-tenant isolation (optional)
            description: Human-readable description (optional)

        Returns:
            Mount ID (UUID string)

        Raises:
            ValueError: If mount already exists at mount_point
            RemoteFilesystemError: If mount manager is not available

        Examples:
            >>> # Save personal Google Drive mount configuration
            >>> mount_id = nx.save_mount(
            ...     mount_point="/personal/alice",
            ...     backend_type="google_drive",
            ...     backend_config={"access_token": "ya29.xxx"},
            ...     owner_user_id="google:alice123",
            ...     tenant_id="acme",
            ...     description="Alice's personal Google Drive"
            ... )
        """
        result = self._call_rpc(
            "save_mount",
            {
                "mount_point": mount_point,
                "backend_type": backend_type,
                "backend_config": backend_config,
                "priority": priority,
                "readonly": readonly,
                "owner_user_id": owner_user_id,
                "tenant_id": tenant_id,
                "description": description,
            },
        )
        return result  # type: ignore[no-any-return]

    def list_saved_mounts(
        self, owner_user_id: str | None = None, tenant_id: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """List mount configurations saved in the database.

        Args:
            owner_user_id: Filter by owner user ID (optional)
            tenant_id: Filter by tenant ID (optional)

        Returns:
            List of saved mount configurations

        Raises:
            RemoteFilesystemError: If mount manager is not available

        Examples:
            >>> # List all saved mounts
            >>> mounts = nx.list_saved_mounts()

            >>> # List mounts for specific user
            >>> alice_mounts = nx.list_saved_mounts(owner_user_id="google:alice123")
        """
        result = self._call_rpc(
            "list_saved_mounts", {"owner_user_id": owner_user_id, "tenant_id": tenant_id}
        )
        return result  # type: ignore[no-any-return]

    def load_mount(self, mount_point: str) -> str:
        """Load a saved mount configuration and activate it.

        This retrieves the mount configuration from the database and activates it
        by calling add_mount() internally.

        Args:
            mount_point: Virtual path of saved mount to load

        Returns:
            Mount ID if successfully loaded and activated

        Raises:
            ValueError: If mount not found in database
            RemoteFilesystemError: If mount manager is not available

        Examples:
            >>> # Load Alice's saved mount
            >>> nx.load_mount("/personal/alice")
        """
        result = self._call_rpc("load_mount", {"mount_point": mount_point})
        return result  # type: ignore[no-any-return]

    def delete_saved_mount(self, mount_point: str) -> bool:
        """Delete a saved mount configuration from the database.

        Note: This does NOT deactivate the mount if it's currently active.
        Use remove_mount() to deactivate an active mount.

        Args:
            mount_point: Virtual path of mount to delete

        Returns:
            True if deleted, False if not found

        Raises:
            RemoteFilesystemError: If mount manager is not available

        Examples:
            >>> # Remove from database
            >>> nx.delete_saved_mount("/personal/alice")
            >>> # Also deactivate if currently mounted
            >>> nx.remove_mount("/personal/alice")
        """
        result = self._call_rpc("delete_saved_mount", {"mount_point": mount_point})
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Workspace and Memory Management
    # ============================================================

    def load_workspace_memory_config(
        self,
        workspaces: builtins.list[dict] | None = None,
        memories: builtins.list[dict] | None = None,
    ) -> dict[str, Any]:
        """Load workspaces and memories from configuration.

        Args:
            workspaces: List of workspace config dicts
            memories: List of memory config dicts

        Returns:
            Configuration result dict

        Raises:
            RemoteFilesystemError: If configuration cannot be loaded
        """
        result = self._call_rpc(
            "load_workspace_memory_config",
            {"workspaces": workspaces, "memories": memories},
        )
        return result  # type: ignore[no-any-return]

    def register_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str
        | None = None,  # v0.5.0: If provided, workspace is session-scoped (temporary)
        ttl: timedelta | None = None,  # v0.5.0: Time-to-live for auto-expiry
    ) -> dict[str, Any]:
        """Register a directory as a workspace.

        Args:
            path: Absolute path to workspace directory
            name: Optional friendly name for the workspace
            description: Human-readable description
            created_by: User/agent who created it
            tags: Tags for categorization (reserved for future use)
            metadata: Additional user-defined metadata
            session_id: If provided, workspace is session-scoped (temporary). If None, persistent. (v0.5.0)
            ttl: Time-to-live as timedelta for auto-expiry (v0.5.0)

        Returns:
            Workspace configuration dict

        Raises:
            RemoteFilesystemError: If registration fails
        """
        # tags parameter reserved for future use
        _ = tags

        result = self._call_rpc(
            "register_workspace",
            {
                "path": path,
                "name": name,
                "description": description,
                "created_by": created_by,
                "metadata": metadata,
                "session_id": session_id,  # v0.5.0
                "ttl": ttl,  # v0.5.0
            },
        )
        return result  # type: ignore[no-any-return]

    def unregister_workspace(self, path: str) -> bool:
        """Unregister a workspace (does NOT delete files).

        Args:
            path: Workspace path to unregister

        Returns:
            True if unregistered, False if not found

        Raises:
            RemoteFilesystemError: If unregistration fails
        """
        result = self._call_rpc("unregister_workspace", {"path": path})
        return result  # type: ignore[no-any-return]

    def list_workspaces(self) -> builtins.list[dict]:
        """List all registered workspaces.

        Returns:
            List of workspace configuration dicts

        Raises:
            RemoteFilesystemError: If listing fails
        """
        result = self._call_rpc("list_workspaces", {})
        return result  # type: ignore[no-any-return]

    def get_workspace_info(self, path: str) -> dict | None:
        """Get information about a registered workspace.

        Args:
            path: Workspace path

        Returns:
            Workspace configuration dict or None if not found

        Raises:
            RemoteFilesystemError: If retrieval fails
        """
        result = self._call_rpc("get_workspace_info", {"path": path})
        return result  # type: ignore[no-any-return]

    def register_memory(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,  # v0.5.0: If provided, memory is session-scoped (temporary)
        ttl: timedelta | None = None,  # v0.5.0: Time-to-live for auto-expiry
    ) -> dict[str, Any]:
        """Register a directory as a memory.

        Args:
            path: Absolute path to memory directory
            name: Optional friendly name for the memory
            description: Human-readable description
            created_by: User/agent who created it
            tags: Tags for categorization (reserved for future use)
            metadata: Additional user-defined metadata
            session_id: If provided, memory is session-scoped (temporary). If None, persistent. (v0.5.0)
            ttl: Time-to-live as timedelta for auto-expiry (v0.5.0)

        Returns:
            Memory configuration dict

        Raises:
            RemoteFilesystemError: If registration fails
        """
        # tags parameter reserved for future use
        _ = tags

        result = self._call_rpc(
            "register_memory",
            {
                "path": path,
                "name": name,
                "description": description,
                "created_by": created_by,
                "metadata": metadata,
                "session_id": session_id,  # v0.5.0
                "ttl": ttl,  # v0.5.0
            },
        )
        return result  # type: ignore[no-any-return]

    def unregister_memory(self, path: str) -> bool:
        """Unregister a memory (does NOT delete files).

        Args:
            path: Memory path to unregister

        Returns:
            True if unregistered, False if not found

        Raises:
            RemoteFilesystemError: If unregistration fails
        """
        result = self._call_rpc("unregister_memory", {"path": path})
        return result  # type: ignore[no-any-return]

    def list_memories(self) -> builtins.list[dict]:
        """List all registered memories.

        Returns:
            List of memory configuration dicts

        Raises:
            RemoteFilesystemError: If listing fails
        """
        result = self._call_rpc("list_memories", {})
        return result  # type: ignore[no-any-return]

    def get_memory_info(self, path: str) -> dict | None:
        """Get information about a registered memory.

        Args:
            path: Memory path

        Returns:
            Memory configuration dict or None if not found

        Raises:
            RemoteFilesystemError: If retrieval fails
        """
        result = self._call_rpc("get_memory_info", {"path": path})
        return result  # type: ignore[no-any-return]

    # ===== Agent Management (v0.5.0) =====

    def register_agent(
        self,
        agent_id: str,
        name: str,
        description: str | None = None,
        generate_api_key: bool = False,
        context: dict | None = None,
    ) -> dict:
        """Register an AI agent (v0.5.0).

        Agents are persistent identities owned by users. They do NOT have session_id
        or expiry - they live forever until explicitly deleted.

        Args:
            agent_id: Unique agent identifier
            name: Human-readable name
            description: Optional description
            generate_api_key: If True, create API key for agent (not recommended)
            context: Optional operation context (for compatibility with NexusFS)

        Returns:
            Agent info dict with agent_id, user_id, name, etc.

        Raises:
            RemoteFilesystemError: If registration fails

        Example:
            >>> # Recommended: No API key (uses user's auth + X-Agent-ID)
            >>> agent = nx.register_agent("data_analyst", "Data Analyst")
            >>> # Agent uses owner's credentials + X-Agent-ID header
        """
        params: dict[str, Any] = {
            "agent_id": agent_id,
            "name": name,
            "description": description,
            "generate_api_key": generate_api_key,
        }

        # Add context if provided (for compatibility with NexusFS)
        if context is not None:
            params["context"] = context

        result = self._call_rpc("register_agent", params)
        return result  # type: ignore[no-any-return]

    def list_agents(self) -> builtins.list[dict]:
        """List all registered agents.

        Returns:
            List of agent info dicts

        Raises:
            RemoteFilesystemError: If listing fails
        """
        result = self._call_rpc("list_agents", {})
        return result  # type: ignore[no-any-return]

    def get_agent(self, agent_id: str) -> dict | None:
        """Get agent information.

        Args:
            agent_id: Agent identifier

        Returns:
            Agent info dict or None if not found

        Raises:
            RemoteFilesystemError: If retrieval fails
        """
        result = self._call_rpc("get_agent", {"agent_id": agent_id})
        return result  # type: ignore[no-any-return]

    def delete_agent(self, agent_id: str) -> bool:
        """Delete an agent.

        Args:
            agent_id: Agent identifier to delete

        Returns:
            True if deleted, False if not found

        Raises:
            RemoteFilesystemError: If deletion fails
        """
        result = self._call_rpc("delete_agent", {"agent_id": agent_id})
        return result  # type: ignore[no-any-return]

    # ============================================================
    # Lifecycle Management
    # ============================================================

    # ACE (Adaptive Concurrency Engine) Methods

    def ace_start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Start tracking a new execution trajectory for ACE learning.

        Args:
            task_description: Description of the task being executed
            task_type: Optional task type ('api_call', 'data_processing', etc.)
            context: Operation context

        Returns:
            Dict with trajectory_id
        """
        params: dict[str, Any] = {"task_description": task_description}
        if task_type is not None:
            params["task_type"] = task_type
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_start_trajectory", params)
        return result  # type: ignore[no-any-return]

    def ace_log_trajectory_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        context: dict | None = None,
    ) -> dict:
        """Log a step in an execution trajectory.

        Args:
            trajectory_id: Trajectory ID
            step_type: Type of step ('action', 'decision', 'observation')
            description: Step description
            result: Optional result data
            context: Operation context

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
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_log_trajectory_step", params)
        return result  # type: ignore[no-any-return]

    def ace_complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Complete a trajectory with outcome.

        Args:
            trajectory_id: Trajectory ID
            status: Status ('success', 'failure', 'partial')
            success_score: Success score (0.0-1.0)
            error_message: Error message if failed
            context: Operation context

        Returns:
            Dict with trajectory_id
        """
        params: dict[str, Any] = {"trajectory_id": trajectory_id, "status": status}
        if success_score is not None:
            params["success_score"] = success_score
        if error_message is not None:
            params["error_message"] = error_message
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_complete_trajectory", params)
        return result  # type: ignore[no-any-return]

    def ace_add_feedback(
        self,
        trajectory_id: str,
        feedback_type: str,
        score: float | None = None,
        source: str | None = None,
        message: str | None = None,
        metrics: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """Add feedback to a completed trajectory.

        Args:
            trajectory_id: Trajectory ID
            feedback_type: Type of feedback
            score: Revised score (0.0-1.0)
            source: Feedback source
            message: Human-readable message
            metrics: Additional metrics
            context: Operation context

        Returns:
            Dict with feedback_id
        """
        params: dict[str, Any] = {"trajectory_id": trajectory_id, "feedback_type": feedback_type}
        if score is not None:
            params["score"] = score
        if source is not None:
            params["source"] = source
        if message is not None:
            params["message"] = message
        if metrics is not None:
            params["metrics"] = metrics
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_add_feedback", params)
        return result  # type: ignore[no-any-return]

    def ace_get_trajectory_feedback(
        self, trajectory_id: str, context: dict | None = None
    ) -> builtins.list[dict]:
        """Get all feedback for a trajectory.

        Args:
            trajectory_id: Trajectory ID
            context: Operation context

        Returns:
            List of feedback dicts
        """
        params: dict[str, Any] = {"trajectory_id": trajectory_id}
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_get_trajectory_feedback", params)
        return result  # type: ignore[no-any-return]

    def ace_get_effective_score(
        self,
        trajectory_id: str,
        strategy: str = "latest",
        context: dict | None = None,
    ) -> dict:
        """Get effective score for a trajectory.

        Args:
            trajectory_id: Trajectory ID
            strategy: Scoring strategy ('latest', 'average', 'weighted')
            context: Operation context

        Returns:
            Dict with effective_score
        """
        params: dict[str, Any] = {"trajectory_id": trajectory_id, "strategy": strategy}
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_get_effective_score", params)
        return result  # type: ignore[no-any-return]

    def ace_mark_for_relearning(
        self,
        trajectory_id: str,
        reason: str,
        priority: int = 5,
        context: dict | None = None,
    ) -> dict:
        """Mark trajectory for re-learning.

        Args:
            trajectory_id: Trajectory ID
            reason: Reason for re-learning
            priority: Priority (1-10)
            context: Operation context

        Returns:
            Success status
        """
        params: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "reason": reason,
            "priority": priority,
        }
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_mark_for_relearning", params)
        return result  # type: ignore[no-any-return]

    def ace_query_trajectories(
        self,
        task_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        context: dict | None = None,
    ) -> builtins.list[dict]:
        """Query execution trajectories.

        Args:
            task_type: Filter by task type
            status: Filter by status
            limit: Maximum results
            context: Operation context

        Returns:
            List of trajectory summaries
        """
        params: dict[str, Any] = {"limit": limit}
        if task_type is not None:
            params["task_type"] = task_type
        if status is not None:
            params["status"] = status
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_query_trajectories", params)
        return result  # type: ignore[no-any-return]

    def ace_create_playbook(
        self,
        name: str,
        description: str | None = None,
        scope: str = "agent",
        context: dict | None = None,
    ) -> dict:
        """Create a new playbook.

        Args:
            name: Playbook name
            description: Optional description
            scope: Scope level ('agent', 'user', 'tenant', 'global')
            context: Operation context

        Returns:
            Dict with playbook_id
        """
        params: dict[str, Any] = {"name": name, "scope": scope}
        if description is not None:
            params["description"] = description
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_create_playbook", params)
        return result  # type: ignore[no-any-return]

    def ace_get_playbook(self, playbook_id: str, context: dict | None = None) -> dict | None:
        """Get playbook details.

        Args:
            playbook_id: Playbook ID
            context: Operation context

        Returns:
            Playbook dict or None
        """
        params: dict[str, Any] = {"playbook_id": playbook_id}
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_get_playbook", params)
        return result  # type: ignore[no-any-return]

    def ace_query_playbooks(
        self,
        scope: str | None = None,
        limit: int = 50,
        context: dict | None = None,
    ) -> builtins.list[dict]:
        """Query playbooks.

        Args:
            scope: Filter by scope
            limit: Maximum results
            context: Operation context

        Returns:
            List of playbook summaries
        """
        params: dict[str, Any] = {"limit": limit}
        if scope is not None:
            params["scope"] = scope
        if context is not None:
            params["context"] = context
        result = self._call_rpc("ace_query_playbooks", params)
        return result  # type: ignore[no-any-return]

    @property
    def memory(self) -> RemoteMemory:
        """Get Memory API instance for agent memory management.

        Lazy initialization on first access.

        Returns:
            RemoteMemory API instance for RPC-based memory operations.

        Example:
            >>> nx = RemoteNexusFS("http://localhost:8080", api_key="...")
            >>> traj_id = nx.memory.start_trajectory("Process data", task_type="data_processing")
            >>> nx.memory.log_step(traj_id, "action", "Loaded 1000 records")
            >>> nx.memory.complete_trajectory(traj_id, "success", success_score=0.95)
        """
        if self._memory_api is None:
            self._memory_api = RemoteMemory(self)
        return self._memory_api

    def shutdown_parser_threads(self, timeout: float = 10.0) -> dict[str, Any]:
        """Shutdown background parser threads on remote server.

        Args:
            timeout: Maximum seconds to wait for each thread (default: 10s)

        Returns:
            Dict with shutdown statistics from server
        """
        result = self._call_rpc("shutdown_parser_threads", {"timeout": timeout})
        return result  # type: ignore[no-any-return]

    def close(self) -> None:
        """Close the client and release resources."""
        self.session.close()
