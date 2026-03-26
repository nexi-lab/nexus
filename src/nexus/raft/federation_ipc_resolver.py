"""FederationIPCResolver — PRE-DISPATCH resolver for remote DT_PIPE/DT_STREAM (#1625, #1665).

Registered as a VFSPathResolver in KernelDispatch.  Each ``try_*`` method
looks up metadata once, decides local vs remote, and either handles the
operation (remote → returns result) or declines (local/unknown → returns
``None``).

Implements the single-call ``try_*`` protocol (#1665):
each method looks up metadata, decides local vs remote, and either
handles the operation (remote → returns result) or declines
(local/unknown → returns ``None``).

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
    from nexus.contracts.protocols.service_hooks import HookSpec
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
    # Hook spec (duck-typed) (#1710) — enables coordinator.enlist()
    # ------------------------------------------------------------------

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(resolvers=(self,))

    # ------------------------------------------------------------------
    # VFSPathResolver single-call try_* protocol (#1665)
    # ------------------------------------------------------------------

    def _resolve_remote(self, path: str) -> tuple[Any, str] | None:
        """Shared metadata lookup + locality check.

        Returns (meta, origin_address) for remote IPC, None otherwise.
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

        if self._self_address in addr.origins:
            return None  # origin is self → local

        return meta, addr.origins[0]  # remote IPC — resolver handles

    def try_read(
        self,
        path: str,
        *,
        context: Any = None,
    ) -> bytes | None:
        """Read from remote DT_PIPE/DT_STREAM via gRPC Call RPC.

        Returns None if path is not a remote IPC inode (decline).
        """
        _ = context  # Protocol-required; not used for IPC
        resolved = self._resolve_remote(path)
        if resolved is None:
            return None
        _meta, origin = resolved
        return self._read_remote(origin, path)

    def try_write(self, path: str, content: bytes) -> dict[str, Any] | None:
        """Write to remote DT_PIPE/DT_STREAM via gRPC Call RPC.

        Returns None if path is not a remote IPC inode (decline).
        """
        resolved = self._resolve_remote(path)
        if resolved is None:
            return None
        meta, origin = resolved
        result = self._write_remote(origin, path, content)
        # For streams, result may contain offset info
        if meta.is_stream:
            return {"offset": result}
        return {}

    def try_delete(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        """Delete remote DT_PIPE/DT_STREAM via gRPC Delete RPC.

        Returns None if path is not a remote IPC inode (decline).
        """
        _ = context  # Protocol-required; not used for IPC
        resolved = self._resolve_remote(path)
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
