"""Sync gRPC transport client for the REMOTE deployment profile.

Client-side gRPC transport that replaces the duplicated HTTP/JSON-RPC
transport (httpx + tenacity) in ``RemoteBackend`` and ``RemoteMetastore``
with a single gRPC channel.

The underlying ``NexusVFSService`` gRPC endpoint is generic — the server
servicer dispatches to the same ``dispatch_method()`` pipeline as the HTTP
``/api/nfs/{method}`` endpoint.  This makes the gRPC endpoint usable by
any client (REMOTE profile, federation peers, CLI tools), though this
particular class lives in ``nexus.remote`` for the REMOTE profile use case.

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
                    details={
                        "timeout": timeout,
                        "server_address": self.server_address,
                    },
                    method=method,
                ) from exc
            # Other gRPC transport errors — let tenacity retry
            raise

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
    # Lifecycle
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """Check gRPC channel connectivity.

        Returns:
            True if the channel is ready within ``connect_timeout``.

        Raises:
            RemoteConnectionError: Channel failed to connect.
        """
        try:
            grpc.channel_ready_future(self._channel).result(timeout=self._connect_timeout)
            return True
        except grpc.FutureTimeoutError as exc:
            raise RemoteConnectionError(
                f"gRPC health check timed out after {self._connect_timeout}s",
                details={"server_address": self.server_address},
            ) from exc

    def close(self) -> None:
        """Close the gRPC channel."""
        self._channel.close()
