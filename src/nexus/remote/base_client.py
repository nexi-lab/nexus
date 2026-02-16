"""Base class for remote Nexus filesystem clients.

Extracts shared non-I/O logic used by both RemoteNexusFS (sync) and
AsyncRemoteNexusFS (async) to eliminate code duplication.

Shared concerns:
- Negative cache (Bloom filter) management
- RPC error handling and exception mapping
- Response parsing (base64 decoding, bytes format handling)
- Zone/agent identity properties
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from nexus.core.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.core.rpc_protocol import RPCErrorCode

logger = logging.getLogger(__name__)


class BaseRemoteNexusFS:
    """Base class containing shared non-I/O logic for remote clients.

    Subclasses (RemoteNexusFS, AsyncRemoteNexusFS) provide the actual
    HTTP transport via _call_rpc() (sync or async).
    """

    # Attributes set by subclass __init__ before calling _init_negative_cache
    _negative_cache_capacity: int
    _negative_cache_fp_rate: float
    _negative_bloom: Any
    _zone_id: str | None
    _agent_id: str | None

    # ============================================================
    # Negative Cache (Bloom Filter)
    # ============================================================

    def _init_negative_cache(self) -> None:
        """Initialize Bloom filter for negative caching of non-existent files.

        Expects self._negative_cache_capacity, self._negative_cache_fp_rate
        to be set before calling.
        """
        try:
            from nexus_fast import BloomFilter

            self._negative_bloom = BloomFilter(
                self._negative_cache_capacity, self._negative_cache_fp_rate
            )
            logger.debug(
                f"Negative cache initialized: capacity={self._negative_cache_capacity}, "
                f"fp_rate={self._negative_cache_fp_rate}, memory={self._negative_bloom.memory_bytes} bytes"
            )
        except ImportError:
            logger.debug("nexus_fast not available, negative cache disabled")
            self._negative_bloom = None
        except Exception as e:
            logger.warning(f"Failed to initialize negative cache: {e}")
            self._negative_bloom = None

    def _negative_cache_key(self, path: str) -> str:
        """Generate cache key with zone isolation."""
        return f"{self._zone_id or 'default'}:{path}"

    def _negative_cache_check(self, path: str) -> bool:
        """Check if path is known to not exist (in negative cache).

        Returns:
            True if path is definitely non-existent (skip RPC)
            False if path might exist (need to check server)
        """
        if self._negative_bloom is None:
            return False
        key = self._negative_cache_key(path)
        return bool(self._negative_bloom.might_exist(key))

    def _negative_cache_add(self, path: str) -> None:
        """Add path to negative cache (file confirmed to not exist)."""
        if self._negative_bloom is None:
            return
        key = self._negative_cache_key(path)
        self._negative_bloom.add(key)

    def _negative_cache_invalidate(self, path: str) -> None:
        """Invalidate negative cache entry for path.

        Note: Bloom filters don't support deletion, so we clear the entire filter
        when invalidation is needed. This is acceptable because:
        1. Write/delete operations are less frequent than reads
        2. The filter will repopulate naturally as files are checked
        """
        if self._negative_bloom is None:
            return
        # Bloom filters can't delete individual keys, so we clear the entire filter
        # This is a trade-off: occasional full clear vs. complex counting bloom filter
        self._negative_bloom.clear()
        logger.debug(f"Negative cache cleared due to write/delete of {path}")

    def _negative_cache_invalidate_bulk(self, paths: list[str]) -> None:
        """Invalidate negative cache for multiple paths."""
        if self._negative_bloom is None or not paths:
            return
        self._negative_bloom.clear()
        logger.debug(f"Negative cache cleared due to bulk write/delete of {len(paths)} paths")

    # ============================================================
    # Zone / Agent Identity Properties
    # ============================================================

    @property
    def zone_id(self) -> str | None:
        """Zone ID for this filesystem instance."""
        return self._zone_id

    @zone_id.setter
    def zone_id(self, value: str | None) -> None:
        """Set zone ID for this filesystem instance."""
        self._zone_id = value

    @property
    def agent_id(self) -> str | None:
        """Agent ID for this filesystem instance."""
        return self._agent_id

    @agent_id.setter
    def agent_id(self, value: str | None) -> None:
        """Set agent ID for this filesystem instance."""
        self._agent_id = value

    # ============================================================
    # RPC Error Handling
    # ============================================================

    def _handle_rpc_error(self, error: dict[str, Any]) -> None:
        """Handle RPC error response by raising the appropriate exception.

        Maps RPC error codes to NexusError subclasses.

        Args:
            error: Error dict from RPC response with 'code', 'message', 'data' keys

        Raises:
            Appropriate NexusError subclass based on error code
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
    # Response Parsing Helpers
    # ============================================================

    def _parse_read_response(
        self, result: Any, return_metadata: bool = False
    ) -> bytes | dict[str, Any]:
        """Parse RPC response from a read() call, decoding content as needed.

        Handles two response formats:
        1. Standard bytes format: {"__type__": "bytes", "data": "<base64>"}
        2. Legacy format: {"content": "<base64>", "encoding": "base64"}

        Args:
            result: Raw RPC result
            return_metadata: If True, return dict with content and metadata

        Returns:
            Decoded bytes content, or dict with decoded content if return_metadata=True
        """
        # Handle standard bytes format: {__type__: 'bytes', data: '...'}
        # This is the format from encode_rpc_message in protocol.py
        if isinstance(result, dict) and result.get("__type__") == "bytes" and "data" in result:
            decoded_content = base64.b64decode(result["data"])
            if return_metadata:
                return {"content": decoded_content}
            return decoded_content

        # Handle legacy format: {content: '...', encoding: 'base64'}
        # (kept for backward compatibility with older servers)
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            encoding = result.get("encoding", "base64")

            # Decode base64 content to bytes
            if encoding == "base64" and isinstance(content, str):
                decoded_content = base64.b64decode(content)
            elif isinstance(content, bytes):
                decoded_content = content
            else:
                # Already decoded or unknown format
                decoded_content = content.encode() if isinstance(content, str) else content

            if return_metadata:
                # Return new dict with decoded content and metadata
                return {**result, "content": decoded_content}
            else:
                # Return just the bytes
                return decoded_content

        # Handle raw bytes (if result is already bytes)
        if isinstance(result, bytes):
            return result

        return result  # type: ignore[no-any-return]

    def _decode_bytes_field(self, value: Any) -> bytes:
        """Decode a single bytes-typed field from RPC response.

        Handles:
        - {"__type__": "bytes", "data": "<base64>"} -> decoded bytes
        - base64 string -> decoded bytes
        - raw bytes -> pass-through

        Args:
            value: Field value from RPC response

        Returns:
            Decoded bytes
        """
        if isinstance(value, dict) and value.get("__type__") == "bytes" and "data" in value:
            return base64.b64decode(value["data"])
        if isinstance(value, str):
            return base64.b64decode(value)
        return value  # type: ignore[no-any-return]

    def _decode_delta_read_response(self, result: Any) -> dict[str, Any]:
        """Decode binary fields in a delta_read RPC response.

        Decodes 'delta' and 'content' fields if they are in the standard
        bytes format ({"__type__": "bytes", "data": "<base64>"}).

        Args:
            result: Raw RPC result from delta_read

        Returns:
            Result with binary fields decoded to bytes
        """
        if not isinstance(result, dict):
            return {"result": result}

        decoded = dict(result)

        # Decode delta if present
        if (
            "delta" in decoded
            and isinstance(decoded["delta"], dict)
            and decoded["delta"].get("__type__") == "bytes"
        ):
            decoded["delta"] = base64.b64decode(decoded["delta"]["data"])

        # Decode content if present
        if (
            "content" in decoded
            and isinstance(decoded["content"], dict)
            and decoded["content"].get("__type__") == "bytes"
        ):
            decoded["content"] = base64.b64decode(decoded["content"]["data"])

        return decoded

    def _parse_auth_info(self, auth_info: dict[str, Any]) -> None:
        """Parse authentication info response and set zone/agent identity.

        Args:
            auth_info: Response dict from /api/auth/whoami endpoint
        """
        if auth_info.get("authenticated"):
            self._zone_id = auth_info.get("zone_id")
            # Only set agent_id if subject_type is "agent"
            # For users, agent_id should remain None
            subject_type = auth_info.get("subject_type")
            if subject_type == "agent":
                self._agent_id = auth_info.get("subject_id")
            else:
                self._agent_id = None
            logger.info(
                f"Authenticated as {subject_type}:{auth_info.get('subject_id')} "
                f"(zone: {self._zone_id})"
            )
        else:
            logger.debug("Not authenticated (anonymous access)")
