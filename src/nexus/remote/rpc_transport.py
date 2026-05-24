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
import re as _re
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

# Heuristic for credential-shaped key names (Issue #4083 rounds 2-3).
# Recursive redactor walks every dict / list at any depth and replaces
# values whose key matches this regex. Round-3 reviewer finding: a
# top-level allowlist (mount_overrides, auth_token, ...) missed nested
# headers/env, sandbox_api_key, nexus_api_key, and any future RPC param
# shape. Heuristic-shaped redaction is the safer default: it errs on the
# side of over-redacting in DEBUG logs (operators see structure, not
# values) rather than leaking a freshly-added secret-shaped param.
_SENSITIVE_KEY_RE = _re.compile(
    r"(?i)(secret|token|password|passwd|credential|api[_-]?key|"
    r"access[_-]?key|session[_-]?token|authorization|auth[_-]?key|"
    r"private[_-]?key)"
)

# Top-level RPC params known to carry secret material as VALUES even when
# their KEY name doesn't match the heuristic. Used in addition to the
# recursive scan.
_SENSITIVE_TOP_LEVEL: frozenset[str] = frozenset({"mount_overrides", "injections", "credentials"})


def _redact_params(params: Any) -> Any:
    """Return a deep redacted copy of params with credential-shaped keys
    replaced by ``"***"``.

    Walks dicts and lists recursively. At every level, a key whose name
    matches ``_SENSITIVE_KEY_RE`` has its value replaced (preserving
    structure: a dict value becomes a dict of "***", a list becomes a
    list of "***"). At the top level, ``mount_overrides`` /
    ``injections`` / ``credentials`` are also redacted regardless of
    key-name shape.

    The original ``params`` is never mutated.
    """
    return _redact(params, top_level=True)


def _redact(value: Any, *, top_level: bool) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_str = k if isinstance(k, str) else ""
            sensitive = bool(_SENSITIVE_KEY_RE.search(key_str)) or (
                top_level and key_str in _SENSITIVE_TOP_LEVEL
            )
            if sensitive and v is not None:
                if isinstance(v, dict):
                    out[k] = _redact_dict_to_stars(v)
                elif isinstance(v, list):
                    out[k] = ["***" for _ in v]
                else:
                    out[k] = "***"
            else:
                out[k] = _redact(v, top_level=False)
        return out
    if isinstance(value, list):
        return [_redact(item, top_level=False) for item in value]
    return value


