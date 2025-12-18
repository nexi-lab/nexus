"""RPC protocol definitions for Nexus filesystem server.

This module defines the JSON-RPC protocol for exposing NexusFileSystem
operations over HTTP. Each method in the NexusFilesystem interface
maps to an RPC endpoint.

Protocol Format:
    POST /api/nfs/{method_name}

    Request:
    {
        "jsonrpc": "2.0",
        "id": "request-id",
        "params": {
            "arg1": value1,
            "arg2": value2
        }
    }

    Response (success):
    {
        "jsonrpc": "2.0",
        "id": "request-id",
        "result": {...}
    }

    Response (error):
    {
        "jsonrpc": "2.0",
        "id": "request-id",
        "error": {
            "code": -32000,
            "message": "Error message",
            "data": {...}
        }
    }
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any


class RPCErrorCode(Enum):
    """Standard JSON-RPC error codes + custom Nexus error codes."""

    # Standard JSON-RPC errors
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # Nexus-specific errors
    FILE_NOT_FOUND = -32000
    FILE_EXISTS = -32001
    INVALID_PATH = -32002
    ACCESS_DENIED = -32003
    PERMISSION_ERROR = -32004
    VALIDATION_ERROR = -32005
    CONFLICT = -32006  # Optimistic concurrency conflict


@dataclass
class RPCRequest:
    """JSON-RPC request."""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str = ""
    params: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RPCRequest:
        """Create request from dict."""
        return cls(
            jsonrpc=data.get("jsonrpc", "2.0"),
            id=data.get("id"),
            method=data.get("method", ""),
            params=data.get("params"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result: dict[str, Any] = {"jsonrpc": self.jsonrpc, "method": self.method}
        if self.id is not None:
            result["id"] = self.id
        if self.params is not None:
            result["params"] = self.params
        return result


@dataclass
class RPCResponse:
    """JSON-RPC response."""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    result: Any = None
    error: dict[str, Any] | None = None

    @classmethod
    def success(cls, request_id: str | int | None, result: Any) -> RPCResponse:
        """Create success response."""
        return cls(id=request_id, result=result, error=None)

    @classmethod
    def create_error(
        cls,
        request_id: str | int | None,
        code: RPCErrorCode,
        message: str,
        data: Any = None,
    ) -> RPCResponse:
        """Create error response."""
        error_dict: dict[str, Any] = {"code": code.value, "message": message}
        if data is not None:
            error_dict["data"] = data
        return cls(id=request_id, result=None, error=error_dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result: dict[str, Any] = {"jsonrpc": self.jsonrpc}
        if self.id is not None:
            result["id"] = self.id
        if self.error is not None:
            result["error"] = self.error
        else:
            result["result"] = self.result
        return result


class RPCEncoder(json.JSONEncoder):
    """Custom JSON encoder for RPC messages.

    Handles special types:
    - bytes: base64-encoded strings
    - datetime: ISO format strings
    - timedelta: total seconds (v0.5.0)
    """

    def default(self, obj: Any) -> Any:
        """Encode special types."""
        if isinstance(obj, bytes):
            return {"__type__": "bytes", "data": base64.b64encode(obj).decode("utf-8")}
        elif isinstance(obj, datetime):
            return {"__type__": "datetime", "data": obj.isoformat()}
        elif isinstance(obj, type(obj)) and obj.__class__.__name__ == "timedelta":
            # v0.5.0: Encode timedelta as total seconds
            from datetime import timedelta

            if isinstance(obj, timedelta):
                return {"__type__": "timedelta", "seconds": obj.total_seconds()}
        elif hasattr(obj, "__dict__"):
            # Convert objects to dictionaries, filtering out methods
            return {
                k: v for k, v in obj.__dict__.items() if not k.startswith("_") and not callable(v)
            }
        return super().default(obj)


def rpc_decode_hook(obj: Any) -> Any:
    """Decode hook for special types."""
    if isinstance(obj, dict) and "__type__" in obj:
        if obj["__type__"] == "bytes":
            return base64.b64decode(obj["data"])
        elif obj["__type__"] == "datetime":
            return datetime.fromisoformat(obj["data"])
        elif obj["__type__"] == "timedelta":
            # v0.5.0: Decode timedelta from seconds
            from datetime import timedelta

            return timedelta(seconds=obj["seconds"])
    return obj


# Try to import orjson for faster JSON serialization (2-3x faster)
try:
    import orjson

    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False


def _prepare_for_orjson(obj: Any) -> Any:
    """Convert objects to orjson-compatible types for encoding responses.

    Handles all special types that RPCEncoder handles:
    - bytes: base64-encoded with __type__ wrapper
    - datetime/date: ISO format with __type__ wrapper
    - timedelta: seconds with __type__ wrapper
    - objects with __dict__: converted to dict
    """
    if isinstance(obj, bytes):
        return {"__type__": "bytes", "data": base64.b64encode(obj).decode("utf-8")}
    elif isinstance(obj, (datetime, date)):
        return {"__type__": "datetime", "data": obj.isoformat()}
    elif isinstance(obj, timedelta):
        return {"__type__": "timedelta", "seconds": obj.total_seconds()}
    elif isinstance(obj, dict):
        return {k: _prepare_for_orjson(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_prepare_for_orjson(item) for item in obj]
    elif hasattr(obj, "__dict__") and not isinstance(obj, type):
        return {
            k: _prepare_for_orjson(v)
            for k, v in obj.__dict__.items()
            if not k.startswith("_") and not callable(v)
        }
    else:
        return obj


def _apply_decode_hook(obj: Any) -> Any:
    """Recursively apply rpc_decode_hook to convert special types after orjson parsing.

    orjson doesn't support object_hook, so we apply it manually after parsing.
    """
    if isinstance(obj, dict):
        # First check if this dict is a special type wrapper
        if "__type__" in obj:
            return rpc_decode_hook(obj)
        # Otherwise recursively process all values
        return {k: _apply_decode_hook(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_apply_decode_hook(item) for item in obj]
    else:
        return obj


def encode_rpc_message(data: dict[str, Any]) -> bytes:
    """Encode RPC message to JSON bytes (uses orjson if available for 2-3x speedup)."""
    import logging
    import time

    logger = logging.getLogger(__name__)
    start = time.time()

    if HAS_ORJSON:
        # orjson is much faster and returns bytes directly
        # But it doesn't support custom encoders, so we ALWAYS pre-process the data
        # to ensure special types (bytes, datetime, timedelta) are wrapped with __type__
        # This is needed because orjson serializes datetime as plain strings,
        # which breaks the decode_rpc_message round-trip.
        prepared_data = _prepare_for_orjson(data)
        result: bytes = orjson.dumps(prepared_data)
        elapsed = (time.time() - start) * 1000
        logger.debug(f"[RPC-PERF] orjson encode: {len(result)} bytes in {elapsed:.1f}ms")
        return result
    else:
        # Fallback to standard json with custom encoder
        result_json: bytes = json.dumps(data, cls=RPCEncoder).encode("utf-8")
        elapsed = (time.time() - start) * 1000
        logger.debug(
            f"[RPC-PERF] standard json encode: {len(result_json)} bytes in {elapsed:.1f}ms"
        )
        return result_json


def decode_rpc_message(data: bytes) -> dict[str, Any]:
    """Decode RPC message from JSON bytes (uses orjson if available).

    When orjson is used, we apply the decode hook manually after parsing
    to convert special types like {"__type__": "bytes", "data": "..."} back to bytes.
    """
    if HAS_ORJSON:
        parsed = orjson.loads(data)
        # Apply decode hook to convert special types (bytes, datetime, timedelta)
        return _apply_decode_hook(parsed)  # type: ignore[no-any-return]
    else:
        return json.loads(data.decode("utf-8"), object_hook=rpc_decode_hook)  # type: ignore[no-any-return]


# ============================================================
# RPC Exposure Decorator
# ============================================================

# Import decorator from core module to avoid circular imports
# Re-export here for backward compatibility
from nexus.core.rpc_decorator import rpc_expose  # noqa: F401, E402

# ============================================================
# Method-specific parameter schemas
# ============================================================


@dataclass
class ReadParams:
    """Parameters for read() method."""

    path: str
    return_metadata: bool = False  # Return dict with content + metadata
    parsed: bool = False  # Return parsed text instead of raw bytes
    return_url: bool = False  # Return presigned URL instead of content (S3/GCS)
    expires_in: int = 3600  # URL expiration in seconds (default 1 hour)


@dataclass
class ReadBulkParams:
    """Parameters for read_bulk() method."""

    paths: list[str]
    return_metadata: bool = False  # Return dict with content + metadata
    skip_errors: bool = True  # Skip files that can't be read


@dataclass
class WriteParams:
    """Parameters for write() method."""

    path: str
    content: bytes
    if_match: str | None = None  # Optimistic concurrency control
    if_none_match: bool = False  # Create-only mode
    force: bool = False  # Skip version check


@dataclass
class AppendParams:
    """Parameters for append() method."""

    path: str
    content: bytes
    if_match: str | None = None  # Optimistic concurrency control
    force: bool = False  # Skip version check


@dataclass
class DeleteParams:
    """Parameters for delete() method."""

    path: str


@dataclass
class RenameParams:
    """Parameters for rename() method."""

    old_path: str
    new_path: str


@dataclass
class DeleteBulkParams:
    """Parameters for delete_bulk() method."""

    paths: list[str]
    recursive: bool = False


@dataclass
class RenameBulkParams:
    """Parameters for rename_bulk() method."""

    renames: list[tuple[str, str]]


@dataclass
class ExistsParams:
    """Parameters for exists() method."""

    path: str


@dataclass
class GetEtagParams:
    """Parameters for get_etag() method."""

    path: str


@dataclass
class ListParams:
    """Parameters for list() method."""

    path: str = "/"
    recursive: bool = True
    details: bool = False
    prefix: str | None = None
    show_parsed: bool = True


@dataclass
class GlobParams:
    """Parameters for glob() method."""

    pattern: str
    path: str = "/"


@dataclass
class GrepParams:
    """Parameters for grep() method."""

    pattern: str
    path: str = "/"
    file_pattern: str | None = None
    ignore_case: bool = False
    max_results: int = (
        100  # Reduced from 1000 for faster responses (user can increase with --max-results)
    )
    search_mode: str = "auto"


@dataclass
class MkdirParams:
    """Parameters for mkdir() method."""

    path: str
    parents: bool = False
    exist_ok: bool = False


@dataclass
class RmdirParams:
    """Parameters for rmdir() method."""

    path: str
    recursive: bool = False


@dataclass
class IsDirectoryParams:
    """Parameters for is_directory() method."""

    path: str


@dataclass
class GetAvailableNamespacesParams:
    """Parameters for get_available_namespaces() method."""

    pass


@dataclass
class GetMetadataParams:
    """Parameters for get_metadata() method."""

    path: str


@dataclass
class RebacCreateParams:
    """Parameters for rebac_create() method."""

    subject: tuple[str, str]
    relation: str
    object: tuple[str, str]
    expires_at: str | None = None
    tenant_id: str | None = None
    column_config: dict[str, Any] | None = None


@dataclass
class RebacCheckParams:
    """Parameters for rebac_check() method."""

    subject: tuple[str, str]
    permission: str
    object: tuple[str, str]
    tenant_id: str | None = None


@dataclass
class RebacExpandParams:
    """Parameters for rebac_expand() method."""

    permission: str
    object: tuple[str, str]


@dataclass
class RebacExplainParams:
    """Parameters for rebac_explain() method."""

    subject: tuple[str, str]
    permission: str
    object: tuple[str, str]
    tenant_id: str | None = None


@dataclass
class RebacDeleteParams:
    """Parameters for rebac_delete() method."""

    tuple_id: str


@dataclass
class RebacListTuplesParams:
    """Parameters for rebac_list_tuples() method."""

    subject: tuple[str, str] | None = None
    relation: str | None = None
    object: tuple[str, str] | None = None


# Cross-tenant sharing params
@dataclass
class ShareWithUserParams:
    """Parameters for share_with_user() method."""

    resource: tuple[str, str]
    user_id: str
    relation: str = "viewer"
    tenant_id: str | None = None
    user_tenant_id: str | None = None
    expires_at: str | None = None


@dataclass
class RevokeShareParams:
    """Parameters for revoke_share() method."""

    resource: tuple[str, str]
    user_id: str


@dataclass
class RevokeShareByIdParams:
    """Parameters for revoke_share_by_id() method."""

    share_id: str


@dataclass
class ListOutgoingSharesParams:
    """Parameters for list_outgoing_shares() method."""

    resource: tuple[str, str] | None = None
    tenant_id: str | None = None
    limit: int = 100
    offset: int = 0


@dataclass
class ListIncomingSharesParams:
    """Parameters for list_incoming_shares() method."""

    user_id: str
    limit: int = 100
    offset: int = 0


# Public access params
@dataclass
class MakePublicParams:
    """Parameters for make_public() method."""

    resource: tuple[str, str]
    tenant_id: str | None = None


@dataclass
class MakePrivateParams:
    """Parameters for make_private() method."""

    resource: tuple[str, str]


@dataclass
class NamespaceCreateParams:
    """Parameters for namespace_create() method."""

    object_type: str
    config: dict[str, Any]


@dataclass
class NamespaceGetParams:
    """Parameters for namespace_get() method."""

    object_type: str


@dataclass
class NamespaceListParams:
    """Parameters for namespace_list() method."""

    pass


@dataclass
class NamespaceDeleteParams:
    """Parameters for namespace_delete() method."""

    object_type: str


@dataclass
class RegisterWorkspaceParams:
    """Parameters for register_workspace() method (v0.5.0)."""

    path: str
    name: str | None = None
    description: str | None = None
    created_by: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    session_id: str | None = None  # v0.5.0
    ttl: Any | None = None  # v0.5.0: Will be converted from seconds


@dataclass
class RegisterMemoryParams:
    """Parameters for register_memory() method (v0.5.0)."""

    path: str
    name: str | None = None
    description: str | None = None
    created_by: str | None = None
    metadata: dict[str, Any] | None = None
    session_id: str | None = None  # v0.5.0
    ttl: Any | None = None  # v0.5.0: Will be converted from seconds


@dataclass
class GetWorkspaceInfoParams:
    """Parameters for get_workspace_info() method (v0.5.0)."""

    path: str


@dataclass
class UnregisterWorkspaceParams:
    """Parameters for unregister_workspace() method (v0.5.0)."""

    path: str


@dataclass
class GetMemoryInfoParams:
    """Parameters for get_memory_info() method (v0.5.0)."""

    path: str


@dataclass
class UnregisterMemoryParams:
    """Parameters for unregister_memory() method (v0.5.0)."""

    path: str


@dataclass
class ListWorkspacesParams:
    """Parameters for list_workspaces() method (v0.5.0)."""

    pass


@dataclass
class ListMemoriesParams:
    """Parameters for list_memories() method (v0.5.0)."""

    limit: int = 50
    scope: str | None = None
    memory_type: str | None = None
    namespace: str | None = None  # v0.8.0
    namespace_prefix: str | None = None  # v0.8.0
    state: str | None = "active"  # #368: Filter by state (inactive/active/all)


@dataclass
class ListRegisteredMemoriesParams:
    """Parameters for list_registered_memories() method."""

    pass


@dataclass
class WorkspaceSnapshotParams:
    """Parameters for workspace_snapshot() method (v0.5.0)."""

    workspace_path: str | None = None
    agent_id: str | None = None  # DEPRECATED
    description: str | None = None
    tags: list[str] | None = None
    created_by: str | None = None


@dataclass
class WorkspaceRestoreParams:
    """Parameters for workspace_restore() method (v0.5.0)."""

    snapshot_number: int
    workspace_path: str | None = None
    agent_id: str | None = None  # DEPRECATED


@dataclass
class WorkspaceLogParams:
    """Parameters for workspace_log() method (v0.5.0)."""

    workspace_path: str | None = None
    agent_id: str | None = None  # DEPRECATED
    limit: int = 100


@dataclass
class WorkspaceDiffParams:
    """Parameters for workspace_diff() method (v0.5.0)."""

    snapshot_1: int
    snapshot_2: int
    workspace_path: str | None = None
    agent_id: str | None = None  # DEPRECATED


@dataclass
class GetVersionParams:
    """Parameters for get_version() method."""

    path: str
    version: int


@dataclass
class ListVersionsParams:
    """Parameters for list_versions() method."""

    path: str


@dataclass
class RollbackParams:
    """Parameters for rollback() method."""

    path: str
    version: int


@dataclass
class DiffVersionsParams:
    """Parameters for diff_versions() method."""

    path: str
    v1: int
    v2: int
    mode: str = "metadata"


@dataclass
class RegisterAgentParams:
    """Parameters for register_agent() method (v0.5.0).

    v0.5.1: Added inherit_permissions for permission control and metadata for flexible config.
    """

    agent_id: str
    name: str
    description: str | None = None
    generate_api_key: bool = False
    inherit_permissions: bool = False  # v0.5.1: Default False (zero permissions)
    metadata: dict | None = None  # v0.5.1: Optional metadata (platform, endpoint_url, etc.)
    context: dict | None = None  # For compatibility with NexusFS signature


@dataclass
class ListAgentsParams:
    """Parameters for list_agents() method (v0.5.0)."""

    pass


@dataclass
class GetAgentParams:
    """Parameters for get_agent() method (v0.5.0)."""

    agent_id: str


@dataclass
class DeleteAgentParams:
    """Parameters for delete_agent() method (v0.5.0)."""

    agent_id: str


# ========== Memory API Parameters (v0.5.0) ==========


@dataclass
class StartTrajectoryParams:
    """Parameters for start_trajectory() method (v0.5.0)."""

    task_description: str
    task_type: str | None = None


@dataclass
class LogTrajectoryStepParams:
    """Parameters for log_trajectory_step() method (v0.5.0)."""

    trajectory_id: str
    step_type: str
    description: str
    result: Any = None


@dataclass
class CompleteTrajectoryParams:
    """Parameters for complete_trajectory() method (v0.5.0)."""

    trajectory_id: str
    status: str
    success_score: float | None = None
    error_message: str | None = None


@dataclass
class GetPlaybookParams:
    """Parameters for get_playbook() method (v0.5.0)."""

    playbook_name: str = "default"


@dataclass
class CuratePlaybookParams:
    """Parameters for curate_playbook() method (v0.5.0)."""

    reflection_memory_ids: list[str]
    playbook_name: str = "default"
    merge_threshold: float = 0.7


@dataclass
class BatchReflectParams:
    """Parameters for batch_reflect() method (v0.5.0)."""

    agent_id: str | None = None
    since: str | None = None
    min_trajectories: int = 10
    task_type: str | None = None


@dataclass
class StoreMemoryParams:
    """Parameters for store_memory() method (v0.5.0)."""

    content: str
    memory_type: str = "fact"
    scope: str = "agent"
    importance: float = 0.5
    namespace: str | None = None  # v0.8.0
    path_key: str | None = None  # v0.8.0
    state: str = "active"  # #368
    tags: list[str] | None = None


@dataclass
class RetrieveMemoryParams:
    """Parameters for retrieve_memory() method (v0.8.0)."""

    namespace: str | None = None
    path_key: str | None = None
    path: str | None = None


@dataclass
class DeleteMemoryParams:
    """Parameters for delete_memory() method (v0.8.0)."""

    memory_id: str


# ========== ACE (Adaptive Concurrency Engine) Parameters ==========


@dataclass
class AceStartTrajectoryParams:
    """Parameters for ace_start_trajectory() method."""

    task_description: str
    task_type: str | None = None
    context: dict | None = None


@dataclass
class AceLogTrajectoryStepParams:
    """Parameters for ace_log_trajectory_step() method."""

    trajectory_id: str
    step_type: str
    description: str
    result: Any = None
    context: dict | None = None


@dataclass
class AceCompleteTrajectoryParams:
    """Parameters for ace_complete_trajectory() method."""

    trajectory_id: str
    status: str
    success_score: float | None = None
    error_message: str | None = None
    context: dict | None = None


@dataclass
class AceAddFeedbackParams:
    """Parameters for ace_add_feedback() method."""

    trajectory_id: str
    feedback_type: str
    score: float | None = None
    source: str | None = None
    message: str | None = None
    metrics: dict | None = None
    context: dict | None = None


@dataclass
class AceGetTrajectoryFeedbackParams:
    """Parameters for ace_get_trajectory_feedback() method."""

    trajectory_id: str
    context: dict | None = None


@dataclass
class AceGetEffectiveScoreParams:
    """Parameters for ace_get_effective_score() method."""

    trajectory_id: str
    strategy: str = "latest"
    context: dict | None = None


@dataclass
class AceMarkForRelearningParams:
    """Parameters for ace_mark_for_relearning() method."""

    trajectory_id: str
    reason: str
    priority: int = 5
    context: dict | None = None


@dataclass
class AceQueryTrajectoriesParams:
    """Parameters for ace_query_trajectories() method."""

    task_type: str | None = None
    status: str | None = None
    limit: int = 50
    context: dict | None = None


@dataclass
class AceCreatePlaybookParams:
    """Parameters for ace_create_playbook() method."""

    name: str
    description: str | None = None
    scope: str = "agent"
    context: dict | None = None


@dataclass
class AceGetPlaybookParams:
    """Parameters for ace_get_playbook() method."""

    playbook_id: str
    context: dict | None = None


@dataclass
class AceQueryPlaybooksParams:
    """Parameters for ace_query_playbooks() method."""

    scope: str | None = None
    limit: int = 50
    context: dict | None = None


@dataclass
class ApproveMemoryParams:
    """Parameters for approve_memory() method (#368)."""

    memory_id: str


@dataclass
class DeactivateMemoryParams:
    """Parameters for deactivate_memory() method (#368)."""

    memory_id: str


@dataclass
class ApproveMemoryBatchParams:
    """Parameters for approve_memory_batch() method (#368)."""

    memory_ids: list[str]


@dataclass
class DeactivateMemoryBatchParams:
    """Parameters for deactivate_memory_batch() method (#368)."""

    memory_ids: list[str]


@dataclass
class DeleteMemoryBatchParams:
    """Parameters for delete_memory_batch() method (#368)."""

    memory_ids: list[str]


@dataclass
class QueryMemoriesParams:
    """Parameters for query_memories() method (v0.5.0)."""

    memory_type: str | None = None
    scope: str | None = None
    state: str | None = "active"  # #368: Filter by state
    limit: int = 50
    # #406: Semantic search support
    query: str | None = None  # Natural language query for semantic search
    search_mode: str | None = None  # "semantic", "keyword", or "hybrid"
    embedding_provider: str | None = None  # "openai", "voyage", or "openrouter"


@dataclass
class QueryTrajectoriesParams:
    """Parameters for query_trajectories() method (v0.5.0)."""

    agent_id: str | None = None
    status: str | None = None
    limit: int = 50


@dataclass
class QueryPlaybooksParams:
    """Parameters for query_playbooks() method (v0.5.0)."""

    agent_id: str | None = None
    scope: str | None = None
    limit: int = 50


@dataclass
class ProcessRelearningParams:
    """Parameters for process_relearning() method (v0.5.0)."""

    limit: int = 10


# ============================================================
# Admin API Parameters (v0.5.1)
# ============================================================


@dataclass
class AdminCreateKeyParams:
    """Parameters for admin_create_key() method.

    Admin-only API to create API keys for users without requiring SSH access.
    """

    user_id: str
    name: str
    is_admin: bool = False
    expires_days: int | None = None
    tenant_id: str = "default"
    subject_type: str = "user"
    subject_id: str | None = None


@dataclass
class AdminListKeysParams:
    """Parameters for admin_list_keys() method.

    Admin-only API to list API keys with optional filtering.
    """

    user_id: str | None = None
    tenant_id: str | None = None
    is_admin: bool | None = None
    include_revoked: bool = False
    include_expired: bool = False
    limit: int = 100
    offset: int = 0


@dataclass
class AdminGetKeyParams:
    """Parameters for admin_get_key() method.

    Admin-only API to get details of a specific API key.
    """

    key_id: str


@dataclass
class AdminRevokeKeyParams:
    """Parameters for admin_revoke_key() method.

    Admin-only API to revoke an API key.
    """

    key_id: str


@dataclass
class AdminUpdateKeyParams:
    """Parameters for admin_update_key() method.

    Admin-only API to update API key properties.
    """

    key_id: str
    expires_days: int | None = None
    is_admin: bool | None = None
    name: str | None = None


# ============================================================================
# Sandbox Management Parameters (Issue #372)
# ============================================================================


@dataclass
class SandboxCreateParams:
    """Parameters for sandbox_create() method."""

    name: str
    ttl_minutes: int = 10
    provider: str = "e2b"
    template_id: str | None = None
    context: dict | None = None


@dataclass
class SandboxRunParams:
    """Parameters for sandbox_run() method."""

    sandbox_id: str
    language: str
    code: str
    timeout: int = 300
    nexus_url: str | None = None
    nexus_api_key: str | None = None
    context: dict | None = None


@dataclass
class SandboxPauseParams:
    """Parameters for sandbox_pause() method."""

    sandbox_id: str
    context: dict | None = None


@dataclass
class SandboxResumeParams:
    """Parameters for sandbox_resume() method."""

    sandbox_id: str
    context: dict | None = None


@dataclass
class SandboxStopParams:
    """Parameters for sandbox_stop() method."""

    sandbox_id: str
    context: dict | None = None


@dataclass
class SandboxListParams:
    """Parameters for sandbox_list() method."""

    context: dict | None = None
    verify_status: bool = False
    user_id: str | None = None
    tenant_id: str | None = None
    agent_id: str | None = None
    status: str | None = None


@dataclass
class SandboxStatusParams:
    """Parameters for sandbox_status() method."""

    sandbox_id: str
    context: dict | None = None


@dataclass
class SandboxGetOrCreateParams:
    """Parameters for sandbox_get_or_create() method."""

    name: str
    ttl_minutes: int = 10
    provider: str | None = None
    template_id: str | None = None
    verify_status: bool = True
    context: dict | None = None


@dataclass
class SandboxConnectParams:
    """Parameters for sandbox_connect() method."""

    sandbox_id: str
    provider: str = "e2b"
    sandbox_api_key: str | None = None
    mount_path: str = "/mnt/nexus"
    nexus_url: str | None = None  # Nexus server URL for mounting
    nexus_api_key: str | None = None  # Nexus API key for mounting
    agent_id: str | None = None  # Agent ID for version attribution (issue #418)
    context: dict | None = None


@dataclass
class SandboxDisconnectParams:
    """Parameters for sandbox_disconnect() method."""

    sandbox_id: str
    provider: str = "e2b"
    sandbox_api_key: str | None = None
    context: dict | None = None


# Mount management parameters (v0.5.6 - Issue #313)
@dataclass
class AddMountParams:
    """Parameters for add_mount() method."""

    mount_point: str
    backend_type: str
    backend_config: dict[str, Any]
    priority: int = 0
    readonly: bool = False


@dataclass
class RemoveMountParams:
    """Parameters for remove_mount() method."""

    mount_point: str


@dataclass
class ListConnectorsParams:
    """Parameters for list_connectors() method."""

    category: str | None = None


@dataclass
class ListMountsParams:
    """Parameters for list_mounts() method."""

    pass


@dataclass
class GetMountParams:
    """Parameters for get_mount() method."""

    mount_point: str


@dataclass
class HasMountParams:
    """Parameters for has_mount() method."""

    mount_point: str


@dataclass
class SaveMountParams:
    """Parameters for save_mount() method."""

    mount_point: str
    backend_type: str
    backend_config: dict[str, Any]
    priority: int = 0
    readonly: bool = False
    owner_user_id: str | None = None
    tenant_id: str | None = None
    description: str | None = None


@dataclass
class ListSavedMountsParams:
    """Parameters for list_saved_mounts() method."""

    owner_user_id: str | None = None
    tenant_id: str | None = None


@dataclass
class LoadMountParams:
    """Parameters for load_mount() method."""

    mount_point: str


@dataclass
class DeleteSavedMountParams:
    """Parameters for delete_saved_mount() method."""

    mount_point: str


@dataclass
class SyncMountParams:
    """Parameters for sync_mount() method."""

    mount_point: str | None = None
    path: str | None = None
    recursive: bool = True
    dry_run: bool = False
    sync_content: bool = True
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    generate_embeddings: bool = False


@dataclass
class SyncMountAsyncParams:
    """Parameters for sync_mount_async() method (Issue #609)."""

    mount_point: str
    path: str | None = None
    recursive: bool = True
    dry_run: bool = False
    sync_content: bool = True
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    generate_embeddings: bool = False


@dataclass
class GetSyncJobParams:
    """Parameters for get_sync_job() method (Issue #609)."""

    job_id: str


@dataclass
class CancelSyncJobParams:
    """Parameters for cancel_sync_job() method (Issue #609)."""

    job_id: str


@dataclass
class ListSyncJobsParams:
    """Parameters for list_sync_jobs() method (Issue #609)."""

    mount_point: str | None = None
    status: str | None = None
    limit: int = 50


# Skills management parameter dataclasses
@dataclass
class SkillsCreateParams:
    """Parameters for skills_create method."""

    name: str
    description: str
    template: str = "basic"
    tier: str = "agent"
    author: str | None = None


@dataclass
class SkillsCreateFromContentParams:
    """Parameters for skills_create_from_content method."""

    name: str
    description: str
    content: str
    tier: str = "agent"
    author: str | None = None
    source_url: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class SkillsCreateFromFileParams:
    """Parameters for skills_create_from_file method."""

    source: str
    file_data: str | None = None
    name: str | None = None
    description: str | None = None
    tier: str = "agent"
    use_ai: bool = False
    use_ocr: bool = False
    extract_tables: bool = False
    extract_images: bool = False
    _author: str | None = None  # Unused: plugin manages authorship


@dataclass
class SkillsListParams:
    """Parameters for skills_list method."""

    tier: str | None = None
    include_metadata: bool = True


@dataclass
class SkillsInfoParams:
    """Parameters for skills_info method."""

    skill_name: str


@dataclass
class SkillsForkParams:
    """Parameters for skills_fork method."""

    source_name: str
    target_name: str
    tier: str = "agent"
    author: str | None = None


@dataclass
class SkillsPublishParams:
    """Parameters for skills_publish method."""

    skill_name: str
    source_tier: str = "agent"
    target_tier: str = "tenant"


@dataclass
class SkillsSearchParams:
    """Parameters for skills_search method."""

    query: str
    tier: str | None = None
    limit: int = 10


@dataclass
class SkillsSubmitApprovalParams:
    """Parameters for skills_submit_approval method."""

    skill_name: str
    submitted_by: str
    reviewers: list[str] | None = None
    comments: str | None = None


@dataclass
class SkillsApproveParams:
    """Parameters for skills_approve method."""

    approval_id: str
    reviewed_by: str
    reviewer_type: str = "user"
    comments: str | None = None
    tenant_id: str | None = None


@dataclass
class SkillsRejectParams:
    """Parameters for skills_reject method."""

    approval_id: str
    reviewed_by: str
    reviewer_type: str = "user"
    comments: str | None = None
    tenant_id: str | None = None


@dataclass
class SkillsListApprovalsParams:
    """Parameters for skills_list_approvals method."""

    status: str | None = None
    skill_name: str | None = None


@dataclass
class SkillsImportParams:
    """Parameters for skills_import method."""

    zip_data: str
    tier: str = "user"
    allow_overwrite: bool = False


@dataclass
class SkillsValidateZipParams:
    """Parameters for skills_validate_zip method."""

    zip_data: str


@dataclass
class SkillsExportParams:
    """Parameters for skills_export method."""

    skill_name: str
    format: str = "generic"
    include_dependencies: bool = False


# OAuth management methods (v0.9.0)
@dataclass
class OAuthListProvidersParams:
    """Parameters for oauth_list_providers method."""

    pass  # No parameters required


@dataclass
class OAuthGetAuthUrlParams:
    """Parameters for oauth_get_auth_url method."""

    provider: str
    redirect_uri: str = "http://localhost:3000/oauth/callback"
    scopes: list[str] | None = None


@dataclass
class OAuthExchangeCodeParams:
    """Parameters for oauth_exchange_code method."""

    provider: str
    code: str
    user_email: str | None = None  # Optional: will be fetched from provider if not provided
    state: str | None = None
    redirect_uri: str = "http://localhost:3000/oauth/callback"


@dataclass
class OAuthListCredentialsParams:
    """Parameters for oauth_list_credentials method."""

    provider: str | None = None
    include_revoked: bool = False


@dataclass
class OAuthRevokeCredentialParams:
    """Parameters for oauth_revoke_credential method."""

    provider: str
    user_email: str


@dataclass
class OAuthTestCredentialParams:
    """Parameters for oauth_test_credential method."""

    provider: str
    user_email: str


@dataclass
class MCPConnectParams:
    """Parameters for mcp_connect method."""

    provider: str
    redirect_url: str | None = None
    user_email: str | None = None
    reuse_nexus_token: bool = True


@dataclass
class MCPListMountsParams:
    """Parameters for mcp_list_mounts method."""

    tier: str | None = None
    include_unmounted: bool = True


@dataclass
class MCPListToolsParams:
    """Parameters for mcp_list_tools method."""

    name: str


@dataclass
class MCPMountParams:
    """Parameters for mcp_mount method."""

    name: str
    transport: str | None = None
    command: str | None = None
    url: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    description: str | None = None
    tier: str = "system"


@dataclass
class MCPUnmountParams:
    """Parameters for mcp_unmount method."""

    name: str


@dataclass
class MCPSyncParams:
    """Parameters for mcp_sync method."""

    name: str


# Mapping of method names to parameter dataclasses
METHOD_PARAMS = {
    "read": ReadParams,
    "write": WriteParams,
    "append": AppendParams,
    "delete": DeleteParams,
    "rename": RenameParams,
    "delete_bulk": DeleteBulkParams,
    "rename_bulk": RenameBulkParams,
    "exists": ExistsParams,
    "get_etag": GetEtagParams,
    "list": ListParams,
    "glob": GlobParams,
    "grep": GrepParams,
    "mkdir": MkdirParams,
    "rmdir": RmdirParams,
    "is_directory": IsDirectoryParams,
    "get_available_namespaces": GetAvailableNamespacesParams,
    "get_metadata": GetMetadataParams,
    "rebac_create": RebacCreateParams,
    "rebac_check": RebacCheckParams,
    "rebac_expand": RebacExpandParams,
    "rebac_explain": RebacExplainParams,
    "rebac_delete": RebacDeleteParams,
    "rebac_list_tuples": RebacListTuplesParams,
    # Cross-tenant sharing methods
    "share_with_user": ShareWithUserParams,
    "revoke_share": RevokeShareParams,
    "revoke_share_by_id": RevokeShareByIdParams,
    "list_outgoing_shares": ListOutgoingSharesParams,
    "list_incoming_shares": ListIncomingSharesParams,
    # Public access methods
    "make_public": MakePublicParams,
    "make_private": MakePrivateParams,
    "namespace_create": NamespaceCreateParams,
    "namespace_get": NamespaceGetParams,
    "namespace_list": NamespaceListParams,
    "namespace_delete": NamespaceDeleteParams,
    "register_workspace": RegisterWorkspaceParams,  # v0.5.0
    "unregister_workspace": UnregisterWorkspaceParams,  # v0.5.0
    "get_workspace_info": GetWorkspaceInfoParams,  # v0.5.0
    "list_workspaces": ListWorkspacesParams,  # v0.5.0
    "workspace_snapshot": WorkspaceSnapshotParams,  # v0.5.0
    "workspace_restore": WorkspaceRestoreParams,  # v0.5.0
    "workspace_log": WorkspaceLogParams,  # v0.5.0
    "workspace_diff": WorkspaceDiffParams,  # v0.5.0
    "register_memory": RegisterMemoryParams,  # v0.5.0
    "unregister_memory": UnregisterMemoryParams,  # v0.5.0
    "get_memory_info": GetMemoryInfoParams,  # v0.5.0
    "list_memories": ListMemoriesParams,  # v0.5.0
    "list_registered_memories": ListRegisteredMemoriesParams,  # v0.5.0
    "register_agent": RegisterAgentParams,  # v0.5.0
    "list_agents": ListAgentsParams,  # v0.5.0
    "get_agent": GetAgentParams,  # v0.5.0
    "delete_agent": DeleteAgentParams,  # v0.5.0
    # Memory API methods (v0.5.0)
    "start_trajectory": StartTrajectoryParams,
    "log_trajectory_step": LogTrajectoryStepParams,
    "complete_trajectory": CompleteTrajectoryParams,
    "query_trajectories": QueryTrajectoriesParams,
    "get_playbook": GetPlaybookParams,
    "curate_playbook": CuratePlaybookParams,
    "query_playbooks": QueryPlaybooksParams,
    "process_relearning": ProcessRelearningParams,
    "batch_reflect": BatchReflectParams,
    "store_memory": StoreMemoryParams,
    "retrieve_memory": RetrieveMemoryParams,  # v0.8.0
    "delete_memory": DeleteMemoryParams,  # v0.8.0
    "approve_memory": ApproveMemoryParams,  # #368
    "deactivate_memory": DeactivateMemoryParams,  # #368
    "approve_memory_batch": ApproveMemoryBatchParams,  # #368
    "deactivate_memory_batch": DeactivateMemoryBatchParams,  # #368
    "delete_memory_batch": DeleteMemoryBatchParams,  # #368
    "query_memories": QueryMemoriesParams,
    # Versioning methods
    "get_version": GetVersionParams,
    "list_versions": ListVersionsParams,
    "rollback": RollbackParams,
    "diff_versions": DiffVersionsParams,
    # Admin API methods (v0.5.1)
    "admin_create_key": AdminCreateKeyParams,
    "admin_list_keys": AdminListKeysParams,
    "admin_get_key": AdminGetKeyParams,
    "admin_revoke_key": AdminRevokeKeyParams,
    "admin_update_key": AdminUpdateKeyParams,
    # Sandbox management methods (v0.8.0 - Issue #372)
    "sandbox_create": SandboxCreateParams,
    "sandbox_run": SandboxRunParams,
    "sandbox_pause": SandboxPauseParams,
    "sandbox_resume": SandboxResumeParams,
    "sandbox_stop": SandboxStopParams,
    "sandbox_list": SandboxListParams,
    "sandbox_status": SandboxStatusParams,
    "sandbox_get_or_create": SandboxGetOrCreateParams,  # Issue #396
    "sandbox_connect": SandboxConnectParams,  # Issue #371
    "sandbox_disconnect": SandboxDisconnectParams,  # Issue #371
    # Mount management methods (v0.5.6 - Issue #313)
    "add_mount": AddMountParams,
    "remove_mount": RemoveMountParams,
    "list_connectors": ListConnectorsParams,  # Issue #528 - Connector registry
    "list_mounts": ListMountsParams,
    "get_mount": GetMountParams,
    "has_mount": HasMountParams,
    # Mount persistence methods
    "save_mount": SaveMountParams,
    "list_saved_mounts": ListSavedMountsParams,
    "load_mount": LoadMountParams,
    "delete_saved_mount": DeleteSavedMountParams,
    "sync_mount": SyncMountParams,
    "sync_mount_async": SyncMountAsyncParams,
    "get_sync_job": GetSyncJobParams,
    "cancel_sync_job": CancelSyncJobParams,
    "list_sync_jobs": ListSyncJobsParams,
    # Skills management methods
    "skills_create": SkillsCreateParams,
    "skills_create_from_content": SkillsCreateFromContentParams,
    "skills_create_from_file": SkillsCreateFromFileParams,
    "skills_list": SkillsListParams,
    "skills_info": SkillsInfoParams,
    "skills_fork": SkillsForkParams,
    "skills_publish": SkillsPublishParams,
    "skills_search": SkillsSearchParams,
    "skills_submit_approval": SkillsSubmitApprovalParams,
    "skills_approve": SkillsApproveParams,
    "skills_reject": SkillsRejectParams,
    "skills_list_approvals": SkillsListApprovalsParams,
    "skills_import": SkillsImportParams,
    "skills_validate_zip": SkillsValidateZipParams,
    "skills_export": SkillsExportParams,
    # OAuth management methods (v0.9.0)
    "oauth_list_providers": OAuthListProvidersParams,
    "oauth_get_auth_url": OAuthGetAuthUrlParams,
    "oauth_exchange_code": OAuthExchangeCodeParams,
    "oauth_list_credentials": OAuthListCredentialsParams,
    "oauth_revoke_credential": OAuthRevokeCredentialParams,
    "oauth_test_credential": OAuthTestCredentialParams,
    # MCP/Klavis integration methods
    "mcp_connect": MCPConnectParams,
    "mcp_list_mounts": MCPListMountsParams,
    "mcp_list_tools": MCPListToolsParams,
    "mcp_mount": MCPMountParams,
    "mcp_unmount": MCPUnmountParams,
    "mcp_sync": MCPSyncParams,
    # ACE (Adaptive Concurrency Engine) methods
    "ace_start_trajectory": AceStartTrajectoryParams,
    "ace_log_trajectory_step": AceLogTrajectoryStepParams,
    "ace_complete_trajectory": AceCompleteTrajectoryParams,
    "ace_add_feedback": AceAddFeedbackParams,
    "ace_get_trajectory_feedback": AceGetTrajectoryFeedbackParams,
    "ace_get_effective_score": AceGetEffectiveScoreParams,
    "ace_mark_for_relearning": AceMarkForRelearningParams,
    "ace_query_trajectories": AceQueryTrajectoriesParams,
    "ace_create_playbook": AceCreatePlaybookParams,
    "ace_get_playbook": AceGetPlaybookParams,
    "ace_query_playbooks": AceQueryPlaybooksParams,
}


def parse_method_params(method: str, params: dict[str, Any] | None) -> Any:
    """Parse and validate method parameters.

    Args:
        method: Method name
        params: Parameter dict

    Returns:
        Parameter dataclass instance

    Raises:
        ValueError: If method is unknown or params are invalid
    """
    if method not in METHOD_PARAMS:
        raise ValueError(f"Unknown method: {method}")

    param_class = METHOD_PARAMS[method]
    if params is None:
        params = {}

    # Convert lists to tuples for ReBAC methods (JSON deserializes tuples as lists)
    if method in [
        "rebac_create",
        "rebac_check",
        "rebac_expand",
        "rebac_list_tuples",
        "rebac_explain",
        # Cross-tenant sharing methods
        "share_with_user",
        "revoke_share",
        "list_outgoing_shares",
        # Public access methods
        "make_public",
        "make_private",
    ]:
        if "subject" in params and isinstance(params["subject"], list):
            params["subject"] = tuple(params["subject"])
        if "object" in params and isinstance(params["object"], list):
            params["object"] = tuple(params["object"])
        if "resource" in params and isinstance(params["resource"], list):
            params["resource"] = tuple(params["resource"])

    try:
        return param_class(**params)
    except TypeError as e:
        raise ValueError(f"Invalid parameters for {method}: {e}") from e
