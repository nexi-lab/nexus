"""FederationContentResolver — PRE-DISPATCH resolver for remote content (#163).

Registered as a VFSPathResolver in KernelDispatch.  On every read,
looks up metadata once:

    - Remote origin → resolver handles the read (fetch via Read RPC).
    - Local origin  → resolver passes prefetched metadata back as hint
                      so the kernel skips its own metastore.get().

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

import grpc

from nexus.contracts.backend_address import BackendAddress
from nexus.contracts.exceptions import NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.metastore import MetastoreABC
    from nexus.security.tls.config import ZoneTlsConfig

logger = logging.getLogger(__name__)

_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms", 10_000),
    ("grpc.keepalive_timeout_ms", 5_000),
    ("grpc.keepalive_permit_without_calls", True),
    ("grpc.http2.max_pings_without_data", 0),
]


class FederationContentResolver:
    """VFSPathResolver that dispatches reads to remote content owners.

    Implements the ``try_read`` protocol (merged matches+read):
    a single call looks up metadata, decides local vs remote, and
    either returns content (handled) or metadata hint (not handled).

    Args:
        metastore: MetastoreABC for metadata lookup.
        backend: Local backend for persisting replicated content.
        self_address: This node's advertise address (e.g., "10.0.0.5:50051").
        tls_config: Optional ZoneTlsConfig for mTLS peer channels.
        timeout: Read RPC timeout in seconds.
    """

    name = "federation-content"

    def __init__(
        self,
        metastore: "MetastoreABC",
        backend: "Backend",
        self_address: str,
        tls_config: "ZoneTlsConfig | None" = None,
        timeout: float = 30.0,
    ) -> None:
        self._metastore = metastore
        self._backend = backend
        self._self_address = self_address
        self._tls_config = tls_config
        self._timeout = timeout

    def try_read(
        self,
        path: str,
        *,
        return_metadata: bool = False,
        **_kwargs: Any,
    ) -> tuple[bool, Any]:
        """Single-call resolve: metadata lookup + local/remote decision.

        Returns:
            (True, content_or_dict) — handled: content fetched from remote peer.
            (False, FileMetadata)   — not handled: local content, metadata hint.
            (False, None)           — not handled: no metadata found.
        """
        meta = self._metastore.get(path)
        if meta is None or not meta.backend_name:
            return False, None

        addr = BackendAddress.parse(meta.backend_name)
        if not addr.has_origin or addr.origin == self._self_address:
            # Local content — pass metadata hint back to kernel
            return False, meta

        # Remote content — fetch from origin peer
        assert addr.origin is not None  # guaranteed by has_origin check above
        logger.info(
            "Federation read: %s -> %s (etag=%s)",
            path,
            addr.origin,
            (meta.etag or "")[:12],
        )
        content = self._fetch_from_peer(addr.origin, path)

        # Progressive replication: persist to local CAS
        self._persist_locally(content)

        if return_metadata:
            return True, {
                "content": content,
                "etag": meta.etag,
                "version": meta.version,
                "modified_at": meta.modified_at,
                "size": len(content),
            }
        return True, content

    # === gRPC Remote Fetch ===

    def _fetch_from_peer(self, address: str, virtual_path: str) -> bytes:
        """Dispatch sync Read RPC to origin peer."""
        from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

        channel = self._build_channel(address)
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

    def _build_channel(self, address: str) -> grpc.Channel:
        """Build sync gRPC channel with optional mTLS."""
        if self._tls_config is not None:
            ca = self._tls_config.ca_cert_path.read_bytes()
            cert = self._tls_config.node_cert_path.read_bytes()
            key = self._tls_config.node_key_path.read_bytes()
            creds = grpc.ssl_channel_credentials(
                root_certificates=ca,
                private_key=key,
                certificate_chain=cert,
            )
            return grpc.secure_channel(address, creds, options=_CHANNEL_OPTIONS)
        return grpc.insecure_channel(address, options=_CHANNEL_OPTIONS)

    def _persist_locally(self, content: bytes) -> None:
        """Write fetched content to local CAS for future reads."""
        try:
            self._backend.write_content(content)
        except Exception as exc:
            logger.warning("Failed to persist replicated content: %s", exc)

    # === Legacy VFSPathResolver compat (matches/read/write/delete) ===
    # KernelDispatch prefers try_read when available; these are fallbacks.

    def matches(self, _path: str) -> bool:
        return False  # Read-only: writes/deletes pass through

    def read(
        self, path: str, *, return_metadata: bool = False, context: Any = None
    ) -> bytes | dict:
        raise NotImplementedError("Use try_read()")

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        raise NotImplementedError("FederationContentResolver is read-only")

    def delete(self, path: str, *, context: Any = None) -> None:
        raise NotImplementedError("FederationContentResolver is read-only")