def _redact_dict_to_stars(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively replace every leaf value with ``"***"`` while preserving
    keys at every level so the redacted log still shows the call shape."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _redact_dict_to_stars(v)
        elif isinstance(v, list):
            out[k] = ["***" for _ in v]
        else:
            out[k] = "***"
    return out


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
        # Strip protocol prefix if present — callers (e.g. FederationHandshake)
        # may pass the full URL "grpc://host:port".  The loopback check below
        # operates on the host portion only and would otherwise see
        # "grpc://localhost" (not "localhost") and reject the connection.
        #
        # Track whether the caller asked for a TLS scheme so we can fail
        # closed if no TLS material is provided — preventing silent
        # plaintext downgrade for grpcs:// URLs.
        _scheme_requires_tls = False
        if server_address.startswith("grpc://"):
            server_address = server_address[len("grpc://") :]
        elif server_address.startswith("grpcs://"):
            server_address = server_address[len("grpcs://") :]
            _scheme_requires_tls = True
        if _scheme_requires_tls and tls_config is None:
            raise ValueError(
                f"grpcs:// scheme requires TLS configuration (got '{server_address}' "
                "with no tls_config). Pass a ZoneTlsConfig or use grpc:// for plaintext."
            )
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

        logger.debug("RPCTransport.call_rpc: %s params=%s", method, _redact_params(params))

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
        """Read file content via typed Read RPC — bytes-only convenience.

        Use ``read()`` when the caller needs entry_type / stream_next_offset
        (e.g. pipe / stream / range reads). This helper just unwraps content.
        """
        return self.read(path, content_id=content_id, read_timeout=read_timeout).content

    def read(
        self,
        path: str,
        *,
        content_id: str = "",
        timeout_ms: int = 0,
        offset: int = 0,
        read_timeout: float | None = None,
    ) -> Any:
        """Typed Read RPC — returns the full ReadResponse message.

        ``timeout_ms=0`` keeps file-read semantics; non-zero blocks pipe /
        stream reads up to that budget. ``offset`` is honored for stream
        + range reads. Caller reads ``response.entry_type`` /
        ``response.stream_next_offset`` for pipe/stream classification.
        """
        request = vfs_pb2.ReadRequest(
            path=path,
            auth_token=self._auth_token,
            content_id=content_id,
            timeout_ms=int(timeout_ms),
            offset=int(offset),
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Read(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Read")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response

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
        content_id: str | None = None,
        read_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Write file content via typed Write RPC — no JSON/base64 overhead.

        Returns:
            Dict with ``content_id``, ``size``, and ``gen``.
        """
        request = vfs_pb2.WriteRequest(
            path=path,
            content=content,
            auth_token=self._auth_token,
            content_id=content_id or "",
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Write(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Write")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return {"content_id": response.content_id, "size": response.size, "gen": response.gen}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def delete(
        self,
        path: str,
        recursive: bool = False,
        read_timeout: float | None = None,
    ) -> Any:
        """Delete a file or directory via the typed Delete RPC.

        Returns the full ``DeleteResponse`` — ``success`` plus the
        ``entry_type`` / ``path`` / ``content_id`` / ``size`` fields the
        former ``sys_unlink`` Call carried for audit / metrics callers.
        Raises on auth or transport failure.
        """
        request = vfs_pb2.DeleteRequest(path=path, auth_token=self._auth_token, recursive=recursive)
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Delete(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Delete")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response

    def delete_file(
        self,
        path: str,
        recursive: bool = False,
        read_timeout: float | None = None,
    ) -> bool:
        """Back-compat thin wrapper over ``delete()`` — returns the
        ``success`` bool that the legacy callers expect."""
        return bool(self.delete(path, recursive, read_timeout).success)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = True,
        read_timeout: float | None = None,
    ) -> Any:
        """Mkdir via the typed Mkdir RPC. Returns the MkdirResponse."""
        request = vfs_pb2.MkdirRequest(
            path=path, auth_token=self._auth_token, parents=parents, exist_ok=exist_ok
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Mkdir(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Mkdir")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def batch_read(
        self,
        items: list[tuple[str, int, int | None]],
        read_timeout: float | None = None,
    ) -> list[Any]:
        """Vectored batch read via the typed BatchRead RPC — one round-trip.

        ``items`` is a list of ``(path, offset, length)`` tuples; ``length``
        is omitted from the request when ``None`` (read to EOF from offset).
        Returns the ``BatchReadItemResponse`` messages in input order —
        per-item failures are reported in-band (``is_error`` /
        ``error_payload``); only transport-level failures raise.
        """
        req_items = []
        for path, offset, length in items:
            item = vfs_pb2.BatchReadItemRequest(path=path, offset=offset)
            if length is not None:
                item.length = length
            req_items.append(item)
        request = vfs_pb2.BatchReadRequest(auth_token=self._auth_token, items=req_items)
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.BatchRead(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "BatchRead")
        return list(response.results)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def batch_write(
        self,
        files: list[tuple[str, bytes]],
        read_timeout: float | None = None,
    ) -> list[Any]:
        """Vectored batch write via the typed BatchWrite RPC — one round-trip.

        Native bytes — no base64/JSON tax. The kernel attempts every item
        (create-or-overwrite, per-item isolated); this raises the first
        per-item failure, preserving the all-or-nothing contract of the
        former generic ``write_batch`` Call. Returns the per-item success
        responses in input order.
        """
        request = vfs_pb2.BatchWriteRequest(
            auth_token=self._auth_token,
            items=[
                vfs_pb2.BatchWriteItemRequest(path=path, content=content) for path, content in files
            ],
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.BatchWrite(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "BatchWrite")
        for item in response.results:
            if item.is_error:
                self._handle_typed_error(item.error_payload)
        return list(response.results)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def readdir(self, path: str, zone_id: str = "", read_timeout: float | None = None) -> list[Any]:
        """List directory entries via the typed Readdir RPC.

        Returns the ``ReaddirEntry`` list from the response (raw protobuf
        objects with ``.name`` / ``.entry_type``). Raises on auth or
        transport failure; the handler treats ``is_admin`` as a
        ctx-derived field, so it's not part of the request.
        """
        request = vfs_pb2.ReaddirRequest(path=path, auth_token=self._auth_token, zone_id=zone_id)
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Readdir(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Readdir")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return list(response.entries)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def batch_stat(
        self,
        paths: list[str],
        zone_id: str = "",
        read_timeout: float | None = None,
    ) -> list[Any]:
        """Vectored stat via the typed BatchStat RPC.

        Returns the ``BatchStatItem`` list in input order (same length as
        ``paths``). Each item exposes the stat fields plus a ``found``
        flag — per-path not-found is in-band, not an error. Raises on
        auth or transport failure.
        """
        request = vfs_pb2.BatchStatRequest(
            auth_token=self._auth_token, zone_id=zone_id, paths=list(paths)
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.BatchStat(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "BatchStat")
        return list(response.results)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def stat(self, path: str, zone_id: str = "", read_timeout: float | None = None) -> Any | None:
        """Stat a path via the typed Stat RPC.

        Returns the ``StatResponse`` message, or ``None`` when the path
        does not exist (``found == false`` — not an error). Raises on
        auth / transport failure.
        """
        request = vfs_pb2.StatRequest(path=path, auth_token=self._auth_token, zone_id=zone_id)
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Stat(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Stat")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response if response.found else None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def setattr(self, path: str, read_timeout: float | None = None, **kwargs: Any) -> Any:
        """Set attributes via the typed Setattr RPC.

        Accepts the JSON-Call kwargs (``entry_type``, ``zone_id``,
        ``mime_type``, ``content_id``, ``modified_at_ms``,
        ``created_at_ms``, ``size``, ``version``, ``backend_name``,
        ``io_profile``, ``is_external``, ``capacity``); unknown kwargs
        are silently dropped, matching the Call path's pick-known-keys
        behaviour. Returns the ``SetattrResponse`` message; raises on
        auth or transport failure.
        """
        request = vfs_pb2.SetattrRequest(
            path=path,
            auth_token=self._auth_token,
            entry_type=int(kwargs.get("entry_type", 0) or 0),
            zone_id=kwargs.get("zone_id", "") or "",
            backend_name=kwargs.get("backend_name", "") or "",
            io_profile=kwargs.get("io_profile", "") or "",
            is_external=bool(kwargs.get("is_external", False)),
            capacity=int(kwargs.get("capacity", 0) or 0),
        )
        # Optional fields — only set when the caller actually supplied a
        # non-None value so `HasField` round-trips correctly.
        for opt_field in (
            "mime_type",
            "content_id",
            "modified_at_ms",
            "created_at_ms",
            "size",
            "version",
        ):
            val = kwargs.get(opt_field)
            if val is not None:
                setattr(request, opt_field, val)

        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Setattr(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Setattr")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def rename(self, path: str, new_path: str, read_timeout: float | None = None) -> Any:
        """Rename via the typed Rename RPC. Returns the RenameResponse."""
        request = vfs_pb2.RenameRequest(path=path, new_path=new_path, auth_token=self._auth_token)
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Rename(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Rename")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def copy(self, src: str, dst: str, read_timeout: float | None = None) -> Any:
        """Server-side copy via the typed Copy RPC. Returns the CopyResponse."""
        request = vfs_pb2.CopyRequest(src=src, dst=dst, auth_token=self._auth_token)
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Copy(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Copy")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def lock(
        self,
        path: str,
        lock_id: str = "",
        timeout_ms: int = 5000,
        read_timeout: float | None = None,
    ) -> Any:
        """Acquire an advisory lock via the typed Lock RPC.

        Returns the ``LockResponse`` — ``acquired=false`` means contention,
        not a transport error. Raises on auth or transport failure.
        """
        request = vfs_pb2.LockRequest(
            path=path,
            auth_token=self._auth_token,
            lock_id=lock_id,
            timeout_ms=timeout_ms,
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Lock(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Lock")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def unlock(
        self,
        path: str,
        lock_id: str = "",
        force: bool = False,
        read_timeout: float | None = None,
    ) -> Any:
        """Release an advisory lock via the typed Unlock RPC."""
        request = vfs_pb2.UnlockRequest(
            path=path, auth_token=self._auth_token, lock_id=lock_id, force=force
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.Unlock(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Unlock")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def watch(
        self,
        path: str,
        timeout_ms: int = 30000,
        read_timeout: float | None = None,
    ) -> Any:
        """Block on a file-event match via the typed Watch RPC.

        Returns the ``WatchResponse`` — ``matched=false`` means the
        kernel timed out (no event), not a transport error. The RPC
        deadline is sized to the kernel timeout plus a small slack so
        callers with ``timeout_ms`` larger than the transport's default
        90 s aren't cut short.
        """
        request = vfs_pb2.WatchRequest(
            path=path, auth_token=self._auth_token, timeout_ms=timeout_ms
        )
        timeout = read_timeout if read_timeout is not None else max(timeout_ms / 1000.0 + 5.0, 5.0)
        try:
            response = self._stub.Watch(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "Watch")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def get_xattr(self, path: str, key: str, read_timeout: float | None = None) -> Any:
        """Get a single xattr via the typed GetXattr RPC.

        Returns the ``GetXattrResponse`` — ``found=false`` means the key
        is not set, not an error.
        """
        request = vfs_pb2.GetXattrRequest(path=path, key=key, auth_token=self._auth_token)
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.GetXattr(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "GetXattr")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def set_xattr(self, path: str, key: str, value: str, read_timeout: float | None = None) -> None:
        """Set an xattr via the typed SetXattr RPC."""
        request = vfs_pb2.SetXattrRequest(
            path=path, key=key, value=value, auth_token=self._auth_token
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.SetXattr(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "SetXattr")
        if response.is_error:
            self._handle_typed_error(response.error_payload)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def get_xattr_bulk(
        self, paths: list[str], key: str, read_timeout: float | None = None
    ) -> list[Any]:
        """Bulk get a single xattr via the typed GetXattrBulk RPC.

        Returns the ``GetXattrBulkItem`` list (positional, same length as
        ``paths``).
        """
        request = vfs_pb2.GetXattrBulkRequest(
            paths=list(paths), key=key, auth_token=self._auth_token
        )
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            response = self._stub.GetXattrBulk(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "GetXattrBulk")
        if response.is_error:
            self._handle_typed_error(response.error_payload)
        return list(response.items)

    # ── Typed IPC pipe / stream ops ────────────────────────────────────

    def _ipc_path_request(self, path: str) -> Any:
        return vfs_pb2.IpcPathRequest(path=path, auth_token=self._auth_token)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def close_pipe(self, path: str, read_timeout: float | None = None) -> None:
        """Close a pipe via the typed ClosePipe RPC."""
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            resp = self._stub.ClosePipe(self._ipc_path_request(path), timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "ClosePipe")
        if resp.is_error:
            self._handle_typed_error(resp.error_payload)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def has_pipe(self, path: str, read_timeout: float | None = None) -> bool:
        """Has-pipe query via the typed HasPipe RPC."""
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            resp = self._stub.HasPipe(self._ipc_path_request(path), timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "HasPipe")
        if resp.is_error:
            self._handle_typed_error(resp.error_payload)
        return bool(resp.present)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def close_all_pipes(self, read_timeout: float | None = None) -> None:
        """Close every pipe via the typed CloseAllPipes RPC."""
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            resp = self._stub.CloseAllPipes(
                vfs_pb2.IpcEmpty(auth_token=self._auth_token), timeout=timeout
            )
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "CloseAllPipes")
        if resp.is_error:
            self._handle_typed_error(resp.error_payload)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def close_stream(self, path: str, read_timeout: float | None = None) -> None:
        """Close a stream via the typed CloseStream RPC."""
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            resp = self._stub.CloseStream(self._ipc_path_request(path), timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "CloseStream")
        if resp.is_error:
            self._handle_typed_error(resp.error_payload)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def has_stream(self, path: str, read_timeout: float | None = None) -> bool:
        """Has-stream query via the typed HasStream RPC."""
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            resp = self._stub.HasStream(self._ipc_path_request(path), timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "HasStream")
        if resp.is_error:
            self._handle_typed_error(resp.error_payload)
        return bool(resp.present)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def stream_write_nowait(self, path: str, data: bytes, read_timeout: float | None = None) -> int:
        """Non-blocking stream write via the typed StreamWriteNowait RPC.

        Returns the offset where the data landed (native bytes — no base64).
        """
        request = vfs_pb2.StreamWriteRequest(path=path, data=data, auth_token=self._auth_token)
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            resp = self._stub.StreamWriteNowait(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "StreamWriteNowait")
        if resp.is_error:
            self._handle_typed_error(resp.error_payload)
        return int(resp.offset)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def stream_read_at(
        self,
        path: str,
        offset: int,
        blocking: bool = False,
        timeout_ms: int = 30000,
        read_timeout: float | None = None,
    ) -> Any:
        """Stream read via the typed StreamReadAt RPC.

        Returns the ``StreamReadAtResponse``. On non-blocking ``eof=true``
        means no data was available. Long blocking reads size the RPC
        deadline to ``timeout_ms + slack`` so the transport's default
        90 s cap doesn't cut them short.
        """
        request = vfs_pb2.StreamReadAtRequest(
            path=path,
            offset=offset,
            blocking=blocking,
            timeout_ms=timeout_ms,
            auth_token=self._auth_token,
        )
        if read_timeout is not None:
            timeout = read_timeout
        elif blocking:
            timeout = max(timeout_ms / 1000.0 + 5.0, 5.0)
        else:
            timeout = self._timeout
        try:
            resp = self._stub.StreamReadAt(request, timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "StreamReadAt")
        if resp.is_error:
            self._handle_typed_error(resp.error_payload)
        return resp

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((grpc.RpcError, RemoteConnectionError)),
        reraise=True,
    )
    def stream_collect_all(self, path: str, read_timeout: float | None = None) -> bytes:
        """Collect all bytes from a stream via the typed StreamCollectAll RPC."""
        timeout = read_timeout if read_timeout is not None else self._timeout
        try:
            resp = self._stub.StreamCollectAll(self._ipc_path_request(path), timeout=timeout)
        except grpc.RpcError as exc:
            self._raise_transport_error(exc, timeout, "StreamCollectAll")
        if resp.is_error:
            self._handle_typed_error(resp.error_payload)
        return bytes(resp.data)

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
