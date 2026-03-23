"""FederationContentResolver — PRE-DISPATCH resolver for remote content (#163, #1665).

Registered as a VFSPathResolver in KernelDispatch.  Each ``try_*`` method
looks up metadata once, decides local vs remote, and either handles the
operation (remote → returns result) or declines (local/unknown → returns
``None``).

Zero kernel coupling: the kernel sees a standard VFSPathResolver.
Federation topology, gRPC channels, and progressive replication are
entirely encapsulated here.

CDC-aware federation read (#1744):
    When a PeerBlobClient and local ObjectStore are injected (Feature DI),
    the resolver fetches CDC manifests by hash, checks which chunks exist
    locally (bloom + stat), and only pulls missing chunks from the origin.
    After fetch, all chunks + manifest are in local CAS — subsequent reads
    are fully local (read-through caching).

Design reference:
    - docs/architecture/federation-memo.md §Content Read Path
    - BackendAddress: contracts/backend_address.py
    - KernelDispatch: core/kernel_dispatch.py (PRE-DISPATCH phase)
    - PeerBlobClient: remote/peer_blob_client.py (driver-to-driver blob fetch)
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.backend_address import BackendAddress
from nexus.contracts.exceptions import NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.core.metastore import MetastoreABC
    from nexus.remote.peer_blob_client import PeerBlobClient
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
        peer_blob_client: Optional PeerBlobClient for CDC-aware chunk assembly.
        local_object_store: Optional local ObjectStore for local CAS check + write.
    """

    name = "federation-content"

    def __init__(
        self,
        metastore: "MetastoreABC",
        self_address: str,
        tls_config: "ZoneTlsConfig | None" = None,
        timeout: float = 30.0,
        peer_blob_client: "PeerBlobClient | None" = None,
        local_object_store: Any = None,
    ) -> None:
        self._metastore = metastore
        self._self_address = self_address
        self._tls_config = tls_config
        self._timeout = timeout
        self._peer_blob_client = peer_blob_client
        self._local_object_store = local_object_store

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
        context: Any = None,
    ) -> bytes | None:
        """Single-call resolve: metadata lookup + local/remote decision.

        Returns:
            bytes — handled: content fetched from remote peer.
            None  — not handled: local content or no metadata.
        """
        _ = context  # unused, present for protocol conformance
        meta = self._metastore.get(path)
        if meta is None or not meta.backend_name:
            return None

        addr = BackendAddress.parse(meta.backend_name)
        if not addr.has_origin or self._self_address in addr.origins:
            return None  # local content — kernel handles

        # Remote content — try each origin until one responds
        content_hash = meta.etag or ""
        file_size = meta.size or 0
        last_err: Exception | None = None

        for origin in addr.origins:
            try:
                return self._read_from_origin(origin, path, content_hash, file_size)
            except NexusFileNotFoundError as exc:
                last_err = exc
                logger.warning("Federation read from %s failed, trying next origin", origin)
                continue

        raise NexusFileNotFoundError(path, f"All origins unreachable for {path}") from last_err

    def _read_from_origin(self, origin: str, path: str, content_hash: str, file_size: int) -> bytes:
        """Read content from a single origin — CDC-aware when possible.

        Strategy selection:
        1. PeerBlobClient + local ObjectStore available + content_hash known
           → CDC-aware: fetch by hash, check manifest, local chunk assembly
        2. Fallback → existing path-based Read/StreamRead
        """
        can_use_blob_fetch = (
            self._peer_blob_client is not None
            and self._local_object_store is not None
            and content_hash
        )

        if can_use_blob_fetch:
            logger.info(
                "Federation read (blob): %s -> %s (hash=%s, size=%d)",
                path,
                origin,
                content_hash[:12],
                file_size,
            )
            return self._fetch_content_by_hash(origin, content_hash)

        # Fallback: path-based fetch
        use_streaming = file_size > _STREAMING_THRESHOLD
        logger.info(
            "Federation read (path): %s -> %s (size=%d, streaming=%s)",
            path,
            origin,
            file_size,
            use_streaming,
        )
        if use_streaming:
            return self._fetch_from_peer_streaming(origin, path)
        return self._fetch_from_peer(origin, path)

    def _fetch_content_by_hash(self, origin: str, content_hash: str) -> bytes:
        """CDC-aware content fetch: manifest → local check → fetch missing → assemble.

        After this method completes, all blobs (manifest + chunks) are in
        local CAS.  Returns the fully assembled content.
        """
        assert self._peer_blob_client is not None  # noqa: S101
        assert self._local_object_store is not None  # noqa: S101
        store = self._local_object_store
        client = self._peer_blob_client

        # Step 1: Check if content already exists locally (e.g. replicated)
        if hasattr(store, "content_exists") and store.content_exists(content_hash):
            logger.debug("Federation read: %s found in local CAS", content_hash[:12])
            result: bytes = store.read_content(content_hash)
            return result

        # Step 2: Fetch the blob by hash from origin
        blob_data = client.fetch_blob(origin, content_hash)

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
            "Federation chunked read: %d chunks, %d local, %d missing",
            manifest.chunk_count,
            manifest.chunk_count - len(missing_hashes),
            len(missing_hashes),
        )

        # Step 5: Fetch missing chunks in parallel
        if missing_hashes:
            fetched = client.fetch_blobs(origin, missing_hashes)
            for chunk_hash, chunk_data in fetched.items():
                store.write_content(chunk_data)
                logger.debug("Stored missing chunk %s (%d bytes)", chunk_hash[:12], len(chunk_data))

        # Step 6: Store manifest blob locally
        # write_content will hash it and store; the hash should match content_hash
        store.write_content(blob_data)

        # Step 7: Read assembled content via local CAS (CDCEngine handles assembly)
        assembled: bytes = store.read_content(content_hash)
        return assembled

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
        if not addr.has_origin or self._self_address in addr.origins:
            return None  # local content — kernel handles

        # Remote content — delegate delete to first reachable origin
        for origin in addr.origins:
            logger.info(
                "Federation delete: %s -> %s (etag=%s)",
                path,
                origin,
                (meta.etag or "")[:12],
            )
            try:
                self._delete_on_peer(origin, path)
                # Clean up local replica blob if we have a local ObjectStore (#1310)
                self._cleanup_local_replica(meta.etag)
                return {}
            except Exception:
                logger.warning("Federation delete to %s failed, trying next origin", origin)
                continue

        logger.warning("Federation delete: all origins unreachable for %s", path)
        return {}

    # === Local replica cleanup (#1310) ===

    def _cleanup_local_replica(self, content_hash: str | None) -> None:
        """Best-effort cleanup of local replica blob after remote delete.

        If local_object_store is injected and has the content, delete it.
        Failure is logged but never propagates — the remote delete already
        succeeded, and orphan blobs are harmless (just waste disk).
        """
        if not content_hash or self._local_object_store is None:
            return
        store = self._local_object_store
        try:
            if hasattr(store, "content_exists") and store.content_exists(content_hash):
                store.delete_content(content_hash)
                logger.debug(
                    "Cleaned up local replica blob %s",
                    content_hash[:16],
                )
        except Exception:
            logger.debug(
                "Failed to clean up local replica %s (harmless)",
                content_hash[:16],
            )

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
