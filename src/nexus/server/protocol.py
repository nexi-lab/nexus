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
from datetime import datetime
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


def encode_rpc_message(data: dict[str, Any]) -> bytes:
    """Encode RPC message to JSON bytes."""
    return json.dumps(data, cls=RPCEncoder).encode("utf-8")


def decode_rpc_message(data: bytes) -> dict[str, Any]:
    """Decode RPC message from JSON bytes."""
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


@dataclass
class WriteParams:
    """Parameters for write() method."""

    path: str
    content: bytes
    if_match: str | None = None  # Optimistic concurrency control
    if_none_match: bool = False  # Create-only mode
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
class ExistsParams:
    """Parameters for exists() method."""

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
    max_results: int = 1000


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
    """Parameters for register_agent() method (v0.5.0)."""

    agent_id: str
    name: str
    description: str | None = None
    generate_api_key: bool = False


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


# Mapping of method names to parameter dataclasses
METHOD_PARAMS = {
    "read": ReadParams,
    "write": WriteParams,
    "delete": DeleteParams,
    "rename": RenameParams,
    "exists": ExistsParams,
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
    "register_agent": RegisterAgentParams,  # v0.5.0
    "list_agents": ListAgentsParams,  # v0.5.0
    "get_agent": GetAgentParams,  # v0.5.0
    "delete_agent": DeleteAgentParams,  # v0.5.0
    # Versioning methods
    "get_version": GetVersionParams,
    "list_versions": ListVersionsParams,
    "rollback": RollbackParams,
    "diff_versions": DiffVersionsParams,
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
    ]:
        if "subject" in params and isinstance(params["subject"], list):
            params["subject"] = tuple(params["subject"])
        if "object" in params and isinstance(params["object"], list):
            params["object"] = tuple(params["object"])

    try:
        return param_class(**params)
    except TypeError as e:
        raise ValueError(f"Invalid parameters for {method}: {e}") from e
