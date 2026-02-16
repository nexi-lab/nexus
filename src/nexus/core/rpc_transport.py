"""RPC transport layer for Nexus inter-process communication.

This is a low-level utility, not a user-facing API. Used internally by:
- RemoteNexusFS (client-server connections)
- NexusFilesystem (P2P federation forwarding)
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

import httpx

# Lazy imports to avoid circular dependency:
# rpc_transport -> server.protocol -> server.__init__ -> rpc_server -> nexus_fs -> nexus_fs_federation -> rpc_transport
# Protocol types are imported inside methods that use them.
if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class TransportError(Exception):
    """Transport-level error (connection, timeout, HTTP)."""

    pass


class RPCError(Exception):
    """RPC-level error (server returned error response)."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"RPC error {code}: {message}")


class NexusRPCTransport:
    """Low-level RPC transport for Nexus communication.

    Handles:
    - HTTP/2 connection with pooling
    - Request/response serialization (JSON/msgpack)
    - Error handling
    - Configurable authentication

    This is NOT a NexusFilesystem implementation - it's a transport utility.

    Example:
        >>> transport = NexusRPCTransport("http://box-b:2026", auth_token="...")
        >>> result = transport.call("read", {"path": "/workspace/file.txt"})
    """

    def __init__(
        self,
        endpoint: str,
        auth_token: str | None = None,
        timeout: float = 30.0,
        connect_timeout: float = 5.0,
        pool_connections: int = 10,
        pool_maxsize: int = 10,
    ):
        """Initialize transport.

        Args:
            endpoint: Target Nexus server URL (e.g., "http://192.168.1.2:2026")
            auth_token: Authentication token (API key or federation token)
            timeout: Read/write timeout in seconds
            connect_timeout: Connection timeout in seconds
            pool_connections: Number of connection pool connections
            pool_maxsize: Maximum connection pool size
        """
        self.endpoint = endpoint.rstrip("/")
        self._auth_token = auth_token
        self._timeout = timeout
        self._connect_timeout = connect_timeout

        headers: dict[str, str] = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        limits = httpx.Limits(
            max_connections=pool_maxsize,
            max_keepalive_connections=pool_connections,
        )

        timeout_config = httpx.Timeout(
            connect=connect_timeout,
            read=timeout,
            write=timeout,
            pool=timeout,
        )

        self._client = httpx.Client(
            limits=limits,
            timeout=timeout_config,
            headers=headers,
            http2=True,
            trust_env=False,  # Bypass system proxy settings
        )

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Make RPC call to target server.

        Args:
            method: RPC method name (e.g., "read", "write", "list")
            params: Method parameters
            timeout: Override default timeout for this call

        Returns:
            RPC response result

        Raises:
            TransportError: Connection or timeout error
            RPCError: Server returned an error response
        """
        from nexus.core.rpc_protocol import (
            RPCErrorCode,
            RPCRequest,
            RPCResponse,
            decode_rpc_message,
            encode_rpc_message,
        )

        request = RPCRequest(
            method=method,
            params=params or {},
            id=str(uuid.uuid4()),
        )

        url = f"{self.endpoint}/api/nfs/{method}"
        body = encode_rpc_message(request.to_dict())

        # Build request timeout if overridden
        request_timeout: httpx.Timeout | None = None
        if timeout is not None:
            request_timeout = httpx.Timeout(
                connect=self._connect_timeout,
                read=timeout,
                write=timeout,
                pool=timeout,
            )

        try:
            response = self._client.post(
                url,
                content=body,
                headers={"Content-Type": "application/json"},
                timeout=request_timeout,
            )

            # Check HTTP status
            if response.status_code != 200:
                raise TransportError(
                    f"HTTP error {response.status_code} from {self.endpoint}: {response.text}"
                )

        except httpx.ConnectError as e:
            raise TransportError(f"Connection failed to {self.endpoint}: {e}") from e
        except httpx.TimeoutException as e:
            raise TransportError(f"Request timeout to {self.endpoint}: {e}") from e
        except httpx.HTTPError as e:
            raise TransportError(f"HTTP error from {self.endpoint}: {e}") from e

        # Decode response
        response_dict = decode_rpc_message(response.content)
        rpc_response = RPCResponse(
            jsonrpc=response_dict.get("jsonrpc", "2.0"),
            id=response_dict.get("id"),
            result=response_dict.get("result"),
            error=response_dict.get("error"),
        )

        # Check for RPC error
        if rpc_response.error:
            error = rpc_response.error
            raise RPCError(
                code=error.get("code", RPCErrorCode.INTERNAL_ERROR.value),
                message=error.get("message", "Unknown error"),
                data=error.get("data"),
            )

        return rpc_response.result

    def ping(self, timeout: float = 5.0) -> bool:
        """Health check - verify target is reachable.

        Returns:
            True if target responded, False otherwise
        """
        try:
            result = self.call("ping", {}, timeout=timeout)
            return bool(result.get("status") == "ok")
        except Exception:
            return False

    def close(self) -> None:
        """Close the HTTP client and release resources."""
        self._client.close()

    def __enter__(self) -> NexusRPCTransport:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"NexusRPCTransport(endpoint={self.endpoint!r})"
