"""FederationIPCResolver — PRE-DISPATCH resolver for remote DT_PIPE/DT_STREAM (#1625, #1665).

Registered as a VFSPathResolver in KernelDispatch.  Each ``try_*`` method
looks up metadata once, decides local vs remote, and either handles the
operation (remote → returns result) or declines (local/unknown → returns
``None``).

Zero kernel coupling: the kernel sees a standard VFSPathResolver.
Federation topology, gRPC channels, and IPC proxying are
entirely encapsulated here.

Design reference:
    - FederationContentResolver: same pattern for CAS content
    - BackendAddress: contracts/backend_address.py
    - KernelDispatch: core/kernel_dispatch.py (PRE-DISPATCH phase)
"""

import base64
import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.backend_address import BackendAddress
from nexus.contracts.exceptions import NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC
    from nexus.security.tls.config import ZoneTlsConfig

logger = logging.getLogger(__name__)


class FederationIPCResolver:
    """VFSPathResolver for remote DT_PIPE and DT_STREAM federation.

    Handles read/write/delete for pipes and streams hosted on remote nodes.
    Local pipes/streams return None (not handled) and fall through
    to the kernel's PipeManager/StreamManager.

    Implements the single-call ``try_*`` protocol (#1665):
    each method looks up metadata, decides local vs remote, and either
    handles the operation (remote → returns result) or declines
    (local/unknown → returns ``None``).

    Args:
        metastore: MetastoreABC for metadata lookup.
        self_address: This node's advertise address (e.g., "10.0.0.5:50051").
        tls_config: Optional ZoneTlsConfig for mTLS peer channels.
        timeout: RPC timeout in seconds.
    """

    name = "federation-ipc"

    def __init__(
        self,
        metastore: "MetastoreABC",
        self_address: str | None,
        tls_config: "ZoneTlsConfig | None" = None,
        timeout: float = 30.0,
    ) -> None:
        self._metastore = metastore
        self._self_address = self_address
        self._tls_config = tls_config
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal: metadata check (shared by all try_* methods)
    # ------------------------------------------------------------------

    def _resolve_remote_ipc(self, path: str) -> tuple[Any, str] | None:
        """Check if path is a remote IPC entry.

        Returns (metadata, origin_address) for remote, None for local/non-IPC.
        """
        meta = self._metastore.get(path)
        if meta is None or not meta.backend_name:
            return None

        if not (meta.is_pipe or meta.is_stream):
            return None

        addr = BackendAddress.parse(meta.backend_name)
        if not addr.has_origin:
            return None  # legacy "pipe"/"stream" without origin → local

        if self._self_address in addr.origins:
            return None  # origin is self → local

        return meta, addr.origins[0]

    # ------------------------------------------------------------------
    # VFSPathResolver single-call try_* protocol (#1665)
    # ------------------------------------------------------------------

    def try_read(
        self,
        path: str,
        *,
        return_metadata: bool = False,
        context: Any = None,
    ) -> bytes | dict[str, Any] | None:
        """Single-call resolve: metadata lookup + local/remote decision for read.

        Returns:
            bytes or dict — handled: content fetched from remote peer.
            None           — not handled: local IPC or non-IPC path.
        """
        _ = (return_metadata, context)
        resolved = self._resolve_remote_ipc(path)
        if resolved is None:
            return None
        meta, origin = resolved
        return self._read_remote(origin, path)

    def try_write(self, path: str, content: bytes) -> dict[str, Any] | None:
        """Single-call resolve: metadata lookup + local/remote decision for write.

        Returns:
            dict — handled: written to remote peer.
            None — not handled: local IPC or non-IPC path.
        """
        resolved = self._resolve_remote_ipc(path)
        if resolved is None:
            return None
        meta, origin = resolved
        result = self._write_remote(origin, path, content)
        if meta.is_stream:
            return {"offset": result}
        return {}

    def try_delete(
        self,
        path: str,
        *,
        context: Any = None,
    ) -> dict[str, Any] | None:
        """Single-call resolve: metadata lookup + local/remote decision for delete.

        Returns:
            dict — handled: remote peer destroyed the IPC entry.
            None — not handled: local IPC or non-IPC path.
        """
        _ = context
        resolved = self._resolve_remote_ipc(path)
        if resolved is None:
            return None
        _meta, origin = resolved
        self._delete_remote(origin, path)
        return {}

    # ------------------------------------------------------------------
    # gRPC remote operations
    # ------------------------------------------------------------------

    def _read_remote(self, origin: str, path: str) -> bytes:
        """Read from a remote pipe/stream via gRPC Call RPC (method=sys_read)."""
        import grpc

        from nexus.grpc.channel_factory import build_peer_channel
        from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

        params: dict[str, Any] = {"path": path}

        channel = build_peer_channel(origin, self._tls_config)
        try:
            from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            request = vfs_pb2.CallRequest(
                method="sys_read",
                payload=encode_rpc_message(params),
            )
            response = stub.Call(request, timeout=self._timeout)
            if response.is_error:
                payload = decode_rpc_message(response.payload) if response.payload else {}
                msg = payload.get("message", "Remote IPC read failed")
                raise NexusFileNotFoundError(path, msg)
            result = decode_rpc_message(response.payload)
            content = result.get("result", b"")
            if isinstance(content, str):
                content = base64.b64decode(content)
            return bytes(content)
        except grpc.RpcError as exc:
            raise NexusFileNotFoundError(
                path, f"Remote IPC read to {origin} failed: {exc}"
            ) from exc
        finally:
            channel.close()

    def _write_remote(self, origin: str, path: str, data: bytes) -> int:
        """Write to a remote pipe/stream via gRPC Call RPC (method=sys_write)."""
        import grpc

        from nexus.grpc.channel_factory import build_peer_channel
        from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

        params = {"path": path, "buf": base64.b64encode(data).decode("ascii")}

        channel = build_peer_channel(origin, self._tls_config)
        try:
            from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            request = vfs_pb2.CallRequest(
                method="sys_write",
                payload=encode_rpc_message(params),
            )
            response = stub.Call(request, timeout=self._timeout)
            if response.is_error:
                payload = decode_rpc_message(response.payload) if response.payload else {}
                msg = payload.get("message", "Remote IPC write failed")
                raise NexusFileNotFoundError(path, msg)
            result = decode_rpc_message(response.payload)
            return int(result.get("result", len(data)))
        except grpc.RpcError as exc:
            raise NexusFileNotFoundError(
                path, f"Remote IPC write to {origin} failed: {exc}"
            ) from exc
        finally:
            channel.close()

    def _delete_remote(self, origin: str, path: str) -> None:
        """Delete a remote pipe/stream via gRPC Delete RPC."""
        import grpc

        from nexus.grpc.channel_factory import build_peer_channel

        channel = build_peer_channel(origin, self._tls_config)
        try:
            from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            request = vfs_pb2.DeleteRequest(path=path, auth_token="")
            response = stub.Delete(request, timeout=self._timeout)
            if response.is_error:
                logger.warning("Remote IPC destroy on %s failed for %s", origin, path)
        except grpc.RpcError as exc:
            logger.warning("Remote IPC destroy to %s failed: %s", origin, exc)
            raise NexusFileNotFoundError(
                path, f"Remote IPC destroy to {origin} failed: {exc}"
            ) from exc
        finally:
            channel.close()
