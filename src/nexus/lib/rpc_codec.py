"""RPC message codec — encode/decode JSON-RPC payloads.

Tier-neutral serialization utilities used by server, proxy, remote, and
service layers.  Lives in ``nexus.lib`` because it has **zero** kernel
dependencies (only stdlib + optional orjson).

Extracted from server.protocol to avoid cross-layer imports
(proxy/core should NOT depend on server).

Handles special types:
- bytes → base64-encoded with __type__ wrapper
- datetime/date → ISO format with __type__ wrapper
- timedelta → total seconds with __type__ wrapper
"""

import base64
import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class RPCEncoder(json.JSONEncoder):
    """Custom JSON encoder for RPC messages.

    Handles special types:
    - bytes: base64-encoded strings
    - datetime: ISO format strings
    - timedelta: total seconds
    """

    def default(self, obj: Any) -> Any:
        """Encode special types."""
        if isinstance(obj, bytes):
            return {"__type__": "bytes", "data": base64.b64encode(obj).decode("utf-8")}
        elif isinstance(obj, date) and not isinstance(obj, datetime):
            return {"__type__": "date", "data": obj.isoformat()}
        elif isinstance(obj, datetime):
            return {"__type__": "datetime", "data": obj.isoformat()}
        elif isinstance(obj, type(obj)) and obj.__class__.__name__ == "timedelta":
            from datetime import timedelta

            if isinstance(obj, timedelta):
                return {"__type__": "timedelta", "seconds": obj.total_seconds()}
        elif hasattr(obj, "__dict__"):
            return {
                k: v for k, v in obj.__dict__.items() if not k.startswith("_") and not callable(v)
            }
        elif hasattr(obj, "__slots__"):
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
        elif obj["__type__"] == "date":
            return date.fromisoformat(obj["data"])
        elif obj["__type__"] == "timedelta":
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
    elif isinstance(obj, datetime):
        return {"__type__": "datetime", "data": obj.isoformat()}
    elif isinstance(obj, date):
        return {"__type__": "date", "data": obj.isoformat()}
    elif isinstance(obj, timedelta):
        return {"__type__": "timedelta", "seconds": obj.total_seconds()}
    elif isinstance(obj, dict):
        return {k: _prepare_for_orjson(v) for k, v in obj.items()}
    elif isinstance(obj, list | tuple):
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
    """Recursively apply rpc_decode_hook to convert special types after orjson parsing.

    orjson doesn't support object_hook, so we apply it manually after parsing.
    """
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
    start = time.time()

    if HAS_ORJSON:
        prepared_data = _prepare_for_orjson(data)
        result: bytes = orjson.dumps(prepared_data)
        if logger.isEnabledFor(logging.DEBUG):
            elapsed = (time.time() - start) * 1000
            logger.debug("[RPC-PERF] orjson encode: %d bytes in %.1fms", len(result), elapsed)
        return result
    else:
        result_json: bytes = json.dumps(data, cls=RPCEncoder).encode("utf-8")
        if logger.isEnabledFor(logging.DEBUG):
            elapsed = (time.time() - start) * 1000
            logger.debug(
                "[RPC-PERF] standard json encode: %d bytes in %.1fms", len(result_json), elapsed
            )
        return result_json


def decode_rpc_message(data: bytes) -> dict[str, Any]:
    """Decode RPC message from JSON bytes (uses orjson if available).

    When orjson is used, we apply the decode hook manually after parsing
    to convert special types like {"__type__": "bytes", "data": "..."} back to bytes.
    """
    if HAS_ORJSON:
        parsed = orjson.loads(data)
        result: dict[str, Any] = _apply_decode_hook(parsed)
        return result
    else:
        result_std: dict[str, Any] = json.loads(data.decode("utf-8"), object_hook=rpc_decode_hook)
        return result_std
