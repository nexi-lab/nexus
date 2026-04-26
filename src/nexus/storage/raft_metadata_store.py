"""Raft-backed metadata store for Nexus.

This is the primary metadata storage for Nexus, using an embedded redb database
with optional Raft consensus for multi-node deployments.

Architecture:
    Without Raft: Python -> Metastore (PyO3) -> redb (~5μs)
    With Raft:    Python -> ZoneManager -> ZoneHandle (PyO3) -> Raft -> redb (~2-10ms)

Usage:
    # Without Raft (mode != 'federation'):
    store = RaftMetadataStore.embedded("/var/lib/nexus/metadata")

    # With Raft (mode='federation', via ZoneManager + ZoneHandle):
    import nexus_kernel
    kernel = nexus_kernel.Kernel()
    kernel.zone_create("root", ["2@peer:2126"])
    handle = mgr.create_zone("root", ["2@peer:2126"])
    store = RaftMetadataStore(engine=handle, zone_id="root")
"""

import builtins
import json
import logging
from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC

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
    from nexus.storage._metadata_mapper_generated import MetadataMapper

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
    from nexus.storage._metadata_mapper_generated import MetadataMapper

    # Handle both bytes and list of ints (from PyO3)
    if isinstance(data, list):
        data = bytes(data)

    if not data:
        raise ValueError("Cannot deserialize empty bytes")

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


