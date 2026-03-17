"""FederationContentResolver — PRE-DISPATCH resolver for remote content (#163, #1665).

Registered as a VFSPathResolver in KernelDispatch.  Each ``try_*`` method
looks up metadata once, decides local vs remote, and either handles the
operation (remote → returns result) or declines (local/unknown → returns
``None``).

Zero kernel coupling: the kernel sees a standard VFSPathResolver.
Federation topology, gRPC channels, and progressive replication are
entirely encapsulated here.

Design reference:
    - docs/architecture/federation-memo.md §Content Read Path
    - BackendAddress: contracts/backend_address.py
    - KernelDispatch: core/kernel_dispatch.py (PRE-DISPATCH phase)
    - RPCTransport.read_file: remote/rpc_transport.py (sync Read RPC pattern)
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.backend_address import BackendAddress
from nexus.contracts.exceptions import NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.core.metastore import MetastoreABC
    from nexus.security.tls.config import ZoneTlsConfig

logger = logging.getLogger(__name__)

# Files larger than this threshold use StreamRead instead of unary Read.
# StreamRead keeps ~1MB in memory at a time; unary Read buffers entire file.
_STREAMING_THRESHOLD = 1_048_576  # 1 MB


class FederationContentResolver:
    """VFSPathResolver that dispatches reads and deletes to remote content owners.

    Content writes are always local — the kernel writes CAS content to the
    local backend; metadata routing is handled transparently by
    FederatedMetadataProxy (DI), which enriches ``backend_name`` with the
    writer node's address so future reads can locate content.

    Implements the single-call ``try_*`` protocol (#1665):
    each method looks up metadata, decides local vs remote, and either
    handles the operation (remote → returns result) or declines
    (local/unknown → returns ``None``).

    Args:
        metastore: MetastoreABC for metadata lookup.
        self_address: This node's advertise address (e.g., "10.0.0.5:50051").
        tls_config: Optional ZoneTlsConfig for mTLS peer channels.
        timeout: Read RPC timeout in seconds.
    """

    name = "federation-content"

    def __init__(
        self,
        metastore: "MetastoreABC",
        self_address: str,
        tls_config: "ZoneTlsConfig | None" = None,
        timeout: float = 30.0,
    ) -> None:
        self._metastore = metastore
        self._self_address = self_address
        self._tls_config = tls_config
        self._timeout = timeout

    # ------------------------------------------------------------------
    # HotSwappable protocol (#1710) — enables coordinator.enlist()
    # ------------------------------------------------------------------

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(resolvers=(self,))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    # ------------------------------------------------------------------
    # VFSPathResolver single-call try_* protocol (#1665)
    # ------------------------------------------------------------------

    def try_read(
        self,
        path: str,
        *,
        return_metadata: bool = False,
        context: Any = None,
    ) -> bytes | dict | None:
        """Single-call resolve: metadata lookup + local/remote decision.

        Returns:
            bytes or dict — handled: content fetched from remote peer.
            None          — not handled: local content or no metadata.
        """
        _ = context  # unused, present for protocol conformance
        meta = self._metastore.get(path)
        if meta is None or not meta.backend_name:
            return None

        addr = BackendAddress.parse(meta.backend_name)
        if not addr.has_origin or addr.origin == self._self_address:
            return None  # local content — kernel handles

        # Remote content — fetch from origin peer
        assert addr.origin is not None  # guaranteed by has_origin check above
        file_size = meta.size or 0
        use_streaming = file_size > _STREAMING_THRESHOLD
        logger.info(
            "Federation read: %s -> %s (etag=%s, size=%d, streaming=%s)",
            path,
            addr.origin,
            (meta.etag or "")[:12],
            file_size,
            use_streaming,
        )

        if use_streaming:
            content = self._fetch_from_peer_streaming(addr.origin, path)
        else:
            content = self._fetch_from_peer(addr.origin, path)

        if return_metadata:
            return {
                "content": content,
                "etag": meta.etag,
                "version": meta.version,
                "modified_at": meta.modified_at,
                "size": len(content),
            }
        return content

    def try_write(self, _path: str, _content: bytes) -> dict[str, Any] | None:
        """Content writes are always local — return None to pass through."""
        return None

    def try_delete(
        self,
        path: str,
        *,
        context: Any = None,
    ) -> dict[str, Any] | None:
        """Single-call resolve: metadata lookup + local/remote decision for delete.

        Symmetric with ``try_read``. If content origin is remote, delegates
        the full ``sys_unlink`` to the origin peer via gRPC Delete RPC.

        Returns:
            dict — handled: remote peer deleted file.
            None — not handled: local content or no metadata.
        """
        _ = context  # unused, present for protocol conformance
        meta = self._metastore.get(path)
        if meta is None or not meta.backend_name:
            return None

        addr = BackendAddress.parse(meta.backend_name)
        if not addr.has_origin or addr.origin == self._self_address:
            return None  # local content — kernel handles

        # Remote content — delegate full sys_unlink to origin peer
        assert addr.origin is not None
        logger.info(
            "Federation delete: %s -> %s (etag=%s)",
            path,
            addr.origin,
            (meta.etag or "")[:12],
        )
        self._delete_on_peer(addr.origin, path)
        return {}

    # === gRPC Remote Operations ===

    def _delete_on_peer(self, address: str, virtual_path: str) -> None:
        """Dispatch sync Delete RPC to origin peer (full sys_unlink)."""
        import grpc

        from nexus.grpc.channel_factory import build_peer_channel
        from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

        channel = build_peer_channel(address, self._tls_config)
        try:
            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            request = vfs_pb2.DeleteRequest(path=virtual_path, auth_token="")
            response = stub.Delete(request, timeout=self._timeout)

            if response.is_error:
                logger.warning(
                    "Federation Delete RPC to %s returned error for %s",
                    address,
                    virtual_path,
                )
        except grpc.RpcError as exc:
            logger.warning("Federation Delete RPC to %s failed: %s", address, exc)
        finally:
            channel.close()

    def _fetch_from_peer(self, address: str, virtual_path: str) -> bytes:
        """Dispatch sync Read RPC to origin peer."""
        import grpc

        from nexus.grpc.channel_factory import build_peer_channel
        from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

        channel = build_peer_channel(address, self._tls_config)
        try:
            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            request = vfs_pb2.ReadRequest(path=virtual_path, auth_token="")
            response = stub.Read(request, timeout=self._timeout)

            if response.is_error:
                raise NexusFileNotFoundError(
                    virtual_path,
                    f"Remote peer {address} returned error",
                )
            return bytes(response.content)
        except grpc.RpcError as exc:
            logger.warning("Federation Read RPC to %s failed: %s", address, exc)
            raise NexusFileNotFoundError(
                virtual_path,
                f"Remote peer {address} unreachable: {exc}",
            ) from exc
        finally:
            channel.close()

    def _fetch_from_peer_streaming(self, address: str, virtual_path: str) -> bytes:
        """Fetch via StreamRead RPC — backend-agnostic streaming.

        The origin's StreamRead handler decides how to serve the file
        (CAS chunk-aware streaming, PAS read-and-chunk, etc).
        This method just collects the stream and returns assembled bytes.
        """
        import grpc

        from nexus.grpc.channel_factory import build_peer_channel
        from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

        channel = build_peer_channel(address, self._tls_config)
        try:
            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            request = vfs_pb2.StreamReadRequest(path=virtual_path, auth_token="")
            chunks: list[bytes] = []
            for chunk in stub.StreamRead(request, timeout=self._timeout):
                if chunk.is_error:
                    raise NexusFileNotFoundError(
                        virtual_path,
                        f"Remote peer {address} returned streaming error",
                    )
                chunks.append(bytes(chunk.data))
                if chunk.is_last:
                    break
            return b"".join(chunks)
        except grpc.RpcError as exc:
            logger.warning("Federation StreamRead to %s failed: %s", address, exc)
            raise NexusFileNotFoundError(
                virtual_path,
                f"Remote peer {address} unreachable: {exc}",
            ) from exc
        finally:
            channel.close()
