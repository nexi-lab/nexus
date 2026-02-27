"""Sync gRPC transport client for the REMOTE deployment profile.

Client-side gRPC transport that replaces the duplicated HTTP/JSON-RPC
transport (httpx + tenacity) in ``RemoteBackend`` and ``RemoteMetastore``
with a single gRPC channel.

Phase 1 (PR #2667): Generic ``call_rpc()`` — method name + JSON payload.
Phase 2: Typed methods (``read_file``, ``write_file``, ``delete_file``,
``stream_read``, ``ping``) with native ``bytes`` fields — no JSON/base64
overhead for content operations.

Generic ``call_rpc()`` stays for 25+ service proxy methods and metadata ops.
Typed methods only for content-heavy operations and health checks.

Issue #1133: Unified gRPC transport.
Issue #1202: gRPC for REMOTE profile.
"""

from __future__ import annotations

import logging
from typing import Any

import grpc
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nexus.contracts.exceptions import (
    RemoteConnectionError,
    RemoteTimeoutError,
)
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.remote.base_client import BaseRemoteNexusFS

logger = logging.getLogger(__name__)

# 64 MB max message size (matches server config)
_MAX_MESSAGE_LENGTH = 64 * 1024 * 1024

_CHANNEL_OPTIONS = [
    ("grpc.max_send_message_length", _MAX_MESSAGE_LENGTH),
    ("grpc.max_receive_message_length", _MAX_MESSAGE_LENGTH),
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
]


