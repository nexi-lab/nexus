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
from collections.abc import Iterator, Sequence
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
    Delegates field mapping to MetadataMapper (Issue #1246).
    """
    from nexus.storage.metadata_mapper import MetadataMapper

    if _HAS_PROTOBUF:
        proto = MetadataMapper.to_proto(metadata)
        return proto.SerializeToString()
    else:
        # Fallback to JSON serialization
        obj = MetadataMapper.to_json(metadata)
        return json.dumps(obj).encode("utf-8")


def _deserialize_metadata(data: bytes | list[int]) -> FileMetadata:
    """Deserialize bytes to FileMetadata.

    Supports both protobuf (new) and JSON (fallback) formats.
    Delegates field mapping to MetadataMapper (Issue #1246).
    """
    from nexus.storage.metadata_mapper import MetadataMapper

    # Handle both bytes and list of ints (from PyO3)
    if isinstance(data, list):
        data = bytes(data)

    # Try protobuf first if available
    if _HAS_PROTOBUF:
        try:
            proto = metadata_pb2.FileMetadata()
            proto.ParseFromString(data)
            return MetadataMapper.from_proto(proto)
        except Exception as proto_err:
            logger.debug("Protobuf parse failed, trying JSON fallback: %s", proto_err)

    # Fallback to JSON format
    try:
        obj = json.loads(data.decode("utf-8"))
        return MetadataMapper.from_json(obj)
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
        engine: Any | None = None,
        client: Any | None = None,
        zone_id: str | None = None,
    ):
        """Initialize RaftMetadataStore.

        Use the factory methods `embedded()`, `sc()`, `ec()`, or `remote()`
        instead of calling this constructor directly.

        Args:
            engine: Metastore or RaftConsensus instance (PyO3 FFI)
            client: RaftClient instance (gRPC)
            zone_id: Zone ID for this store
        """
        if engine is None and client is None:
            raise ValueError("Either engine or client must be provided")

        self._engine = engine
        self._client = client
        self._zone_id = zone_id

    @property
    def _has_engine(self) -> bool:
        """True if this store has an embedded engine (not a gRPC client)."""
        return self._engine is not None

    # =========================================================================
    # Shared Engine Helpers — DRY extraction
    # Used by both sync and async public methods for engine mode operations.
    # =========================================================================

    def _get_engine(self, path: str) -> FileMetadata | None:
        """Get metadata from the embedded sled engine.

        Args:
            path: Virtual path

        Returns:
            FileMetadata if found, None otherwise
        """
        data = self._engine.get_metadata(path)
        if data is None:
            return None
        return _deserialize_metadata(data)

    def _put_engine(self, metadata: FileMetadata) -> None:
        """Store metadata in the embedded sled engine.

        Args:
            metadata: File metadata to store
        """
        data = _serialize_metadata(metadata)
        self._engine.set_metadata(metadata.path, data)

    def _delete_engine(self, path: str) -> dict[str, Any] | None:
        """Delete metadata from the embedded sled engine.

        Args:
            path: Virtual path

        Returns:
            Dictionary with deleted file info or None
        """
        existing = self._get_engine(path)
        self._engine.delete_metadata(path)
        if existing:
            return {
                "path": existing.path,
                "size": existing.size,
                "etag": existing.etag,
            }
        return None

    def _list_engine(
        self,
        prefix: str = "",
        recursive: bool = True,
        accessible_int_ids: set[int] | None = None,
    ) -> list[FileMetadata]:
        """List metadata entries from the embedded sled engine.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files
            accessible_int_ids: Optional set of accessible file int_ids for filtering

        Returns:
            List of file metadata
        """
        entries = self._engine.list_metadata(prefix)
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
            # Filter by accessible_int_ids if provided
            if accessible_int_ids is not None:
                if metadata.int_id is None or metadata.int_id not in accessible_int_ids:
                    continue
            result.append(metadata)
        return result

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
        return cls(engine=metastore, zone_id=zone_id)

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
        return cls(engine=consensus, zone_id=zone_id)

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
        return cls(engine=consensus, zone_id=zone_id)

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
        return cls(client=client, zone_id=zone_id)

    def get(self, path: str) -> FileMetadata | None:
        """Get metadata for a file.

        Args:
            path: Virtual path

        Returns:
            FileMetadata if found, None otherwise
        """
        if self._has_engine:
            return self._get_engine(path)
        else:
            raise NotImplementedError("Remote mode requires async. Use get_async() instead.")

    def put(self, metadata: FileMetadata) -> None:
        """Store or update file metadata.

        Args:
            metadata: File metadata to store
        """
        if self._has_engine:
            self._put_engine(metadata)
        else:
            raise NotImplementedError("Remote mode requires async. Use put_async() instead.")

    def delete(self, path: str) -> dict[str, Any] | None:
        """Delete file metadata.

        Args:
            path: Virtual path

        Returns:
            Dictionary with deleted file info or None
        """
        if self._has_engine:
            return self._delete_engine(path)
        else:
            raise NotImplementedError("Remote mode requires async. Use delete_async() instead.")

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
            entry_type=metadata.entry_type,
            target_zone_id=metadata.target_zone_id,
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
        if self._has_engine:
            entries = self._engine.list_metadata(prefix)
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
        if self._has_engine:
            return self._list_engine(prefix, recursive, accessible_int_ids)
        else:
            raise NotImplementedError("Remote mode requires async. Use list_async() instead.")

    def list_iter(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,
    ) -> Iterator[FileMetadata]:
        """Iterate over file metadata matching prefix.

        Memory-efficient alternative to list(). Yields results one at a time
        instead of materializing the full result list.

        The underlying sled store returns all matching entries at once, but this
        generator lets callers avoid accumulating all results into a second list.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files
            **kwargs: Accepts zone_id (ignored) and accessible_int_ids for filtering

        Yields:
            FileMetadata entries matching the prefix
        """
        accessible_int_ids: set[int] | None = kwargs.get("accessible_int_ids")

        if self._has_engine:
            entries = self._engine.list_metadata(prefix)
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
                # Filter by accessible_int_ids if provided
                if accessible_int_ids is not None:
                    if metadata.int_id is None or metadata.int_id not in accessible_int_ids:
                        continue
                yield metadata
        else:
            raise NotImplementedError("Remote mode requires async. Use list_async() instead.")

    def list_iter(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,
    ) -> Iterator[FileMetadata]:
        """Iterate over file metadata matching prefix.

        Memory-efficient alternative to list(). Yields results one at a time
        instead of materializing the full result list.

        The underlying sled store returns all matching entries at once, but this
        generator lets callers avoid accumulating all results into a second list.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files
            **kwargs: Accepts zone_id (ignored) and accessible_int_ids for filtering

        Yields:
            FileMetadata entries matching the prefix
        """
        accessible_int_ids: set[int] | None = kwargs.get("accessible_int_ids")

        if self._is_local:
            entries = self._local.list_metadata(prefix)
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
                # Filter by accessible_int_ids if provided
                if accessible_int_ids is not None:
                    if metadata.int_id is None or metadata.int_id not in accessible_int_ids:
                        continue
                yield metadata
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
        if self._has_engine:
            if hasattr(self._engine, "shutdown"):
                # RaftConsensus: gracefully stop gRPC server + transport loop
                self._engine.shutdown()
            else:
                # Metastore: just flush sled
                self._engine.flush()
        else:
            # Remote client close is async
            # Would need to handle this differently
            pass

    # =========================================================================
    # Revision Counter (Issue #1330 Phase 4.2)
    # =========================================================================

    def increment_revision(self, zone_id: str) -> int:
        """Atomically increment and return the new revision for a zone.

        Uses redb's dedicated REVISIONS_TABLE — no Python lock needed.

        Args:
            zone_id: The zone to increment revision for

        Returns:
            The new revision number after incrementing
        """
        if self._is_local:
            return self._local.increment_revision(zone_id)
        raise NotImplementedError("Remote revision counter requires async")

    def get_revision(self, zone_id: str) -> int:
        """Get the current revision for a zone without incrementing.

        Args:
            zone_id: The zone to get revision for

        Returns:
            The current revision number (0 if not found)
        """
        if self._is_local:
            return self._local.get_revision(zone_id)
        raise NotImplementedError("Remote revision counter requires async")

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
        """Store or update multiple file metadata entries atomically.

        All entries are serialized upfront to fail fast on bad data.
        If any write fails mid-batch, previously written entries are
        rolled back on a best-effort basis.

        Args:
            metadata_list: List of file metadata to store

        Raises:
            RuntimeError: If a write fails (includes details of partial progress)
        """
        if not metadata_list:
            return

        # Phase 1: serialize all entries upfront (fail fast before any writes)
        serialized: list[tuple[str, bytes]] = [
            (m.path, _serialize_metadata(m)) for m in metadata_list
        ]

        # Phase 2: apply all writes, tracking progress for rollback
        completed_paths: list[str] = []
        try:
            if self._has_engine:
                for path, data in serialized:
                    self._engine.set_metadata(path, data)
                    completed_paths.append(path)
            else:
                raise NotImplementedError(
                    "Remote mode requires async. Use put_batch_async() instead."
                )
        except NotImplementedError:
            raise
        except Exception as e:
            # Best-effort rollback: delete entries that were written
            for rollback_path in completed_paths:
                try:
                    self._engine.delete_metadata(rollback_path)
                except Exception:
                    logger.warning("put_batch rollback failed for path: %s", rollback_path)
            raise RuntimeError(
                f"put_batch failed after writing {len(completed_paths)}/{len(serialized)} "
                f"entries (rolled back {len(completed_paths)} writes): {e}"
            ) from e

    def delete_batch(self, paths: Sequence[str]) -> None:
        """Delete multiple file metadata entries atomically.

        Existing metadata is captured upfront so that previously deleted
        entries can be restored on a best-effort basis if a failure occurs
        mid-batch.

        Args:
            paths: List of virtual paths to delete

        Raises:
            RuntimeError: If a delete fails (includes details of partial progress)
        """
        if not paths:
            return

        # Phase 1: capture existing metadata for rollback
        snapshots: list[tuple[str, bytes | None]] = []
        if self._has_engine:
            for path in paths:
                existing_data = self._engine.get_metadata(path)
                snapshots.append((path, existing_data))
        else:
            raise NotImplementedError(
                "Remote mode requires async. Use delete_batch_async() instead."
            )

        # Phase 2: apply all deletes, tracking progress for rollback
        completed: list[tuple[str, bytes | None]] = []
        try:
            for path, existing_data in snapshots:
                self._engine.delete_metadata(path)
                completed.append((path, existing_data))
        except Exception as e:
            # Best-effort rollback: restore entries that were deleted
            for rollback_path, rollback_data in completed:
                if rollback_data is not None:
                    try:
                        self._engine.set_metadata(rollback_path, rollback_data)
                    except Exception:
                        logger.warning(
                            "delete_batch rollback failed for path: %s",
                            rollback_path,
                        )
            raise RuntimeError(
                f"delete_batch failed after deleting {len(completed)}/{len(paths)} "
                f"entries (rolled back {len(completed)} deletes): {e}"
            ) from e

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
        if self._has_engine:
            # Store in a separate namespace: "meta:{path}:{key}"
            meta_key = f"meta:{path}:{key}"
            if value is None:
                # Delete the key
                self._engine.delete_metadata(meta_key)
            else:
                # Store as JSON bytes
                data = json.dumps(value).encode("utf-8")
                self._engine.set_metadata(meta_key, data)
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
        if self._has_engine:
            meta_key = f"meta:{path}:{key}"
            data = self._engine.get_metadata(meta_key)
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
        if self._has_engine:
            result = self._engine.acquire_lock(
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
        if self._has_engine:
            return self._engine.release_lock(path, holder_id)
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
        if self._has_engine:
            return self._engine.extend_lock(path, holder_id, ttl_secs)
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
        if self._has_engine:
            lock_info = self._engine.get_lock(path)
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
        if self._has_engine:
            lock_infos = self._engine.list_locks(prefix, limit)
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
        if self._has_engine:
            return self._engine.force_release_lock(path)
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
        if self._has_engine:
            return self._get_engine(path)
        else:
            return await self._client.get_metadata(path, zone_id=self._zone_id)

    async def put_async(self, metadata: FileMetadata) -> None:
        """Store or update file metadata (async).

        Args:
            metadata: File metadata to store
        """
        if self._has_engine:
            self._put_engine(metadata)
        else:
            await self._client.put_metadata(metadata, zone_id=self._zone_id)

    async def delete_async(self, path: str) -> dict[str, Any] | None:
        """Delete file metadata (async).

        Args:
            path: Virtual path

        Returns:
            Dictionary with deleted file info or None
        """
        if self._has_engine:
            return self._delete_engine(path)
        else:
            existing = await self.get_async(path)
            await self._client.delete_metadata(path, zone_id=self._zone_id)
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
        if self._has_engine:
            return self._list_engine(prefix, recursive)
        else:
            return await self._client.list_metadata(
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
        if self._has_engine:
            result = self._engine.acquire_lock(
                path, holder_id, max_holders, ttl_secs, f"metadata:{holder_id}"
            )
            return result.acquired
        else:
            result = await self._client.acquire_lock(
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
        if self._has_engine:
            return self._engine.release_lock(path, holder_id)
        else:
            return await self._client.release_lock(
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
        if self._has_engine:
            return self._engine.extend_lock(path, holder_id, ttl_secs)
        else:
            return await self._client.extend_lock(
                lock_id=path,
                holder_id=holder_id,
                ttl_ms=ttl_secs * 1000,
                zone_id=self._zone_id,
            )

    async def close_async(self) -> None:
        """Close the metadata store and release resources (async)."""
        if self._has_engine:
            if hasattr(self._engine, "shutdown"):
                self._engine.shutdown()
            else:
                self._engine.flush()
        else:
            await self._client.close()
