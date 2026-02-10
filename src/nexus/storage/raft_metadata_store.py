"""Raft-backed metadata store for Nexus.

This is the primary metadata storage for Nexus, using an embedded sled database
with optional Raft consensus for multi-node deployments.

Architecture:
    Embedded:  Python -> Metastore (PyO3) -> sled (~5μs)
    SC mode:   Python -> RaftConsensus (PyO3) -> Raft consensus -> sled (~2-10ms)
    EC mode:   Python -> RaftConsensus (PyO3, lazy) -> local apply + bg propose (~5μs)
    Remote:    Python -> gRPC -> Rust (nexus_raft) -> sled (~200μs)

Usage:
    # Embedded mode (same box) - DEFAULT
    store = RaftMetadataStore.embedded("/var/lib/nexus/metadata")

    # SC mode (multi-node with Raft consensus)
    store = RaftMetadataStore.sc(1, "/var/lib/nexus/metadata", peers=["2@peer:2126"])

    # EC mode (multi-node with lazy consensus)
    store = RaftMetadataStore.ec(1, "/var/lib/nexus/metadata", peers=["2@peer:2126"])

    # Remote mode (thin client)
    store = await RaftMetadataStore.remote("10.0.0.2:2026")
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol, PaginatedResult

logger = logging.getLogger(__name__)

# Try to import protobuf code (may not be available in CI/testing)
try:
    from nexus.core import metadata_pb2

    _HAS_PROTOBUF = True
except ImportError:
    _HAS_PROTOBUF = False
    logger.debug(
        "metadata_pb2 not available (protobuf code not generated). "
        "Falling back to JSON serialization. This is expected in CI/testing."
    )


def _serialize_metadata(metadata: FileMetadata) -> bytes:
    """Serialize FileMetadata to bytes for Raft storage.

    Uses protobuf when available, falls back to JSON otherwise.
    """
    if _HAS_PROTOBUF:
        proto = metadata_pb2.FileMetadata(
            path=metadata.path,
            backend_name=metadata.backend_name,
            physical_path=metadata.physical_path or "",
            size=metadata.size,
            etag=metadata.etag or "",
            mime_type=metadata.mime_type or "",
            created_at=metadata.created_at.isoformat() if metadata.created_at else "",
            modified_at=metadata.modified_at.isoformat() if metadata.modified_at else "",
            version=metadata.version,
            zone_id=metadata.zone_id or "",
            created_by=metadata.created_by or "",
            is_directory=metadata.is_directory,
            owner_id=metadata.owner_id or "",
        )
        return proto.SerializeToString()
    else:
        # Fallback to JSON serialization
        obj = {
            "path": metadata.path,
            "backend_name": metadata.backend_name,
            "physical_path": metadata.physical_path,
            "size": metadata.size,
            "etag": metadata.etag,
            "mime_type": metadata.mime_type,
            "created_at": metadata.created_at.isoformat() if metadata.created_at else None,
            "modified_at": metadata.modified_at.isoformat() if metadata.modified_at else None,
            "version": metadata.version,
            "zone_id": metadata.zone_id,
            "created_by": metadata.created_by,
            "is_directory": metadata.is_directory,
            "owner_id": metadata.owner_id,
        }
        return json.dumps(obj).encode("utf-8")


def _deserialize_metadata(data: bytes | list[int]) -> FileMetadata:
    """Deserialize bytes to FileMetadata.

    Supports both protobuf (new) and JSON (fallback) formats.
    """
    # Handle both bytes and list of ints (from PyO3)
    if isinstance(data, list):
        data = bytes(data)

    # Try protobuf first if available
    if _HAS_PROTOBUF:
        try:
            proto = metadata_pb2.FileMetadata()
            proto.ParseFromString(data)
            # Convert protobuf to dataclass
            created_at = None
            modified_at = None
            if proto.created_at:
                try:
                    created_at = datetime.fromisoformat(proto.created_at)
                except ValueError:
                    pass
            if proto.modified_at:
                try:
                    modified_at = datetime.fromisoformat(proto.modified_at)
                except ValueError:
                    pass
            return FileMetadata(
                path=proto.path,
                backend_name=proto.backend_name,
                physical_path=proto.physical_path or None,
                size=proto.size,
                etag=proto.etag or None,
                mime_type=proto.mime_type or None,
                created_at=created_at,
                modified_at=modified_at,
                version=proto.version,
                zone_id=proto.zone_id or None,
                created_by=proto.created_by or None,
                is_directory=proto.is_directory,
                owner_id=proto.owner_id or None,
            )
        except Exception as proto_err:
            # Log protobuf parse failure; will try JSON fallback next.
            import logging

            logging.getLogger(__name__).debug(
                "Protobuf parse failed, trying JSON fallback: %s", proto_err
            )

    # Fallback to JSON format
    try:
        obj = json.loads(data.decode("utf-8"))
        if obj.get("created_at"):
            obj["created_at"] = datetime.fromisoformat(obj["created_at"])
        if obj.get("modified_at"):
            obj["modified_at"] = datetime.fromisoformat(obj["modified_at"])
        return FileMetadata(**obj)
    except Exception as e:
        raise ValueError(f"Failed to deserialize metadata: {e}") from e


class RaftMetadataStore(FileMetadataProtocol):
    """Primary metadata store for Nexus using embedded sled database.

    This store provides fast local metadata operations (~5μs) with optional
    Raft consensus for multi-node strong consistency.

    Four modes of operation:
    1. Embedded mode (DEFAULT): Direct sled via Metastore PyO3 (~5μs latency)
    2. SC mode: Raft consensus via RaftConsensus PyO3 (~2-10ms, replicated)
    3. EC mode: Lazy consensus via RaftConsensus PyO3 (~5μs, background replication)
    4. Remote mode: gRPC client (~200μs latency)

    Example:
        # Embedded mode (default)
        store = RaftMetadataStore.embedded("/var/lib/nexus/metadata")
        store.put(metadata)

        # SC mode (multi-node consensus)
        store = RaftMetadataStore.sc(1, "/var/lib/nexus/metadata", peers=["2@peer:2126"])
        store.put(metadata)  # replicated

        # EC mode (multi-node lazy consensus)
        store = RaftMetadataStore.ec(1, "/var/lib/nexus/metadata", peers=["2@peer:2126"])
        store.put(metadata)  # local + background replication

        # Remote mode (thin client)
        store = await RaftMetadataStore.remote("10.0.0.2:2026")
    """

    def __init__(
        self,
        local_raft: Any | None = None,
        remote_client: Any | None = None,
        zone_id: str | None = None,
    ):
        """Initialize RaftMetadataStore.

        Use the factory methods `embedded()`, `sc()`, or `remote()` instead
        of calling this constructor directly.

        Args:
            local_raft: Metastore or RaftConsensus instance (PyO3 FFI)
            remote_client: RaftClient instance (gRPC)
            zone_id: Zone ID for this store
        """
        if local_raft is None and remote_client is None:
            raise ValueError("Either local_raft or remote_client must be provided")

        self._local = local_raft
        self._remote = remote_client
        self._zone_id = zone_id
        self._is_local = local_raft is not None

    @classmethod
    def embedded(cls, db_path: str, zone_id: str | None = None) -> RaftMetadataStore:
        """Create an embedded metastore using direct sled access.

        This is the fast path (~5μs per operation) for embedded/standalone mode.

        Args:
            db_path: Path to the sled database directory
            zone_id: Zone ID for this store

        Returns:
            RaftMetadataStore instance
        """
        from nexus.raft import Metastore

        if Metastore is None:
            raise RuntimeError(
                "Metastore not available. Build with: "
                "maturin develop -m rust/nexus_raft/Cargo.toml --features python"
            )

        metastore = Metastore(db_path)
        logger.info(f"Created embedded RaftMetadataStore at {db_path}")
        return cls(local_raft=metastore, zone_id=zone_id)

    @classmethod
    def sc(
        cls,
        node_id: int,
        db_path: str,
        bind_address: str = "0.0.0.0:2126",
        peers: list[str] | None = None,
        zone_id: str | None = None,
    ) -> RaftMetadataStore:
        """Create an SC (Strong Consistency) metastore with Raft consensus.

        Each NexusFS node becomes a full Raft participant. Writes go through
        consensus (replicated to peers), reads are local (~5us).

        Args:
            node_id: Unique node ID within the cluster (1-indexed).
            db_path: Path to the sled database directory.
            bind_address: gRPC bind address for Raft inter-node traffic.
            peers: List of peer addresses in "id@host:port" format.
            zone_id: Zone ID for this store.

        Returns:
            RaftMetadataStore instance with Raft consensus (SC mode).
        """
        from nexus.raft import RaftConsensus

        if RaftConsensus is None:
            raise RuntimeError(
                "RaftConsensus not available. Build with: "
                "maturin develop -m rust/nexus_raft/Cargo.toml --features full"
            )

        consensus = RaftConsensus(node_id, db_path, bind_address, peers or [])
        logger.info(
            f"Created SC RaftMetadataStore (node={node_id}, bind={bind_address}, "
            f"peers={len(peers or [])})"
        )
        return cls(local_raft=consensus, zone_id=zone_id)

    @classmethod
    def ec(
        cls,
        node_id: int,
        db_path: str,
        bind_address: str = "0.0.0.0:2126",
        peers: list[str] | None = None,
        zone_id: str | None = None,
    ) -> RaftMetadataStore:
        """Create an EC (Eventual Consistency) metastore with lazy consensus.

        Metadata writes apply locally (~5μs) and replicate in the background
        via Raft propose with retry. Lock operations always use SC (consensus).

        Args:
            node_id: Unique node ID within the cluster (1-indexed).
            db_path: Path to the sled database directory.
            bind_address: gRPC bind address for Raft inter-node traffic.
            peers: List of peer addresses in "id@host:port" format.
            zone_id: Zone ID for this store.

        Returns:
            RaftMetadataStore instance with lazy consensus (EC mode).
        """
        from nexus.raft import RaftConsensus

        if RaftConsensus is None:
            raise RuntimeError(
                "RaftConsensus not available. Build with: "
                "maturin develop -m rust/nexus_raft/Cargo.toml --features full"
            )

        consensus = RaftConsensus(node_id, db_path, bind_address, peers or [], lazy=True)
        logger.info(
            f"Created EC RaftMetadataStore (node={node_id}, bind={bind_address}, "
            f"peers={len(peers or [])})"
        )
        return cls(local_raft=consensus, zone_id=zone_id)

    @classmethod
    async def remote(
        cls,
        address: str,
        zone_id: str | None = None,
    ) -> RaftMetadataStore:
        """Create a remote Raft metadata store using gRPC.

        This connects to a remote Raft node over gRPC (~200μs per operation).

        Args:
            address: Raft node address (e.g., "10.0.0.2:2026")
            zone_id: Zone ID for this store

        Returns:
            RaftMetadataStore instance
        """
        from nexus.raft import RaftClient

        client = RaftClient(address, zone_id=zone_id)
        await client.connect()
        logger.info(f"Created remote RaftMetadataStore connected to {address}")
        return cls(remote_client=client, zone_id=zone_id)

    def get(self, path: str) -> FileMetadata | None:
        """Get metadata for a file.

        Args:
            path: Virtual path

        Returns:
            FileMetadata if found, None otherwise
        """
        if self._is_local:
            data = self._local.get_metadata(path)
            if data is None:
                return None
            return _deserialize_metadata(data)
        else:
            # Remote mode - would need async, but interface is sync
            # For now, raise error - caller should use async methods
            raise NotImplementedError("Remote mode requires async. Use get_async() instead.")

    def put(self, metadata: FileMetadata) -> None:
        """Store or update file metadata.

        Args:
            metadata: File metadata to store
        """
        data = _serialize_metadata(metadata)

        if self._is_local:
            self._local.set_metadata(metadata.path, data)
        else:
            raise NotImplementedError("Remote mode requires async. Use put_async() instead.")

    def delete(self, path: str) -> dict[str, Any] | None:
        """Delete file metadata.

        Args:
            path: Virtual path

        Returns:
            Dictionary with deleted file info or None
        """
        # Get existing metadata before delete for return value
        existing = self.get(path)

        if self._is_local:
            self._local.delete_metadata(path)
        else:
            raise NotImplementedError("Remote mode requires async. Use delete_async() instead.")

        if existing:
            return {
                "path": existing.path,
                "size": existing.size,
                "etag": existing.etag,
            }
        return None

    def exists(self, path: str) -> bool:
        """Check if metadata exists for a path.

        Args:
            path: Virtual path

        Returns:
            True if metadata exists, False otherwise
        """
        return self.get(path) is not None

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Rename a file by updating its path in metadata.

        Args:
            old_path: Current path
            new_path: New path

        Raises:
            FileNotFoundError: If old_path doesn't exist
        """
        metadata = self.get(old_path)
        if metadata is None:
            raise FileNotFoundError(f"No metadata found for {old_path}")

        # Create new metadata with updated path
        new_metadata = FileMetadata(
            path=new_path,
            backend_name=metadata.backend_name,
            physical_path=metadata.physical_path,
            size=metadata.size,
            version=metadata.version,
            created_at=metadata.created_at,
            modified_at=metadata.modified_at,
            etag=metadata.etag,
            mime_type=metadata.mime_type,
            zone_id=metadata.zone_id,
            created_by=metadata.created_by,
            is_directory=metadata.is_directory,
            owner_id=metadata.owner_id,
        )

        # Delete old, put new
        self.delete(old_path)
        self.put(new_metadata)

    def is_implicit_directory(self, path: str) -> bool:
        """Check if a path is an implicit directory.

        An implicit directory exists because files exist underneath it,
        even though the directory itself has no explicit metadata.
        This is common in object storage systems like S3.

        Args:
            path: Virtual path to check

        Returns:
            True if path is an implicit directory, False otherwise
        """
        # Normalize path to ensure consistent prefix matching
        if not path.endswith("/"):
            prefix = path + "/"
        else:
            prefix = path

        # Check if any files exist with this prefix
        # We just need to find one file to confirm it's an implicit directory
        if self._is_local:
            entries = self._local.list_metadata(prefix)
            return len(entries) > 0
        else:
            raise NotImplementedError(
                "Remote mode requires async. Use is_implicit_directory_async() instead."
            )

    def list(
        self,
        prefix: str = "",
        recursive: bool = True,
        zone_id: str | None = None,
        accessible_int_ids: set[int] | None = None,
    ) -> list[FileMetadata]:
        """List all files with given path prefix.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files
            zone_id: Zone ID (ignored - store is zone-local)
            accessible_int_ids: Optional set of accessible file int_ids for filtering

        Returns:
            List of file metadata
        """
        # Note: zone_id is ignored since RaftMetadataStore is zone-local
        if self._is_local:
            entries = self._local.list_metadata(prefix)
            result = []
            for path, data in entries:
                # Skip extended attribute keys (format: "meta:{path}:{key}")
                # These are stored by set_file_metadata() and are NOT file entries.
                if path.startswith("meta:"):
                    continue
                metadata = _deserialize_metadata(data)
                if not recursive:
                    # Filter to direct children only
                    rel_path = path[len(prefix) :].lstrip("/")
                    if "/" in rel_path:
                        continue
                # Filter by accessible_int_ids if provided
                if accessible_int_ids is not None:
                    if metadata.int_id is None or metadata.int_id not in accessible_int_ids:
                        continue
                result.append(metadata)
            return result
        else:
            raise NotImplementedError("Remote mode requires async. Use list_async() instead.")

    def list_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        zone_id: str | None = None,
    ) -> PaginatedResult:
        """List files with cursor-based pagination."""
        # Simple implementation - get all and paginate in memory
        all_items = self.list(prefix, recursive)

        # Handle cursor - decode if it's a base64-encoded cursor from pagination module
        start_idx = 0
        if cursor:
            cursor_path = cursor
            try:
                from nexus.core.pagination import decode_cursor

                filters = {
                    "prefix": prefix,
                    "recursive": recursive,
                    "zone_id": zone_id,
                }
                decoded = decode_cursor(cursor, filters)
                cursor_path = decoded.path
            except Exception:
                # Fall back to treating cursor as a raw path
                cursor_path = cursor

            for i, item in enumerate(all_items):
                if item.path > cursor_path:
                    start_idx = i
                    break
            else:
                # No item found after cursor - we're past the end
                start_idx = len(all_items)

        page = all_items[start_idx : start_idx + limit]
        has_more = start_idx + limit < len(all_items)
        next_cursor = page[-1].path if has_more and page else None

        return PaginatedResult(
            items=page,
            next_cursor=next_cursor,
            has_more=has_more,
            total_count=len(all_items),
        )

    def close(self) -> None:
        """Close the metadata store and release resources."""
        if self._is_local:
            if hasattr(self._local, "shutdown"):
                # RaftConsensus: gracefully stop gRPC server + transport loop
                self._local.shutdown()
            else:
                # Metastore: just flush sled
                self._local.flush()
        else:
            # Remote client close is async
            # Would need to handle this differently
            pass

    # =========================================================================
    # Batch Operations
    # =========================================================================

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        """Get metadata for multiple files.

        Args:
            paths: List of virtual paths

        Returns:
            Dictionary mapping path to FileMetadata (or None if not found)
        """
        return {path: self.get(path) for path in paths}

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        """Store or update multiple file metadata entries.

        Args:
            metadata_list: List of file metadata to store
        """
        for metadata in metadata_list:
            self.put(metadata)

    def delete_batch(self, paths: Sequence[str]) -> None:
        """Delete multiple files.

        Args:
            paths: List of virtual paths to delete
        """
        for path in paths:
            self.delete(path)

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        """Get content IDs (hashes) for multiple paths.

        Useful for CAS deduplication.

        Args:
            paths: List of virtual paths

        Returns:
            Dictionary mapping path to content_hash (or None if not found)
        """
        result = {}
        for path in paths:
            metadata = self.get(path)
            result[path] = metadata.etag if metadata else None
        return result

    # =========================================================================
    # Custom File Metadata (key-value pairs per file)
    # =========================================================================

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        """Store custom metadata key-value pair for a file.

        This is used for storing extended attributes like parsed_text,
        parsed_at, parser_name, etc.

        Args:
            path: Virtual file path
            key: Metadata key
            value: Metadata value (will be JSON serialized)
        """
        if self._is_local:
            # Store in a separate namespace: "meta:{path}:{key}"
            meta_key = f"meta:{path}:{key}"
            if value is None:
                # Delete the key
                self._local.delete_metadata(meta_key)
            else:
                # Store as JSON bytes
                data = json.dumps(value).encode("utf-8")
                self._local.set_metadata(meta_key, data)
        else:
            raise NotImplementedError(
                "Remote mode requires async. Use set_file_metadata_async() instead."
            )

    def get_file_metadata(self, path: str, key: str) -> Any:
        """Get custom metadata value for a file.

        Args:
            path: Virtual file path
            key: Metadata key

        Returns:
            Metadata value or None if not found
        """
        if self._is_local:
            meta_key = f"meta:{path}:{key}"
            data = self._local.get_metadata(meta_key)
            if data is None:
                return None
            # Handle both bytes and list of ints (from PyO3)
            if isinstance(data, list):
                data = bytes(data)
            return json.loads(data.decode("utf-8"))
        else:
            raise NotImplementedError(
                "Remote mode requires async. Use get_file_metadata_async() instead."
            )

    def get_file_metadata_bulk(self, paths: Sequence[str], key: str) -> dict[str, Any]:
        """Get custom metadata value for multiple files.

        Args:
            paths: List of virtual file paths
            key: Metadata key

        Returns:
            Dictionary mapping path to value (or None if not found)
        """
        return {path: self.get_file_metadata(path, key) for path in paths}

    def get_searchable_text(self, path: str) -> str | None:
        """Get cached searchable text for a file.

        Returns the parsed_text extended attribute, which is stored by
        auto_parse when files are written. Used by grep and semantic search.

        Args:
            path: Virtual file path

        Returns:
            Searchable text content or None if not cached
        """
        return self.get_file_metadata(path, "parsed_text")

    def get_searchable_text_bulk(self, paths: Sequence[str]) -> dict[str, str]:
        """Get cached searchable text for multiple files.

        Args:
            paths: List of virtual file paths

        Returns:
            Dictionary mapping path to text (only includes paths with cached text)
        """
        result = {}
        for path in paths:
            text = self.get_searchable_text(path)
            if text is not None:
                result[path] = text
        return result

    # =========================================================================
    # Lock Operations (Raft provides distributed locks)
    # =========================================================================

    def acquire_lock(
        self,
        path: str,
        holder_id: str,
        max_holders: int = 1,
        ttl_secs: int = 30,
    ) -> bool:
        """Acquire a distributed lock on a path.

        Args:
            path: Resource path to lock
            holder_id: Unique identifier for this holder
            max_holders: Maximum concurrent holders (1=mutex, >1=semaphore)
            ttl_secs: Lock TTL in seconds

        Returns:
            True if lock was acquired
        """
        if self._is_local:
            result = self._local.acquire_lock(
                path, holder_id, max_holders, ttl_secs, f"metadata:{holder_id}"
            )
            return result.acquired
        else:
            raise NotImplementedError("Remote locks require async")

    def release_lock(self, path: str, holder_id: str) -> bool:
        """Release a distributed lock.

        Args:
            path: Resource path
            holder_id: Holder identifier

        Returns:
            True if holder was found and released, False if not owned
        """
        if self._is_local:
            return self._local.release_lock(path, holder_id)
        else:
            raise NotImplementedError("Remote locks require async")

    def extend_lock(self, path: str, holder_id: str, ttl_secs: int = 30) -> bool:
        """Extend a lock's TTL (heartbeat).

        Args:
            path: Resource path
            holder_id: Holder identifier
            ttl_secs: New TTL in seconds

        Returns:
            True if holder was found and TTL extended, False if not owned
        """
        if self._is_local:
            return self._local.extend_lock(path, holder_id, ttl_secs)
        else:
            raise NotImplementedError("Remote locks require async. Use extend_lock_async()")

    def get_lock_info(self, path: str) -> dict[str, Any] | None:
        """Get lock information for a path.

        Args:
            path: Lock key (typically "zone_id:resource_path")

        Returns:
            Dict with lock info if lock exists and has holders, None otherwise.
            Dict keys: path, max_holders, holders (list of holder dicts)
        """
        if self._is_local:
            lock_info = self._local.get_lock(path)
            if lock_info is None or not lock_info.holders:
                return None
            return {
                "path": lock_info.path,
                "max_holders": lock_info.max_holders,
                "holders": [
                    {
                        "lock_id": h.lock_id,
                        "holder_info": h.holder_info,
                        "acquired_at": h.acquired_at,
                        "expires_at": h.expires_at,
                    }
                    for h in lock_info.holders
                ],
            }
        else:
            raise NotImplementedError("Remote lock info requires async")

    def list_locks(self, prefix: str = "", limit: int = 1000) -> list[dict[str, Any]]:
        """List all active locks matching a prefix.

        Args:
            prefix: Key prefix to filter by (e.g., "zone_id:" for zone-scoped)
            limit: Maximum number of results

        Returns:
            List of lock info dicts
        """
        if self._is_local:
            lock_infos = self._local.list_locks(prefix, limit)
            return [
                {
                    "path": lock.path,
                    "max_holders": lock.max_holders,
                    "holders": [
                        {
                            "lock_id": h.lock_id,
                            "holder_info": h.holder_info,
                            "acquired_at": h.acquired_at,
                            "expires_at": h.expires_at,
                        }
                        for h in lock.holders
                    ],
                }
                for lock in lock_infos
            ]
        else:
            raise NotImplementedError("Remote list_locks requires async")

    def force_release_lock(self, path: str) -> bool:
        """Force-release all holders of a lock (admin operation).

        Args:
            path: Lock key to force-release

        Returns:
            True if a lock was found and released, False if no lock exists
        """
        if self._is_local:
            return self._local.force_release_lock(path)
        else:
            raise NotImplementedError("Remote force_release requires async")

    # =========================================================================
    # Async Methods (for RemoteNexusFS using remote mode)
    # =========================================================================

    async def get_async(self, path: str) -> FileMetadata | None:
        """Get metadata for a file (async).

        Args:
            path: Virtual path

        Returns:
            FileMetadata if found, None otherwise
        """
        if self._is_local:
            # For local mode, wrap sync call
            data = self._local.get_metadata(path)
            if data is None:
                return None
            return _deserialize_metadata(data)
        else:
            # Remote mode - use RaftClient
            return await self._remote.get_metadata(path, zone_id=self._zone_id)

    async def put_async(self, metadata: FileMetadata) -> None:
        """Store or update file metadata (async).

        Args:
            metadata: File metadata to store
        """
        if self._is_local:
            data = _serialize_metadata(metadata)
            self._local.set_metadata(metadata.path, data)
        else:
            await self._remote.put_metadata(metadata, zone_id=self._zone_id)

    async def delete_async(self, path: str) -> dict[str, Any] | None:
        """Delete file metadata (async).

        Args:
            path: Virtual path

        Returns:
            Dictionary with deleted file info or None
        """
        # Get existing metadata before delete for return value
        existing = await self.get_async(path)

        if self._is_local:
            self._local.delete_metadata(path)
        else:
            await self._remote.delete_metadata(path, zone_id=self._zone_id)

        if existing:
            return {
                "path": existing.path,
                "size": existing.size,
                "etag": existing.etag,
            }
        return None

    async def exists_async(self, path: str) -> bool:
        """Check if metadata exists for a path (async).

        Args:
            path: Virtual path

        Returns:
            True if metadata exists, False otherwise
        """
        result = await self.get_async(path)
        return result is not None

    async def list_async(
        self,
        prefix: str = "",
        recursive: bool = True,
        zone_id: str | None = None,
    ) -> list[FileMetadata]:
        """List all files with given path prefix (async).

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files
            zone_id: Zone ID (ignored - store is zone-local)

        Returns:
            List of file metadata
        """
        if self._is_local:
            entries = self._local.list_metadata(prefix)
            result = []
            for path, data in entries:
                # Skip extended attribute keys (format: "meta:{path}:{key}")
                if path.startswith("meta:"):
                    continue
                metadata = _deserialize_metadata(data)
                if not recursive:
                    # Filter to direct children only
                    rel_path = path[len(prefix) :].lstrip("/")
                    if "/" in rel_path:
                        continue
                result.append(metadata)
            return result
        else:
            return await self._remote.list_metadata(
                prefix=prefix,
                zone_id=self._zone_id,
                recursive=recursive,
            )

    async def acquire_lock_async(
        self,
        path: str,
        holder_id: str,
        max_holders: int = 1,
        ttl_secs: int = 30,
    ) -> bool:
        """Acquire a distributed lock on a path (async).

        Args:
            path: Resource path to lock
            holder_id: Unique identifier for this holder
            max_holders: Maximum concurrent holders (1=mutex, >1=semaphore)
            ttl_secs: Lock TTL in seconds

        Returns:
            True if lock was acquired
        """
        if self._is_local:
            result = self._local.acquire_lock(
                path, holder_id, max_holders, ttl_secs, f"metadata:{holder_id}"
            )
            return result.acquired
        else:
            result = await self._remote.acquire_lock(
                lock_id=path,
                holder_id=holder_id,
                ttl_ms=ttl_secs * 1000,
                zone_id=self._zone_id,
            )
            return result.acquired

    async def release_lock_async(self, path: str, holder_id: str) -> bool:
        """Release a distributed lock (async).

        Args:
            path: Resource path
            holder_id: Holder identifier

        Returns:
            True if holder was found and released, False if not owned
        """
        if self._is_local:
            return self._local.release_lock(path, holder_id)
        else:
            return await self._remote.release_lock(
                lock_id=path,
                holder_id=holder_id,
                zone_id=self._zone_id,
            )

    async def extend_lock_async(self, path: str, holder_id: str, ttl_secs: int = 30) -> bool:
        """Extend a lock's TTL (async).

        Args:
            path: Resource path
            holder_id: Holder identifier
            ttl_secs: New TTL in seconds

        Returns:
            True if holder was found and TTL extended, False if not owned
        """
        if self._is_local:
            return self._local.extend_lock(path, holder_id, ttl_secs)
        else:
            return await self._remote.extend_lock(
                lock_id=path,
                holder_id=holder_id,
                ttl_ms=ttl_secs * 1000,
                zone_id=self._zone_id,
            )

    async def close_async(self) -> None:
        """Close the metadata store and release resources (async)."""
        if self._is_local:
            if hasattr(self._local, "shutdown"):
                self._local.shutdown()
            else:
                self._local.flush()
        else:
            await self._remote.close()