class RPCTransport:
    """Sync gRPC transport — replaces HTTP/JSON-RPC in RemoteBackend & RemoteMetastore.

    Creates a single ``grpc.Channel`` with automatic keepalive and retry.
    All RPC calls go through the generic ``NexusVFSService.Call`` endpoint
    which dispatches to the same handler pipeline as the HTTP endpoint.

    Args:
        server_address: gRPC server address (e.g. ``localhost:2028``).
        auth_token: Optional Bearer token for authentication.
        timeout: Default RPC timeout in seconds.
        connect_timeout: Channel connectivity check timeout in seconds.
    """

    def __init__(
        self,
        server_address: str,
        auth_token: str | None = None,
        timeout: float = 90.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self.server_address = server_address
        self._auth_token = auth_token or ""
        self._timeout = timeout
        self._connect_timeout = connect_timeout

        self._channel: grpc.Channel = grpc.insecure_channel(
            server_address, options=_CHANNEL_OPTIONS
        )
        self._stub = vfs_pb2_grpc.NexusVFSServiceStub(self._channel)

        # Reuse BaseRemoteNexusFS error handling (static method access)
        self._error_handler = BaseRemoteNexusFS()

    # ------------------------------------------------------------------
    # RPC call
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def call_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        """Make a gRPC call to the server with automatic retry.

        Args:
            method: RPC method name (e.g. ``sys_read``).
            params: Parameter dict (JSON-serialised via rpc_codec).
            read_timeout: Per-call timeout override in seconds.

        Returns:
            Decoded result from the server.

        Raises:
            RemoteConnectionError: Channel is not reachable.
            RemoteTimeoutError: Call exceeded deadline.
            NexusError subclasses: Application-level errors from server.
        """
        payload = encode_rpc_message(params or {})
        request = vfs_pb2.CallRequest(
            method=method,
            payload=payload,
            auth_token=self._auth_token,
        )
        timeout = read_timeout if read_timeout is not None else self._timeout

        logger.debug("RPCTransport.call_rpc: %s params=%s", method, params)

        try:
            response = self._stub.Call(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, method)

        # Application-level error (gRPC status OK, but is_error flag set)
        if response.is_error:
            error_dict = decode_rpc_message(response.payload)
            logger.error(
                "RPCTransport RPC error: %s — %s",
                method,
                error_dict.get("message"),
            )
            self._error_handler._handle_rpc_error(error_dict)

        result = decode_rpc_message(response.payload)
        logger.debug("RPCTransport.call_rpc OK: %s", method)
        return result

    # ------------------------------------------------------------------
    # Typed RPCs — content operations (Phase 2)
    # ------------------------------------------------------------------

    def _handle_typed_error(self, error_payload: bytes) -> None:
        """Decode error_payload and raise the appropriate NexusError."""
        error_dict = decode_rpc_message(error_payload)
        self._error_handler._handle_rpc_error(error_dict)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def read_file(self, path: str, read_timeout: float | None = None) -> bytes:
        """Read file content via typed Read RPC — no JSON/base64 overhead.

        Returns:
            Raw file content as bytes.
        """
        request = vfs_pb2.ReadRequest(path=path, auth_token=self._auth_token)
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Read(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Read")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return bytes(response.content)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def write_file(
        self,
        path: str,
        content: bytes,
        etag: str | None = None,
        read_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Write file content via typed Write RPC — no JSON/base64 overhead.

        Returns:
            Dict with ``etag`` and ``size``.
        """
        request = vfs_pb2.WriteRequest(
            path=path, content=content, auth_token=self._auth_token, etag=etag or ""
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Write(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Write")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return {"etag": response.etag, "size": response.size}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def delete_file(
        self,
        path: str,
        recursive: bool = False,
        read_timeout: float | None = None,
    ) -> bool:
        """Delete file or directory via typed Delete RPC.

        Returns:
            True on success.
        """
        request = vfs_pb2.DeleteRequest(path=path, auth_token=self._auth_token, recursive=recursive)
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Delete(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Delete")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return bool(response.success)

    def stream_read(
        self,
        path: str,
        chunk_size: int = 1_048_576,
        read_timeout: float | None = None,
    ) -> bytes:
        """Read large file via streaming RPC — assembles chunks client-side.

        Returns:
            Complete file content as bytes.
        """
        request = vfs_pb2.StreamReadRequest(
            path=path, auth_token=self._auth_token, chunk_size=chunk_size
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        chunks: list[bytes] = []
        try:
            for chunk in self._stub.StreamRead(request, timeout=timeout):
                if chunk.is_error:
                    self._handle_typed_error(chunk.error_payload)
                chunks.append(chunk.data)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "StreamRead")
        return b"".join(chunks)

    def ping(self) -> dict[str, Any]:
        """Ping server — returns version, zone_id, uptime."""
        request = vfs_pb2.PingRequest(auth_token=self._auth_token)
        try:
            response = self._stub.Ping(request, timeout=self._connect_timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, self._connect_timeout, "Ping")
        return {
            "version": response.version,
            "zone_id": response.zone_id,
            "uptime": response.uptime_seconds,
        }

    # ------------------------------------------------------------------
    # Transport error handling (shared)
    # ------------------------------------------------------------------

    def _raise_transport_error(self, exc: grpc.RpcError, timeout: float, method: str) -> None:
        """Convert gRPC transport errors to RemoteConnectionError/RemoteTimeoutError."""
        code = exc.code()
        details = exc.details()
        if code == grpc.StatusCode.UNAVAILABLE:
            raise RemoteConnectionError(
                f"gRPC server unavailable: {details}",
                details={"server_address": self.server_address},
                method=method,
            ) from exc
        if code == grpc.StatusCode.DEADLINE_EXCEEDED:
            raise RemoteTimeoutError(
                f"gRPC call timed out after {timeout}s",
                details={"timeout": timeout, "server_address": self.server_address},
                method=method,
            ) from exc
        raise

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """Check server health via Ping RPC.

        Returns:
            True if the server responds to Ping.

        Raises:
            RemoteConnectionError: Server is unreachable.
        """
        try:
            self.ping()
            return True
        except (grpc.RpcError, RemoteConnectionError) as exc:
            raise RemoteConnectionError(
                f"gRPC health check failed: {exc}",
                details={"server_address": self.server_address},
            ) from exc

    def close(self) -> None:
        """Close the gRPC channel."""
        self._channel.close()
