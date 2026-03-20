"""PeerBlobClient — CAS-level peer-to-peer blob fetch (#1744).

Driver-to-driver protocol: reads CAS blobs by content hash from remote
peers via gRPC ReadBlob RPC.  Bypasses VFS path routing entirely —
this is backend transport, not a syscall.

Used by:
    - FederationContentResolver: CDC-aware chunk assembly
    - ContentReplicationService: hash-based content pull

Auth/encryption: same mTLS as all peer gRPC (zone join certificates).

Design reference:
    - docs/architecture/federation-memo.md §Content Read Path
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from nexus.contracts.exceptions import NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.security.tls.config import ZoneTlsConfig

logger = logging.getLogger(__name__)

_DEFAULT_WORKERS = 8


class PeerBlobClient:
    """Fetch CAS blobs from remote peers by content hash.

    Args:
        tls_config: Optional ZoneTlsConfig for mTLS peer channels.
        timeout: ReadBlob RPC timeout in seconds.
        workers: Max parallel ReadBlob RPCs for chunk fetching.
    """

    __slots__ = ("_tls_config", "_timeout", "_workers")

    def __init__(
        self,
        tls_config: ZoneTlsConfig | None = None,
        timeout: float = 30.0,
        workers: int = _DEFAULT_WORKERS,
    ) -> None:
        self._tls_config = tls_config
        self._timeout = timeout
        self._workers = workers

    def fetch_blob(self, address: str, content_hash: str) -> bytes:
        """Fetch a single CAS blob by content hash from a remote peer.

        Returns raw bytes (chunk content, manifest JSON, or single-blob file).
        Raises NexusFileNotFoundError if the peer doesn't have the blob.
        """
        import grpc

        from nexus.grpc.channel_factory import build_peer_channel
        from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

        channel = build_peer_channel(address, self._tls_config)
        try:
            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            request = vfs_pb2.ReadBlobRequest(
                content_hash=content_hash,
                auth_token="",
            )
            response = stub.ReadBlob(request, timeout=self._timeout)
            if response.is_error:
                raise NexusFileNotFoundError(
                    content_hash,
                    f"Remote peer {address} returned error for blob {content_hash[:16]}",
                )
            return bytes(response.content)
        except grpc.RpcError as exc:
            raise NexusFileNotFoundError(
                content_hash,
                f"Remote peer {address} unreachable: {exc}",
            ) from exc
        finally:
            channel.close()

    def fetch_blobs(
        self,
        address: str,
        content_hashes: list[str],
    ) -> dict[str, bytes]:
        """Fetch multiple CAS blobs in parallel from a single peer.

        Returns dict mapping content_hash → bytes for successfully fetched blobs.
        Raises on first failure (fail-fast).
        """
        if not content_hashes:
            return {}

        results: dict[str, bytes] = {}
        with ThreadPoolExecutor(max_workers=min(self._workers, len(content_hashes))) as executor:
            futures = {executor.submit(self.fetch_blob, address, h): h for h in content_hashes}
            for future in as_completed(futures):
                h = futures[future]
                results[h] = future.result()  # raises on error

        return results