class RaftMetadataStore(MetastoreABC):
    """Primary metadata store for Nexus using embedded redb database.

    This store provides fast local metadata operations (~5μs) with optional
    Raft consensus for multi-node replication. The engine is always a local
    PyO3 object — either a bare Metastore (no Raft) or a ZoneHandle (Raft-backed).

    Example:
        # Without Raft (mode != 'federation'):
        store = RaftMetadataStore.embedded("/var/lib/nexus/metadata")
        store.put(metadata)

        # With Raft (via kernel zone ops + ZoneHandle):
        import nexus_kernel
        kernel = nexus_kernel.Kernel()
        kernel.zone_create("root", ["2@peer:2126"])
        # Construct via embedded(); the kernel wires the per-zone handle
        # internally. Direct ZoneHandle construction is internal API.
        store = RaftMetadataStore.embedded("/var/lib/nexus/metadata")
        store.put(metadata)  # replicated
    """

    def __init__(
        self,
        engine: Any,
        zone_id: str | None = None,
    ):
        """Initialize RaftMetadataStore.

        Use the factory method ``embedded()`` or pass a ZoneHandle from
        ZoneManager directly as the engine parameter.

        Args:
            engine: Metastore or ZoneHandle instance (PyO3 FFI)
            zone_id: Zone ID for this store
        """
        super().__init__()
        if engine is None:
            raise ValueError("engine must be provided")

        self._engine = engine
        self._zone_id = zone_id

    def is_leader(self) -> bool:
        """Check if this node is the Raft leader for its zone.

        Returns:
            True if the embedded engine is a ZoneHandle and is the leader,
            False if not leader, no engine, or engine doesn't support leadership.
        """
        if self._engine is None:
            return False
        if not hasattr(self._engine, "is_leader"):
            return False
        return self._engine.is_leader()

    # =========================================================================
    # Batch Engine Helpers — DRY fallback for engines without batch methods.
    # ZoneHandle may not expose batch_set_metadata / batch_delete_metadata;
    # these helpers fall back to individual calls (Issue #3469).
    # =========================================================================

    def _engine_batch_set(self, items: list[tuple[str, bytes]]) -> int:
        """Batch-set metadata entries via the engine.

        Falls back to individual set_metadata() calls when the engine
        does not expose batch_set_metadata (e.g., ZoneHandle).
        """
        if hasattr(self._engine, "batch_set_metadata"):
            return self._engine.batch_set_metadata(items)
        for path, data in items:
            self._engine.set_metadata(path, data)
        return len(items)

    def _engine_batch_delete(self, keys: list[str]) -> int:
        """Batch-delete metadata entries via the engine.

        Falls back to individual delete_metadata() calls when the engine
        does not expose batch_delete_metadata (e.g., ZoneHandle).
        """
        if hasattr(self._engine, "batch_delete_metadata"):
            return self._engine.batch_delete_metadata(keys)
        for key in keys:
            self._engine.delete_metadata(key)
        return len(keys)

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

    def _put_engine(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        """Store metadata in the embedded sled engine.

        Args:
            metadata: File metadata to store
            consistency: "sc" (wait for Raft commit) or "ec" (fire-and-forget).

        Returns:
            EC mode: write token (int) for polling via is_committed().
            SC mode: None (write is already committed).
        """
        data = _serialize_metadata(metadata)
        try:
            return self._engine.set_metadata(metadata.path, data, consistency=consistency)
        except TypeError:
            # Compiled PyO3 binary may not yet support the consistency parameter
            return self._engine.set_metadata(metadata.path, data)

    def _delete_engine(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        """Delete metadata from the embedded sled engine.

        Args:
            path: Virtual path
            consistency: "sc" (wait for commit) or "ec" (fire-and-forget).
                         Only used when the engine is a ZoneHandle (consensus mode).

        Returns:
            Dictionary with deleted file info or None
        """
        existing = self._get_engine(path)
        try:
            self._engine.delete_metadata(path, consistency=consistency)
        except TypeError:
            # Compiled PyO3 binary may not yet support the consistency parameter
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
    ) -> list[FileMetadata]:
        """List metadata entries from the embedded sled engine.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files

        Returns:
            List of file metadata
        """
        entries = self._engine.list_metadata(prefix)
        result = []
        for path, data in entries:
            # Skip extended attribute keys (format: "meta:{path}:{key}")
            if path.startswith("meta:"):
                continue
            if not recursive:
                # Filter to direct children only — before expensive deserialization
                rel_path = path[len(prefix) :].lstrip("/")
                if "/" in rel_path:
                    continue
            metadata = _deserialize_metadata(data)
            result.append(metadata)
        return result

    @classmethod
    def embedded(cls, db_path: str, zone_id: str | None = None) -> "RaftMetadataStore":
        """Create an embedded metastore using direct sled access.

        This is the fast path (~5μs per operation) for standalone mode.

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

    def _get_raw(self, path: str) -> FileMetadata | None:
        """Get metadata for a file.

        Args:
            path: Virtual path

        Returns:
            FileMetadata if found, None otherwise
        """
        return self._get_engine(path)

    def _put_raw(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        """Store or update file metadata.

        Args:
            metadata: File metadata to store
            consistency: "sc" (wait for commit) or "ec" (fire-and-forget)

        Returns:
            EC mode: write token (int) for polling via is_committed().
            SC mode: None (write is already committed when this returns).
        """
        return self._put_engine(metadata, consistency=consistency)

    def is_committed(self, token: int) -> str | None:
        """Check if an EC write token has been replicated to a majority.

        Args:
            token: Write token returned by put() with consistency="ec".

        Returns:
            "committed" — replicated to majority.
            "pending" — local only, awaiting replication.
            None — invalid token or no replication log.
        """
        return self._engine.is_committed(token)

    def _delete_raw(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        """Delete file metadata.

        Args:
            path: Virtual path
            consistency: "sc" (wait for commit) or "ec" (fire-and-forget)

        Returns:
            Dictionary with deleted file info or None
        """
        return self._delete_engine(path, consistency=consistency)

    def _exists_raw(self, path: str) -> bool:
        """Check if metadata exists for a path.

        Args:
            path: Virtual path

        Returns:
            True if metadata exists, False otherwise
        """
        return self._get_raw(path) is not None

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
        entries = self._engine.list_metadata(prefix)
        return len(entries) > 0

    def _list_raw(
        self,
        prefix: str = "",
        recursive: bool = True,
        zone_id: str | None = None,
        accessible_int_ids: set[int] | None = None,
        **kwargs: Any,
    ) -> list[FileMetadata]:
        """List all files with given path prefix.

        RaftMetadataStore is zone-local: each zone has its own sled database,
        so zone_id filtering is inherent to the store instance.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files
            zone_id: Accepted for API consistency (filtering is inherent)
            accessible_int_ids: Deprecated — filtering moved to service layer

        Returns:
            List of file metadata
        """
        # RaftMetadataStore is zone-local: each zone has its own sled database.
        # zone_id parameter accepted for API consistency but filtering is inherent.
        # RaftMetadataStore is zone-local. Non-zoned stores serve the "root" zone,
        # so zone_id="root" is always allowed. Only assert on specific zone_ids.
        # When prefix is already zone-scoped (e.g. /zone/corp/...), zone_id is
        # redundant — path prefix already constrains to the zone namespace.
        if zone_id is not None and zone_id != ROOT_ZONE_ID:
            if self._zone_id is None and not prefix.startswith(f"/zone/{zone_id}"):
                raise ValueError(f"zone_id filter '{zone_id}' passed to a non-zone-scoped store")
        return self._list_engine(prefix, recursive)

    def _list_iter_raw(
        self,
        prefix: str = "",
        recursive: bool = True,
        zone_id: str | None = None,
        **kwargs: Any,
    ) -> Iterator[FileMetadata]:
        """Iterate over file metadata matching prefix.

        RaftMetadataStore is zone-local: each zone has its own sled database,
        so zone_id filtering is inherent to the store instance.

        Memory-efficient alternative to list(). Yields results one at a time
        instead of materializing the full result list.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files
            zone_id: Accepted for API consistency (filtering is inherent)
            **kwargs: Additional keyword arguments (ignored)

        Yields:
            FileMetadata entries matching the prefix
        """
        # Issue #3706: same zone-scope validation as _list_raw
        if zone_id is not None and zone_id != ROOT_ZONE_ID:
            if self._zone_id is None and not prefix.startswith(f"/zone/{zone_id}"):
                raise ValueError(f"zone_id filter '{zone_id}' passed to a non-zone-scoped store")
        entries = self._engine.list_metadata(prefix)
        for path, data in entries:
            # Skip extended attribute keys (format: "meta:{path}:{key}")
            if path.startswith("meta:"):
                continue
            # Apply path-based filters BEFORE expensive deserialization
            if not recursive:
                rel_path = path[len(prefix) :].lstrip("/")
                if "/" in rel_path:
                    continue
            metadata = _deserialize_metadata(data)
            yield metadata

    def close(self) -> None:
        """Close the metadata store and release resources.

        Explicitly deletes the engine reference to trigger Rust Drop,
        which releases the redb file lock. Without this, Python's
        non-deterministic GC may hold the lock, causing 'Directory not
        empty' errors when cleaning up temp directories in tests.
        """
        if hasattr(self._engine, "shutdown"):
            # ZoneManager: gracefully stop gRPC server + transport loop
            self._engine.shutdown()
        elif hasattr(self._engine, "flush"):
            # Metastore: flush redb to disk
            self._engine.flush()
        # ZoneHandle: no explicit teardown needed (managed by ZoneManager)

        # Release redb file lock by dropping all Rust references.
        # PyMetastore holds Arc<Database> in store + state machine trees.
        # Setting to None drops PyMetastore → Rust Drop → flock release.
        # gc.collect() handles any circular refs preventing immediate drop.
        import gc
        import time

        self._engine = None
        gc.collect()
        # Brief yield to allow OS to release flock after Rust Drop.
        # Prevents rare "Directory not empty" race during temp dir cleanup.
        time.sleep(0.01)

    # =========================================================================
    # Zone-level reserved keys (federation ref counting)
    # =========================================================================

    _KEY_LINKS_COUNT = "__i_links_count__"

    def get_zone_links_count(self) -> int:
        """Get the zone's i_links_count (number of DT_MOUNT references).

        Uses the __i_links_count__ reserved key in Raft metadata,
        same SetMetadata command as regular metadata — no Rust changes needed.

        Returns:
            Current link count (0 if not set).
        """
        data = self._engine.get_metadata(self._KEY_LINKS_COUNT)
        if data is None:
            return 0
        if isinstance(data, list):
            data = bytes(data)
        return int.from_bytes(data, "big")

    def set_zone_links_count(self, count: int) -> None:
        """Set the zone's i_links_count (Raft-replicated).

        Must be called on the leader node. Uses the same SetMetadata
        Raft command as regular file metadata.

        Args:
            count: New link count value.
        """
        self._engine.set_metadata(self._KEY_LINKS_COUNT, count.to_bytes(8, "big"))

    def adjust_zone_links_count(self, delta: int) -> int:
        """Atomically adjust the zone's i_links_count by delta.

        Uses AdjustCounter Raft command — read-modify-write happens
        in apply(), serialized by Raft. No lost updates under concurrency.

        Args:
            delta: Signed adjustment (+1 to increment, -1 to decrement).

        Returns:
            New count after adjustment.
        """
        return self._engine.adjust_counter(self._KEY_LINKS_COUNT, delta)

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
        return self._engine.increment_revision(zone_id)

    def get_revision(self, zone_id: str) -> int:
        """Get the current revision for a zone without incrementing.

        Args:
            zone_id: The zone to get revision for

        Returns:
            The current revision number (0 if not found)
        """
        return self._engine.get_revision(zone_id)

    # =========================================================================
    # Batch Operations
    # =========================================================================

    def _get_batch_raw(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        """Get metadata for multiple files.

        Uses native get_metadata_multi when available (single FFI call)
        instead of N individual _get_raw() calls.

        Args:
            paths: List of virtual paths

        Returns:
            Dictionary mapping path to FileMetadata (or None if not found)
        """
        if not paths:
            return {}

        if hasattr(self._engine, "get_metadata_multi"):
            results = self._engine.get_metadata_multi(list(paths))
            return {
                path: _deserialize_metadata(data) if data is not None else None
                for path, data in results
            }
        # Fallback: individual calls when batch API not yet compiled
        return {path: self._get_raw(path) for path in paths}

    def _put_batch_raw(
        self,
        metadata_list: Sequence[FileMetadata],
        *,
        consistency: str = "sc",  # noqa: ARG002
        skip_snapshot: bool = False,
    ) -> None:
        """Store or update multiple file metadata entries atomically.

        All entries are serialized upfront to fail fast on bad data.
        If any write fails mid-batch, previously written entries are
        rolled back on a best-effort basis (unless skip_snapshot=True).

        Args:
            metadata_list: List of file metadata to store
            consistency: Consistency mode (see put() for details).
            skip_snapshot: Skip pre-write snapshot for rollback.

        Raises:
            RuntimeError: If a write fails (includes details of partial progress)
        """
        if not metadata_list:
            return

        # Phase 1: serialize all entries upfront (fail fast before any writes)
        serialized: list[tuple[str, bytes]] = [
            (m.path, _serialize_metadata(m)) for m in metadata_list
        ]

        # Phase 2: capture existing metadata for rollback (single FFI call)
        # Skipped when caller manages its own retry logic (e.g., deferred buffer flush)
        snapshots: list[tuple[str, bytes | None]] | None = None
        if not skip_snapshot:
            path_list = [path for path, _ in serialized]
            if hasattr(self._engine, "get_metadata_multi"):
                snapshots = self._engine.get_metadata_multi(path_list)
            else:
                snapshots = [(p, self._engine.get_metadata(p)) for p in path_list]

        # Phase 3: apply all writes (single FFI call or fallback loop)
        try:
            self._engine_batch_set(serialized)
        except Exception as e:
            # Best-effort rollback (only when snapshots were captured)
            if snapshots is not None:
                restore_items = [(path, data) for path, data in snapshots if data is not None]
                delete_paths = [path for path, data in snapshots if data is None]
                try:
                    if restore_items:
                        self._engine_batch_set(restore_items)
                    if delete_paths:
                        self._engine_batch_delete(delete_paths)
                except Exception:
                    logger.warning("put_batch rollback failed for %d paths", len(serialized))
            raise RuntimeError(f"put_batch failed writing {len(serialized)} entries: {e}") from e

    def _delete_batch_raw(self, paths: Sequence[str]) -> None:
        """Delete multiple file metadata entries atomically.

        Uses batch FFI calls: get_metadata_multi (1 call) for rollback snapshots
        and batch_delete_metadata (1 call) for deletes.

        Args:
            paths: List of virtual paths to delete

        Raises:
            RuntimeError: If a delete fails (includes details of partial progress)
        """
        if not paths:
            return

        path_list = list(paths)

        # Phase 1: capture existing metadata for rollback (single FFI call)
        if hasattr(self._engine, "get_metadata_multi"):
            snapshots = self._engine.get_metadata_multi(path_list)
        else:
            snapshots = [(p, self._engine.get_metadata(p)) for p in path_list]

        # Phase 2: apply all deletes (single FFI call or fallback loop)
        try:
            self._engine_batch_delete(path_list)
        except Exception as e:
            # Best-effort rollback: restore entries that were deleted
            rollback_items = [(path, data) for path, data in snapshots if data is not None]
            if rollback_items:
                try:
                    self._engine_batch_set(rollback_items)
                except Exception:
                    logger.warning(
                        "delete_batch rollback failed for %d paths",
                        len(rollback_items),
                    )
            raise RuntimeError(f"delete_batch failed deleting {len(path_list)} entries: {e}") from e

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        """Get content IDs (hashes) for multiple paths.

        Uses get_batch() for a single FFI call instead of N individual calls.

        Args:
            paths: List of virtual paths

        Returns:
            Dictionary mapping path to content_hash (or None if not found)
        """
        if not paths:
            return {}
        batch = self.get_batch(paths)
        return {path: (meta.etag if meta else None) for path, meta in batch.items()}

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
        # Store in a separate namespace: "meta:{path}:{key}"
        meta_key = f"meta:{path}:{key}"
        if value is None:
            # Delete the key
            self._engine.delete_metadata(meta_key)
        else:
            # Store as JSON bytes
            data = json.dumps(value).encode("utf-8")
            self._engine.set_metadata(meta_key, data)

    def get_file_metadata(self, path: str, key: str) -> Any:
        """Get custom metadata value for a file.

        Args:
            path: Virtual file path
            key: Metadata key

        Returns:
            Metadata value or None if not found
        """
        meta_key = f"meta:{path}:{key}"
        data = self._engine.get_metadata(meta_key)
        if data is None:
            return None
        # Handle both bytes and list of ints (from PyO3)
        if isinstance(data, list):
            data = bytes(data)
        return json.loads(data.decode("utf-8"))

    def get_file_metadata_bulk(self, paths: Sequence[str], key: str) -> dict[str, Any]:
        """Get custom metadata value for multiple files.

        Uses get_metadata_multi for a single FFI call instead of N individual calls.

        Args:
            paths: List of virtual file paths
            key: Metadata key

        Returns:
            Dictionary mapping path to value (or None if not found)
        """
        if not paths:
            return {}
        # Build meta keys and batch-fetch in one FFI call
        meta_keys = [f"meta:{path}:{key}" for path in paths]
        if hasattr(self._engine, "get_metadata_multi"):
            results = self._engine.get_metadata_multi(meta_keys)
        else:
            results = [(k, self._engine.get_metadata(k)) for k in meta_keys]
        out: dict[str, Any] = {}
        for (_meta_key, data), path in zip(results, paths, strict=True):
            if data is None:
                out[path] = None
            else:
                raw = bytes(data) if isinstance(data, list) else data
                out[path] = json.loads(raw.decode("utf-8"))
        return out

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

        Uses get_file_metadata_bulk for a single FFI call instead of N individual calls.

        Args:
            paths: List of virtual file paths

        Returns:
            Dictionary mapping path to text (only includes paths with cached text)
        """
        if not paths:
            return {}
        bulk = self.get_file_metadata_bulk(paths, "parsed_text")
        return {path: text for path, text in bulk.items() if text is not None}

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
        result = self._engine.acquire_lock(
            path, holder_id, max_holders, ttl_secs, f"metadata:{holder_id}"
        )
        return result.acquired

    def release_lock(self, path: str, holder_id: str) -> bool:
        """Release a distributed lock.

        Args:
            path: Resource path
            holder_id: Holder identifier

        Returns:
            True if holder was found and released, False if not owned
        """
        return self._engine.release_lock(path, holder_id)

    def extend_lock(self, path: str, holder_id: str, ttl_secs: int = 30) -> bool:
        """Extend a lock's TTL (heartbeat).

        Args:
            path: Resource path
            holder_id: Holder identifier
            ttl_secs: New TTL in seconds

        Returns:
            True if holder was found and TTL extended, False if not owned
        """
        return self._engine.extend_lock(path, holder_id, ttl_secs)

    def get_lock_info(self, path: str) -> dict[str, Any] | None:
        """Get lock information for a path.

        Args:
            path: Lock key (typically "zone_id:resource_path")

        Returns:
            Dict with lock info if lock exists and has holders, None otherwise.
            Dict keys: path, max_holders, holders (list of holder dicts)
        """
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

    def list_locks(self, prefix: str = "", limit: int = 1000) -> builtins.list[dict[str, Any]]:
        """List all active locks matching a prefix.

        Args:
            prefix: Key prefix to filter by (e.g., "zone_id:" for zone-scoped)
            limit: Maximum number of results

        Returns:
            List of lock info dicts
        """
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

    def force_release_lock(self, path: str) -> bool:
        """Force-release all holders of a lock (admin operation).

        Args:
            path: Lock key to force-release

        Returns:
            True if a lock was found and released, False if no lock exists
        """
        return self._engine.force_release_lock(path)

    # =========================================================================
    # Async wrappers (sync engine, async interface for caller convenience)
    # =========================================================================

    async def get_async(self, path: str) -> FileMetadata | None:
        """Get metadata for a file (async wrapper over sync engine)."""
        return self._get_engine(path)

    async def put_async(self, metadata: FileMetadata, *, consistency: str = "sc") -> None:
        """Store or update file metadata (async wrapper over sync engine)."""
        self._put_engine(metadata, consistency=consistency)

    async def delete_async(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        """Delete file metadata (async).

        Args:
            path: Virtual path
            consistency: "sc" (wait for commit) or "ec" (fire-and-forget)

        Returns:
            Dictionary with deleted file info or None
        """
        return self._delete_engine(path, consistency=consistency)

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
    ) -> builtins.list[FileMetadata]:
        """List all files with given path prefix (async).

        RaftMetadataStore is zone-local: each zone has its own sled database,
        so zone_id filtering is inherent to the store instance.

        Args:
            prefix: Path prefix to filter by
            recursive: If True, include all nested files
            zone_id: Accepted for API consistency (filtering is inherent)

        Returns:
            List of file metadata
        """
        # RaftMetadataStore is zone-local: zone_id accepted for API consistency.
        # When prefix is already zone-scoped, zone_id is redundant.
        if zone_id is not None and zone_id != ROOT_ZONE_ID:
            if self._zone_id is None and not prefix.startswith(f"/zone/{zone_id}"):
                raise ValueError(f"zone_id filter '{zone_id}' passed to a non-zone-scoped store")
        return self._list_engine(prefix, recursive)

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
        result = self._engine.acquire_lock(
            path, holder_id, max_holders, ttl_secs, f"metadata:{holder_id}"
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
        return self._engine.release_lock(path, holder_id)

    async def extend_lock_async(self, path: str, holder_id: str, ttl_secs: int = 30) -> bool:
        """Extend a lock's TTL (async).

        Args:
            path: Resource path
            holder_id: Holder identifier
            ttl_secs: New TTL in seconds

        Returns:
            True if holder was found and TTL extended, False if not owned
        """
        return self._engine.extend_lock(path, holder_id, ttl_secs)

    async def close_async(self) -> None:
        """Close the metadata store and release resources (async)."""
        self.close()
