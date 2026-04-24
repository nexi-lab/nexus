"""Sync gRPC transport client for the REMOTE deployment profile.

Client-side gRPC transport that replaces the duplicated HTTP/JSON-RPC
transport (httpx + tenacity) in ``RemoteBackend`` and ``RemoteMetastore`` (now Rust)
with a single gRPC channel.

Phase 1 (PR #2667): Generic ``call_rpc()`` — method name + JSON payload.
Phase 2: Typed methods (``read_file``, ``write_file``, ``delete_file``,
``ping``) with native ``bytes`` fields — no JSON/base64
overhead for content operations.

Generic ``call_rpc()`` stays for 25+ service proxy methods and metadata ops.
Typed methods only for content-heavy operations and health checks.

Issue #1133: Unified gRPC transport.
Issue #1202: gRPC for REMOTE profile.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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
from nexus.grpc.defaults import build_channel_options
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.remote.base_client import BaseRemoteNexusFS

if TYPE_CHECKING:
    from nexus.security.tls.config import ZoneTlsConfig

logger = logging.getLogger(__name__)

_CHANNEL_OPTIONS = build_channel_options(
    keepalive_time_ms=30_000,
    keepalive_timeout_ms=10_000,
)


class RPCTransport:
    """Sync gRPC transport — replaces HTTP/JSON-RPC in RemoteBackend & RemoteMetastore (now Rust).

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
        *,
        tls_config: ZoneTlsConfig | None = None,
        peer_ca_pem: bytes | None = None,
    ) -> None:
        self.server_address = server_address
        self._auth_token = auth_token or ""
        self._timeout = timeout
        self._connect_timeout = connect_timeout

        if tls_config is not None:
            root_ca = peer_ca_pem if peer_ca_pem is not None else tls_config.ca_pem
            creds = grpc.ssl_channel_credentials(
                root_certificates=root_ca,
                private_key=tls_config.node_key_pem,
                certificate_chain=tls_config.node_cert_pem,
            )
            self._channel: grpc.Channel = grpc.secure_channel(
                server_address, creds, options=_CHANNEL_OPTIONS
            )
        else:
            host = server_address.rsplit(":", 1)[0].strip("[]")
            import ipaddress as _ipaddress
            import os as _os

            _is_local = host == "localhost"
            if not _is_local:
                try:
                    _is_local = _ipaddress.ip_address(host).is_loopback
                except ValueError:
                    _is_local = False
            # Escape hatch for trusted private networks (docker-compose, k8s
            # pods on a shared network namespace). Default refuses insecure
            # non-loopback to prevent accidental plaintext-over-internet.
            _allow_insecure = _os.environ.get("NEXUS_GRPC_ALLOW_INSECURE", "").lower() in (
                "1",
                "true",
                "yes",
            )
            if not _is_local and not _allow_insecure:
                raise ValueError(
                    f"Insecure gRPC channel refused for non-loopback address '{server_address}'. "
                    "Configure TLS for remote connections, or set "
                    "NEXUS_GRPC_ALLOW_INSECURE=true for trusted private networks "
                    "(docker-compose, k8s pod-local)."
                )
            self._channel = grpc.insecure_channel(server_address, options=_CHANNEL_OPTIONS)
        self._stub = vfs_pb2_grpc.NexusVFSServiceStub(self._channel)

        # Pre-warm: trigger eager TCP/TLS handshake so connection establishment
        # overlaps with NexusFS construction instead of blocking on first RPC.
        self._channel_ready = grpc.channel_ready_future(self._channel)

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
        auth_token: str | None = None,
    ) -> Any:
        """Make a gRPC call to the server with automatic retry.

        Args:
            method: RPC method name (e.g. ``sys_read``).
            params: Parameter dict (JSON-serialised via rpc_codec).
            read_timeout: Per-call timeout override in seconds.
            auth_token: Override the default auth token for this call.
                Used by federated search to pass SearchDelegation credentials
                for cross-zone queries (Issue #3147).

        Returns:
            Decoded result from the server.

        Raises:
            RemoteConnectionError: Channel is not reachable.
            RemoteTimeoutError: Call exceeded deadline.
            NexusError subclasses: Application-level errors from server.
        """
        payload = encode_rpc_message(params or {})
        effective_token = auth_token if auth_token is not None else self._auth_token
        request = vfs_pb2.CallRequest(
            method=method,
            payload=payload,
            auth_token=effective_token,
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
        # Unwrap server envelope: gRPC servicer wraps as {"result": actual_result}
        if isinstance(result, dict) and "result" in result:
            result = result["result"]
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
    def read_file(
        self, path: str, *, content_id: str = "", read_timeout: float | None = None
    ) -> bytes:
        """Read file content via typed Read RPC — no JSON/base64 overhead.

        Args:
            path: Virtual file path (used for routing on server).
            content_id: Opaque content identifier. When set, server reads
                content directly from the backend (no metastore lookup).
            read_timeout: Optional per-call timeout override.

        Returns:
            Raw file content as bytes.
        """
        request = vfs_pb2.ReadRequest(path=path, auth_token=self._auth_token, content_id=content_id)
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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
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


class RPCTransportPool:
    """Pool of RPCTransport instances — one per peer address.

    Thread-safe. Replaces PeerChannelPool with a higher-level abstraction
    that includes retry, auth, and typed RPC methods.

    TLS config is deferred: set via ``set_tls_config()`` after federation
    bootstrap (not available at NexusFS init time).
    """

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._transports: dict[str, RPCTransport] = {}
        self._tls_config: ZoneTlsConfig | None = None
        self._timeout = timeout
        self._lock = __import__("threading").Lock()

    def get(self, address: str) -> RPCTransport:
        """Get or create a persistent RPCTransport to *address*."""
        transport = self._transports.get(address)
        if transport is not None:
            return transport
        with self._lock:
            transport = self._transports.get(address)
            if transport is not None:
                return transport
            transport = RPCTransport(address, timeout=self._timeout, tls_config=self._tls_config)
            self._transports[address] = transport
            logger.debug("RPCTransportPool: created transport to %s", address)
            return transport

    def set_tls_config(self, config: "ZoneTlsConfig") -> None:
        """Set TLS config for future transports. Existing transports are NOT replaced."""
        self._tls_config = config

    def close_all(self) -> None:
        """Close all pooled transports."""
        with self._lock:
            for addr, t in self._transports.items():
                try:
                    t.close()
                except Exception:
                    logger.debug("RPCTransportPool: error closing transport to %s", addr)
            self._transports.clear()
        logger.debug("RPCTransportPool: all transports closed")
