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
    """

    def default(self, obj: Any) -> Any:
        """Encode special types."""
        if isinstance(obj, bytes):
            return {"__type__": "bytes", "data": base64.b64encode(obj).decode("utf-8")}
        elif isinstance(obj, datetime):
            return {"__type__": "datetime", "data": obj.isoformat()}
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
    return obj


def encode_rpc_message(data: dict[str, Any]) -> bytes:
    """Encode RPC message to JSON bytes."""
    return json.dumps(data, cls=RPCEncoder).encode("utf-8")


def decode_rpc_message(data: bytes) -> dict[str, Any]:
    """Decode RPC message from JSON bytes."""
    return json.loads(data.decode("utf-8"), object_hook=rpc_decode_hook)  # type: ignore[no-any-return]


# ============================================================
# Method-specific parameter schemas
# ============================================================


@dataclass
class ReadParams:
    """Parameters for read() method."""

    path: str


@dataclass
class WriteParams:
    """Parameters for write() method."""

    path: str
    content: bytes


@dataclass
class DeleteParams:
    """Parameters for delete() method."""

    path: str


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


# Mapping of method names to parameter dataclasses
METHOD_PARAMS = {
    "read": ReadParams,
    "write": WriteParams,
    "delete": DeleteParams,
    "exists": ExistsParams,
    "list": ListParams,
    "glob": GlobParams,
    "grep": GrepParams,
    "mkdir": MkdirParams,
    "rmdir": RmdirParams,
    "is_directory": IsDirectoryParams,
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

    try:
        return param_class(**params)
    except TypeError as e:
        raise ValueError(f"Invalid parameters for {method}: {e}") from e
