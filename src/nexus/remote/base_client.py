"""Base class for remote Nexus transport layer.

Provides shared non-I/O logic used by RemoteBackend, RemoteMetastore,
and the REMOTE deployment profile transport layer.

Shared concerns:
- Negative cache management (via injectable NegativeCache protocol)
- RPC error handling and exception mapping
- Response parsing (base64 decoding, bytes format handling)
- Zone/agent identity properties
"""

import base64
import logging
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.contracts.rpc_types import RPCErrorCode
from nexus.remote.negative_cache import NegativeCache, create_negative_cache

logger = logging.getLogger(__name__)


class BaseRemoteNexusFS:
    """Base class containing shared non-I/O logic for remote transport.

    Used by RemoteBackend and RemoteMetastore for error handling and
    response parsing. The _call_rpc() transport is provided by the
    concrete backend/metastore implementations.
    """

    _negative_cache: NegativeCache
    _zone_id: str | None
    _agent_id: str | None
    # Codex review #3 finding #1: identity fields populated from
    # ``/api/auth/whoami`` so callers (e.g. the MCP server) can build
    # an explicit ``OperationContext`` from the remote connection's
    # authenticated subject instead of falling back to whatever the
    # server-side auth context happens to be.
    _subject_id: str | None = None
    _subject_type: str | None = None
    _is_admin: bool = False

    # ============================================================
    # Negative Cache
    # ============================================================

    def _init_negative_cache(
        self,
        negative_cache: NegativeCache | None = None,
        capacity: int = 100_000,
        fp_rate: float = 0.01,
    ) -> None:
        """Initialize or inject the negative cache.

        Args:
            negative_cache: Pre-built cache instance (DI). If None, one is
                created via the factory using capacity/fp_rate.
            capacity: Bloom filter capacity (ignored when negative_cache given).
            fp_rate: Bloom filter false-positive rate (ignored when
                negative_cache given).
        """
        if negative_cache is not None:
            self._negative_cache = negative_cache
        else:
            self._negative_cache = create_negative_cache(capacity, fp_rate)

    def _negative_cache_key(self, path: str) -> str:
        """Generate cache key with zone isolation."""
        return f"{self._zone_id or ROOT_ZONE_ID}:{path}"

    def _negative_cache_check(self, path: str) -> bool:
        """Check if path is known to not exist (in negative cache).

        Returns:
            True if path is definitely non-existent (skip RPC)
            False if path might exist (need to check server)
        """
        key = self._negative_cache_key(path)
        return self._negative_cache.check(key)

    def _negative_cache_add(self, path: str) -> None:
        """Add path to negative cache (file confirmed to not exist)."""
        key = self._negative_cache_key(path)
        self._negative_cache.add(key)

    def _negative_cache_invalidate(self, path: str) -> None:
        """Invalidate negative cache for path.

        Bloom filters don't support per-key deletion, so the underlying
        cache is cleared entirely. This is acceptable because write/delete
        operations are less frequent than reads and the cache repopulates
        naturally.
        """
        self._negative_cache.clear()
        logger.debug("Negative cache cleared due to write/delete of %s", path)

    def _negative_cache_invalidate_bulk(self, paths: list[str]) -> None:
        """Invalidate negative cache for multiple paths."""
        if not paths:
            return
        self._negative_cache.clear()
        logger.debug("Negative cache cleared due to bulk write/delete of %d paths", len(paths))

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

    @property
    def subject_id(self) -> str | None:
        """Authenticated subject id (user or agent) for this connection.

        Populated from ``/api/auth/whoami`` during connect. Used by
        the MCP server to build an explicit ``OperationContext``
        (Codex review #3 finding #1).
        """
        return self._subject_id

    @property
    def subject_type(self) -> str | None:
        """Authenticated subject type (``user`` / ``agent`` / ``service``)."""
        return self._subject_type

    @property
    def is_admin(self) -> bool:
        """Whether the authenticated subject has admin privileges.

        Populated from ``/api/auth/whoami``; defaults to ``False``
        until whoami completes. Used by the MCP server to build an
        explicit ``OperationContext``.
        """
        return self._is_admin

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
            # Extract content_id info from data
            expected_content_id = data.get("expected_content_id") if data else "(unknown)"
            current_content_id = data.get("current_content_id") if data else "(unknown)"
            path = data.get("path") if data else "unknown"
            raise ConflictError(path, expected_content_id, current_content_id)
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
            subject_type = auth_info.get("subject_type")
            subject_id = auth_info.get("subject_id")
            # Codex review #3 finding #1: retain the full subject + is_admin
            # so the MCP server (and any other caller) can build an explicit
            # ``OperationContext`` from the remote connection's authenticated
            # identity. Previously only ``_zone_id`` and ``_agent_id`` were
            # stored, so user-typed subjects lost their identity on the
            # client side and any downstream caller had to re-query whoami.
            self._subject_type = subject_type
            self._subject_id = subject_id
            self._is_admin = bool(auth_info.get("is_admin", False))
            # Only set agent_id if subject_type is "agent"
            # For users, agent_id should remain None
            if subject_type == "agent":
                self._agent_id = subject_id
            else:
                self._agent_id = None
            logger.info(
                f"Authenticated as {subject_type}:{subject_id} "
                f"(zone: {self._zone_id}, admin: {self._is_admin})"
            )
        else:
            logger.debug("Not authenticated (anonymous access)")
