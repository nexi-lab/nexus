"""ContentReplicationService — background content pull for replicated paths.

Each Voter independently scans metastore entries under replicated mount
prefixes, identifies entries where ``self_address`` is not in
``backend_name.origins``, pulls content from a listed origin via gRPC
Read RPC, stores it locally in ObjectStoreABC, and appends
``self_address`` to ``backend_name``.

Key invariant: ``backend_name`` is updated ONLY after content is
confirmed stored locally. Never "expect have but don't have."

Design reference:
    - Plan B: Peer-Initiated Pull via Metastore Scan
    - BackendAddress multi-origin: contracts/backend_address.py
    - ReplicationPolicyResolver: raft/replication_policy.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from nexus.contracts.backend_address import BackendAddress

if TYPE_CHECKING:
    from nexus.contracts.metadata import FileMetadata
    from nexus.raft.replication_policy import ReplicationPolicyResolver
    from nexus.security.tls.config import ZoneTlsConfig

logger = logging.getLogger(__name__)

_DEFAULT_SCAN_INTERVAL = 2.0  # seconds


class _MetastoreProto(Protocol):
    def get(self, path: str) -> FileMetadata | None: ...
    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None: ...
    def list(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> list[FileMetadata]: ...


class _ObjectStoreProto(Protocol):
    def write_content(
        self, content: bytes, content_id: str = "", *, context: Any = None
    ) -> Any: ...
    def read_content(self, content_id: str, context: Any = None) -> bytes: ...


class ContentReplicationService:
    """PersistentService: background content replication for replicated paths.

    Scan loop (every N seconds):
      1. Refresh replication policies from mount configs
      2. For each replicated path prefix, scan metastore entries
      3. For entries where self_address not in backend_name.origins:
         a. Pull content from any listed origin via gRPC ReadBlob RPC
            (hash-based, CDC-aware when PeerBlobClient is injected)
         b. Store in local ObjectStoreABC.write_content()
         c. Update backend_name to include self_address (via metastore.put())

    Implements PersistentService protocol (Q3):
        - async start() — launch background scan loop
        - async stop()  — graceful shutdown
    """

    def __init__(
        self,
        metastore: _MetastoreProto,
        object_store: _ObjectStoreProto,
        policy_resolver: ReplicationPolicyResolver,
        self_address: str,
        *,
        tls_config: ZoneTlsConfig | None = None,
        scan_interval: float = _DEFAULT_SCAN_INTERVAL,
        timeout: float = 30.0,
        peer_blob_client: Any = None,
    ) -> None:
        self._metastore = metastore
        self._object_store = object_store
        self._policy_resolver = policy_resolver
        self._self_address = self_address
        self._tls_config = tls_config
        self._scan_interval = scan_interval
        self._timeout = timeout
        self._peer_blob_client = peer_blob_client
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # PersistentService protocol (Q3)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch background scan loop."""
        if self._task is not None and not self._task.done():
            return  # idempotent
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="content-replication")
        logger.info(
            "[REPLICATION] Started (interval=%.1fs, self=%s)",
            self._scan_interval,
            self._self_address,
        )

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._stopped = True
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("[REPLICATION] Stopped")

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Periodic scan loop — runs until stopped."""
        while not self._stopped:
            try:
                await asyncio.to_thread(self._scan_and_replicate)
            except Exception:
                logger.exception("[REPLICATION] Scan cycle failed")
            try:
                await asyncio.sleep(self._scan_interval)
            except asyncio.CancelledError:
                break

    def _scan_and_replicate(self) -> None:
        """One scan iteration (runs in thread)."""
        self._policy_resolver.refresh()
        prefixes = self._policy_resolver.get_replicated_prefixes()
        if not prefixes:
            return

        replicated = 0
        for prefix in prefixes:
            entries = self._metastore.list(prefix, recursive=True)
            for meta in entries:
                if self._stopped:
                    return
                if not meta.backend_name or "@" not in meta.backend_name:
                    continue  # no origin info — skip
                if meta.is_dir or meta.is_mount:
                    continue  # only replicate regular files

                addr = BackendAddress.parse(meta.backend_name)
                if not addr.has_origin:
                    continue
                if self._self_address in addr.origins:
                    continue  # already replicated locally

                if self._replicate_entry(meta, addr):
                    replicated += 1

        if replicated > 0:
            logger.info("[REPLICATION] Replicated %d entries", replicated)

    def _replicate_entry(self, meta: FileMetadata, addr: BackendAddress) -> bool:
        """Pull content from a remote origin and store locally.

        Uses hash-based ReadBlob when PeerBlobClient is available (CDC-aware),
        falls back to path-based Read RPC otherwise.

        Returns True on success, False on failure (will retry next scan).
        """
        content_hash = meta.etag or ""
        use_blob_fetch = self._peer_blob_client is not None and content_hash

        for origin in addr.origins:
            try:
                if use_blob_fetch:
                    self._replicate_by_hash(origin, content_hash)
                else:
                    content = self._fetch_from_peer(origin, meta.path)
                    self._object_store.write_content(content)

                # Update backend_name to include self
                updated_addr = addr.with_origin(self._self_address)
                updated_meta = replace(meta, backend_name=str(updated_addr))
                self._metastore.put(updated_meta)
                logger.debug(
                    "[REPLICATION] %s pulled from %s (hash=%s)",
                    meta.path,
                    origin,
                    content_hash[:12] if content_hash else "n/a",
                )
                return True
            except Exception:
                logger.debug(
                    "[REPLICATION] Failed to pull %s from %s, trying next",
                    meta.path,
                    origin,
                )
                continue

        logger.warning("[REPLICATION] All origins unreachable for %s", meta.path)
        return False

    def _replicate_by_hash(self, origin: str, content_hash: str) -> None:
        """CDC-aware replication: fetch manifest → local check → fetch missing chunks.

        Same logic as FederationContentResolver._fetch_content_by_hash but for
        background replication (stores content locally, no assembly needed).
        """
        client = self._peer_blob_client
        store = self._object_store

        # Already replicated?
        if hasattr(store, "content_exists") and store.content_exists(content_hash):
            return

        blob_data = client.fetch_blob(origin, content_hash)

        from nexus.backends.engines.cdc import ChunkedReference

        if not ChunkedReference.is_chunked_manifest(blob_data):
            store.write_content(blob_data)
            return

        # CDC manifest — fetch missing chunks
        manifest = ChunkedReference.from_json(blob_data)
        missing = [
            ci.chunk_hash
            for ci in manifest.chunks
            if not (hasattr(store, "content_exists") and store.content_exists(ci.chunk_hash))
        ]

        if missing:
            fetched = client.fetch_blobs(origin, missing)
            for chunk_data in fetched.values():
                store.write_content(chunk_data)

        # Store manifest
        store.write_content(blob_data)

    # ------------------------------------------------------------------
    # gRPC content pull (reuses FederationContentResolver pattern)
    # ------------------------------------------------------------------

    def _fetch_from_peer(self, address: str, virtual_path: str) -> bytes:
        """Fetch content from peer via gRPC Read RPC."""
        import grpc

        from nexus.grpc.channel_factory import build_peer_channel
        from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

        channel = build_peer_channel(address, self._tls_config)
        try:
            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            request = vfs_pb2.ReadRequest(path=virtual_path, auth_token="")
            response = stub.Read(request, timeout=self._timeout)
            if response.is_error:
                raise RuntimeError(f"Remote peer {address} returned error for {virtual_path}")
            return bytes(response.content)
        except grpc.RpcError as exc:
            raise RuntimeError(f"Remote peer {address} unreachable: {exc}") from exc
        finally:
            channel.close()
