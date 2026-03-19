"""RPC protocol types for Nexus JSON-RPC communication.

Extracted from server/protocol.py (Issue #1519, 1A) so that core/
modules (rpc_transport) can use these types without importing from
the server layer.  (rpc_codec moved to nexus.contracts, Issue #2054.)

These are pure data types with no dependencies beyond the standard library.
"""

from dataclasses import dataclass
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

    # Keys that are part of the JSON-RPC envelope, not user params.
    _ENVELOPE_KEYS = frozenset({"jsonrpc", "id", "method", "params"})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RPCRequest":
        """Create request from dict.

        If the body contains an explicit ``"params"`` key, use it.
        Otherwise treat any non-envelope keys in the body as bare params
        so that ``POST /api/nfs/list_versions {"path": "/foo"}`` works
        the same as ``{"params": {"path": "/foo"}}``.
        """
        explicit_params = data.get("params")
        if explicit_params is None:
            bare = {k: v for k, v in data.items() if k not in cls._ENVELOPE_KEYS}
            if bare:
                explicit_params = bare
        return cls(
            jsonrpc=data.get("jsonrpc", "2.0"),
            id=data.get("id"),
            method=data.get("method", ""),
            params=explicit_params,
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
    def success(cls, request_id: str | int | None, result: Any) -> "RPCResponse":
        """Create success response."""
        return cls(id=request_id, result=result, error=None)

    @classmethod
    def create_error(
        cls,
        request_id: str | int | None,
        code: RPCErrorCode,
        message: str,
        data: Any = None,
        is_expected: bool = False,
    ) -> "RPCResponse":
        """Create error response.

        Args:
            request_id: The request ID to respond to
            code: The error code
            message: Human-readable error message
            data: Optional additional error data
            is_expected: Whether this is an expected error (user error) vs
                        unexpected (system error). Used for logging/alerting.
        """
        error_dict: dict[str, Any] = {
            "code": code.value,
            "message": message,
            "is_expected": is_expected,
        }
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
