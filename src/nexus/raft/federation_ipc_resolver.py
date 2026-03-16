"""FederationIPCResolver — PRE-DISPATCH resolver for remote DT_PIPE/DT_STREAM (#1625).

Registered as a VFSPathResolver in KernelDispatch.  On every read/write/delete,
``matches()`` looks up metadata and checks:

    1. Is this a DT_PIPE or DT_STREAM inode? (entry_type check)
    2. Is the pipe/stream hosted on a remote node? (locality check via backend_name)

If both are true, the resolver handles the operation entirely via gRPC Call/Delete
RPCs to the origin peer.  Local pipes/streams return None from matches() and fall
through to the kernel's normal IPC dispatch.

This extracts ~200 lines of federation remote proxy from NexusFS (kernel) to the
service layer, per federation-memo.md §6.6: "Federation is optional DI subsystem,
NOT kernel."

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
    Local pipes/streams are not matched (returns None) and fall through
    to the kernel's PipeManager/StreamManager.

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
    # VFSPathResolver protocol
    # ------------------------------------------------------------------

    def matches(self, path: str) -> Any:
        """Check if path refers to a remote DT_PIPE or DT_STREAM.

        Returns metadata (truthy) for remote IPC, None otherwise.
        The metadata is passed as ``match_ctx`` to read/write/delete.
        """
        meta = self._metastore.get(path)
        if meta is None or not meta.backend_name:
            return None

        # Only handle DT_PIPE (entry_type=3) and DT_STREAM (entry_type=4)
        if not (meta.is_pipe or meta.is_stream):
            return None

        addr = BackendAddress.parse(meta.backend_name)
        if not addr.has_origin:
            return None  # legacy "pipe"/"stream" without origin → local

        if addr.origin == self._self_address:
            return None  # origin is self → local

        return meta  # remote IPC — resolver handles

    def read(
        self,
        path: str,
        *,
        match_ctx: Any = None,
        return_metadata: bool = False,
        context: Any = None,
    ) -> bytes | dict[str, Any]:
        """Read from remote DT_PIPE/DT_STREAM via gRPC Call RPC."""
        _ = (return_metadata, context)  # Protocol-required; not used for IPC federation
        meta = match_ctx
        addr = BackendAddress.parse(meta.backend_name)
        assert addr.origin is not None

        return self._read_remote(addr.origin, path)

    def write(self, path: str, content: bytes, *, match_ctx: Any = None) -> dict[str, Any]:
        """Write to remote DT_PIPE/DT_STREAM via gRPC Call RPC."""
        meta = match_ctx
        addr = BackendAddress.parse(meta.backend_name)
        assert addr.origin is not None

        result = self._write_remote(addr.origin, path, content)
        # For streams, result may contain offset info
        if meta.is_stream:
            return {"offset": result}
        return {}

    def delete(self, path: str, *, match_ctx: Any = None, context: Any = None) -> None:
        """Delete remote DT_PIPE/DT_STREAM via gRPC Delete RPC."""
        _ = context  # Protocol-required; not used for IPC federation
        meta = match_ctx
        addr = BackendAddress.parse(meta.backend_name)
        assert addr.origin is not None

        self._delete_remote(addr.origin, path)

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
