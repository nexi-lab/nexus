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

    # ------------------------------------------------------------------
    # Scatter-gather: fan-out to multiple origins (#1744 Phase 2)
    # ------------------------------------------------------------------

    def fetch_blob_scatter(self, origins: list[str], content_hash: str) -> bytes:
        """Fetch a single blob from the first origin that has it.

        Sends ReadBlob to all origins in parallel.  Returns data from the
        first successful response; remaining futures are cancelled/ignored.

        Raises NexusFileNotFoundError if no origin has the blob.
        """
        if len(origins) == 1:
            return self.fetch_blob(origins[0], content_hash)

        with ThreadPoolExecutor(max_workers=min(self._workers, len(origins))) as executor:
            futures = {
                executor.submit(self.fetch_blob, origin, content_hash): origin for origin in origins
            }
            last_err: Exception | None = None
            for future in as_completed(futures):
                try:
                    data = future.result()
                    # First success — cancel remaining futures
                    for f in futures:
                        f.cancel()
                    return data
                except NexusFileNotFoundError as exc:
                    last_err = exc
                    continue

        raise NexusFileNotFoundError(
            content_hash,
            f"Blob {content_hash[:16]} not found on any origin",
        ) from last_err

    def fetch_blobs_scatter(
        self,
        origins: list[str],
        content_hashes: list[str],
    ) -> dict[str, bytes]:
        """Fetch multiple blobs, each scattered across all origins.

        For each chunk hash, tries all origins in parallel — first responder
        wins.  CAS identity guarantees same hash = same content regardless
        of which origin responds.

        Returns dict mapping content_hash → bytes.
        Raises NexusFileNotFoundError if any chunk is missing from all origins.
        """
        if not content_hashes:
            return {}

        if len(origins) == 1:
            return self.fetch_blobs(origins[0], content_hashes)

        results: dict[str, bytes] = {}
        with ThreadPoolExecutor(max_workers=min(self._workers, len(content_hashes))) as executor:
            futures = {
                executor.submit(self.fetch_blob_scatter, origins, h): h for h in content_hashes
            }
            for future in as_completed(futures):
                h = futures[future]
                results[h] = future.result()  # raises on error

        return results
