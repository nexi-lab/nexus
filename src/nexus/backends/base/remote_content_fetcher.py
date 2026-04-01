"""RemoteContentFetcher — addressing-agnostic protocol for remote content fetch.

DriverLifecycleCoordinator.resolve_backend() delegates to this protocol so it
never needs to know about CAS, CDC, manifests, or chunks.  Each addressing mode
provides its own implementation.

    RemoteContentFetcher (Protocol)
        └── CASRemoteContentFetcher  — CAS+CDC: manifest parse, local chunk
                                       check, scatter-gather fan-out

Design reference:
    - Issue #1744 Phase 2: scatter-gather chunked read
    - backends/base/cas_addressing_engine.py (CAS storage layer)
    - remote/peer_blob_client.py (peer-to-peer blob transport)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus.contracts.exceptions import NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.remote.peer_blob_client import PeerBlobClient

logger = logging.getLogger(__name__)


@runtime_checkable
class RemoteContentFetcher(Protocol):
    """Addressing-agnostic protocol for fetching content from remote origins.

    DriverLifecycleCoordinator.resolve_backend() calls this with a list of
    origins and a content hash.  The implementation owns transport, manifest
    logic, local caching, and scatter-gather strategy.
    """

    def fetch_remote_content(
        self,
        origins: list[str],
        content_hash: str,
    ) -> bytes:
        """Fetch content by hash from remote origins, store locally, return bytes.

        Args:
            origins: Peer addresses that may have the content (from BackendAddress).
            content_hash: Content hash (e.g. SHA-256 hex) identifying the blob.

        Returns:
            Assembled content bytes.

        Raises:
            NexusFileNotFoundError: If no origin has the content.
        """
        ...


class CASRemoteContentFetcher:
    """CAS+CDC implementation: manifest parse, local chunk check, scatter-gather.

    Absorbs all CAS-specific logic that was previously spread across the
    federation content resolution layer.

    Args:
        peer_blob_client: Transport for fetching blobs by hash from peers.
        local_object_store: Local CAS backend for existence checks and storage.
    """

    __slots__ = ("_client", "_store")

    def __init__(
        self,
        peer_blob_client: "PeerBlobClient",
        local_object_store: Any,
    ) -> None:
        self._client = peer_blob_client
        self._store = local_object_store

    def fetch_remote_content(
        self,
        origins: list[str],
        content_hash: str,
    ) -> bytes:
        """CAS+CDC fetch: manifest → local check → scatter-gather missing → assemble.

        After this method completes, all blobs (manifest + chunks) are in
        local CAS.  Returns the fully assembled content.
        """
        store = self._store
        client = self._client

        # Step 1: Check if content already exists locally (e.g. replicated)
        if hasattr(store, "content_exists") and store.content_exists(content_hash):
            logger.debug("CAS remote fetch: %s found in local CAS", content_hash[:12])
            result: bytes = store.read_content(content_hash)
            return result

        # Step 2: Fetch the blob by hash from first reachable origin
        blob_data = self._fetch_blob_from_origins(origins, content_hash)

        # Step 3: Check if it's a CDC manifest
        from nexus.backends.engines.cdc import ChunkedReference

        if not ChunkedReference.is_chunked_manifest(blob_data):
            # Single-blob file — store locally and return
            store.write_content(blob_data)
            return blob_data

        # Step 4: Parse manifest → check local CAS for each chunk
        manifest = ChunkedReference.from_json(blob_data)
        missing_hashes: list[str] = []
        for ci in manifest.chunks:
            if hasattr(store, "content_exists") and store.content_exists(ci.chunk_hash):
                continue
            missing_hashes.append(ci.chunk_hash)

        logger.info(
            "CAS remote fetch: %d chunks, %d local, %d missing",
            manifest.chunk_count,
            manifest.chunk_count - len(missing_hashes),
            len(missing_hashes),
        )

        # Step 5: Fetch missing chunks — scatter-gather across all origins
        if missing_hashes:
            if len(origins) > 1:
                fetched = client.fetch_blobs_scatter(origins, missing_hashes)
            else:
                fetched = client.fetch_blobs(origins[0], missing_hashes)
            for chunk_hash, chunk_data in fetched.items():
                store.write_content(chunk_data)
                logger.debug("Stored missing chunk %s (%d bytes)", chunk_hash[:12], len(chunk_data))

        # Step 6: Store manifest blob locally
        store.write_content(blob_data)

        # Step 7: Read assembled content via local CAS (CDCEngine handles assembly)
        assembled: bytes = store.read_content(content_hash)
        return assembled

    def _fetch_blob_from_origins(self, origins: list[str], content_hash: str) -> bytes:
        """Fetch a single blob, trying each origin in order until one succeeds."""
        last_err: Exception | None = None
        for origin in origins:
            try:
                return self._client.fetch_blob(origin, content_hash)
            except NexusFileNotFoundError as exc:
                last_err = exc
                logger.warning(
                    "CAS remote fetch: %s not found on %s, trying next",
                    content_hash[:12],
                    origin,
                )
                continue
        raise NexusFileNotFoundError(
            content_hash,
            f"Blob {content_hash[:16]} not found on any origin",
        ) from last_err
