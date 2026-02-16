"""Core RPC wire-format protocol types.

These types define the JSON-RPC wire format shared between client and server.
They live in core/ so that both core/rpc_transport.py (client) and
server/protocol.py (server) can import them without a kernel→server dependency.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


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
        is_expected: bool = False,
    ) -> RPCResponse:
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
        elif hasattr(obj, "__slots__"):
            # Handle slotted dataclasses (slots=True) which have no __dict__
            from dataclasses import fields, is_dataclass

            if is_dataclass(obj):
                return {f.name: getattr(obj, f.name) for f in fields(obj)}
        return super().default(obj)


def rpc_decode_hook(obj: Any) -> Any:
    """Decode hook for special types."""
    if isinstance(obj, dict) and "__type__" in obj:
        if obj["__type__"] == "bytes":
            return base64.b64decode(obj["data"])
        elif obj["__type__"] == "datetime":
            return datetime.fromisoformat(obj["data"])
        elif obj["__type__"] == "timedelta":
            return timedelta(seconds=obj["seconds"])
    return obj


# Try to import orjson for faster JSON serialization (2-3x faster)
try:
    import orjson

    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False


def _prepare_for_orjson(obj: Any) -> Any:
    """Convert objects to orjson-compatible types for encoding responses."""
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
    elif hasattr(obj, "__slots__"):
        from dataclasses import fields, is_dataclass

        if is_dataclass(obj):
            return {f.name: _prepare_for_orjson(getattr(obj, f.name)) for f in fields(obj)}
        return obj
    else:
        return obj


def _apply_decode_hook(obj: Any) -> Any:
    """Recursively apply rpc_decode_hook to convert special types after orjson parsing."""
    if isinstance(obj, dict):
        if "__type__" in obj:
            return rpc_decode_hook(obj)
        return {k: _apply_decode_hook(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_apply_decode_hook(item) for item in obj]
    else:
        return obj


def encode_rpc_message(data: dict[str, Any]) -> bytes:
    """Encode RPC message to JSON bytes (uses orjson if available for 2-3x speedup)."""
    import time

    start = time.time()

    if HAS_ORJSON:
        prepared_data = _prepare_for_orjson(data)
        result: bytes = orjson.dumps(prepared_data)
        elapsed = (time.time() - start) * 1000
        logger.debug(f"[RPC-PERF] orjson encode: {len(result)} bytes in {elapsed:.1f}ms")
        return result
    else:
        result_json: bytes = json.dumps(data, cls=RPCEncoder).encode("utf-8")
        elapsed = (time.time() - start) * 1000
        logger.debug(
            f"[RPC-PERF] standard json encode: {len(result_json)} bytes in {elapsed:.1f}ms"
        )
        return result_json


def decode_rpc_message(data: bytes) -> dict[str, Any]:
    """Decode RPC message from JSON bytes (uses orjson if available)."""
    if HAS_ORJSON:
        parsed = orjson.loads(data)
        result: dict[str, Any] = _apply_decode_hook(parsed)
        return result
    else:
        decoded: dict[str, Any] = json.loads(data.decode("utf-8"), object_hook=rpc_decode_hook)
        return decoded
