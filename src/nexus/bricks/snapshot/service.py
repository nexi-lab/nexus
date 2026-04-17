"""Transactional snapshot service implementation (Issue #1752).

Provides begin/commit/rollback semantics for filesystem operations.
Uses CAS ref-count holds (near-zero I/O) for COW strategy and
an in-memory registry for fast-path O(1) lookups.

Architecture:
    - TransactionRegistry: in-memory fast-path (zero cost when no txns active)
    - CASAddressingEngine.hold_reference(): prevents GC of pre-modification content
    - DB: TransactionSnapshotModel + SnapshotEntryModel for durability
    - Conflict detection: MVCC at commit time via hash comparison

Follows VersionService pattern (DI constructor, asyncio.to_thread for DB ops).
"""

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

from nexus.bricks.snapshot.errors import (
    TransactionConflictError,
    TransactionNotActiveError,
    TransactionNotFoundError,
)
from nexus.bricks.snapshot.registry import TransactionRegistry
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.protocols.snapshot import (
    ConflictInfo,
    SnapshotEntry,
    TransactionInfo,
)

logger = logging.getLogger(__name__)

# Re-export errors for backward compatibility
__all__ = [
    "TransactionConflictError",
    "TransactionNotActiveError",
    "TransactionNotFoundError",
    "TransactionalSnapshotService",
]


def _model_to_info(model: Any) -> TransactionInfo:
    """Convert a TransactionSnapshotModel to a TransactionInfo dataclass."""
    return TransactionInfo(
        transaction_id=model.transaction_id,
        zone_id=model.zone_id,
        agent_id=model.agent_id,
        status=model.status,
        description=model.description,
        created_at=model.created_at,
        expires_at=model.expires_at,
        entry_count=model.entry_count,
    )


def _model_to_entry(model: Any) -> SnapshotEntry:
    """Convert a SnapshotEntryModel to a SnapshotEntry dataclass."""
    return SnapshotEntry(
        entry_id=model.entry_id,
        transaction_id=model.transaction_id,
        path=model.path,
        operation=model.operation,
        original_hash=model.original_hash,
        original_metadata=model.original_metadata,
        new_hash=model.new_hash,
        created_at=model.created_at,
    )


class TransactionalSnapshotService:
    """Manages transactional filesystem snapshots.

    Lifecycle: begin() -> track_write()/track_delete() -> commit()/rollback()

    Thread safety:
        - is_tracked() and track_*() are synchronous (called from sync write path)
        - begin(), commit(), rollback() are async (DB operations via asyncio.to_thread)
        - TransactionRegistry provides thread-safe O(1) lookups
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        cas_store: Any,
        metadata_store: Any,
        metadata_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Initialize the snapshot service.

        Args:
            record_store: RecordStoreABC for DB persistence.
            cas_store: CASAddressingEngine for hold_reference/release.
            metadata_store: MetastoreABC for reading current file state.
            metadata_factory: Callable to construct FileMetadata-like objects
                (injected by factory.py to avoid importing nexus.contracts.metadata).
        """
        self._session_factory = record_store.session_factory
        self._cas_store = cas_store
        self._metadata_store = metadata_store
        self._metadata_factory = metadata_factory
        self._registry = TransactionRegistry()

    @property
    def registry(self) -> TransactionRegistry:
        """Expose registry for metrics/testing."""
        return self._registry

    # ------------------------------------------------------------------
    # Sync hot-path methods (called from nexus_fs write/delete)
    # ------------------------------------------------------------------

    def is_tracked(self, path: str) -> str | None:
        """Check if a path is tracked by an active transaction.

        Fast-path: has_active_transactions() early exit.
        Returns transaction_id or None.
        """
        if not self._registry.has_active_transactions():
            return None
        return self._registry.get_transaction_for_path(path)

    def track_write(
        self,
        transaction_id: str,
        path: str,
        original_hash: str | None,
        original_metadata: dict[str, Any] | None,
        new_hash: str | None,
    ) -> None:
        """Track a write operation within a transaction (sync).

        Called from the write path after content is written to CAS.
        Holds a CAS reference to the original content to prevent GC.
        """
        self._track_operation(
            transaction_id=transaction_id,
            path=path,
            operation="write",
            original_hash=original_hash,
            original_metadata=original_metadata,
            new_hash=new_hash,
        )

    def track_delete(
        self,
        transaction_id: str,
        path: str,
        original_hash: str | None,
        original_metadata: dict[str, Any] | None,
    ) -> None:
        """Track a delete operation within a transaction (sync).

        Called from the delete path before content is removed.
        Holds a CAS reference to the original content for rollback.
        """
        self._track_operation(
            transaction_id=transaction_id,
            path=path,
            operation="delete",
            original_hash=original_hash,
            original_metadata=original_metadata,
            new_hash=None,
        )

    def validate_path_available(self, transaction_id: str, path: str) -> None:
        """Check that *path* can be tracked by *transaction_id*.

        Call this **before** performing the filesystem mutation so that a
        conflict is detected before the write/delete occurs.

        Raises:
            TransactionConflictError: if *path* is owned by another transaction.
        """
        if not self._registry.has_active_transactions():
            return
        existing_txn = self._registry.get_transaction_for_path(path)
        if existing_txn is not None and existing_txn != transaction_id:
            raise TransactionConflictError(
                conflicts=[
                    ConflictInfo(
                        path=path,
                        expected_hash=None,
                        current_hash=None,
                        reason=f"Path already tracked by transaction {existing_txn}",
                    )
                ]
            )

    def _track_operation(
        self,
        transaction_id: str,
        path: str,
        operation: str,
        original_hash: str | None,
        original_metadata: dict[str, Any] | None,
        new_hash: str | None,
    ) -> None:
        """Internal: track a filesystem operation within a transaction."""
        # Hold CAS reference to prevent GC of original content
        if original_hash is not None and hasattr(self._cas_store, "hold_reference"):
            held = self._cas_store.hold_reference(original_hash)
            if not held:
                logger.warning(
                    "CAS hold_reference failed for hash=%s path=%s txn=%s",
                    original_hash,
                    path,
                    transaction_id,
                )

        # Register path in memory registry
        tracked = self._registry.track_path(transaction_id, path)
        if not tracked:
            # Release the hold we just acquired since we can't track
            if original_hash is not None and hasattr(self._cas_store, "release"):
                self._cas_store.release(original_hash)
            raise TransactionConflictError(
                conflicts=[
                    ConflictInfo(
                        path=path,
                        expected_hash=original_hash,
                        current_hash=new_hash,
                        reason=f"Path already tracked by a different transaction (txn={transaction_id})",
                    )
                ]
            )

        # Persist entry to DB
        metadata_json = json.dumps(original_metadata) if original_metadata else None
        try:
            self._persist_entry(
                transaction_id=transaction_id,
                path=path,
                operation=operation,
                original_hash=original_hash,
                original_metadata=metadata_json,
                new_hash=new_hash,
            )
        except Exception:
            logger.exception(
                "Failed to persist snapshot entry for path=%s txn=%s",
                path,
                transaction_id,
            )
            # CAS blob cleanup deferred to reachability GC — no release needed.
            raise

    def _persist_entry(
        self,
        transaction_id: str,
        path: str,
        operation: str,
        original_hash: str | None,
        original_metadata: str | None,
        new_hash: str | None,
    ) -> None:
        """Persist a SnapshotEntryModel to DB (synchronous)."""
        from nexus.storage.models.transaction_snapshot import SnapshotEntryModel

        with self._session_factory() as session:
            entry = SnapshotEntryModel(
                entry_id=str(uuid.uuid4()),
                transaction_id=transaction_id,
                path=path,
                operation=operation,
                original_hash=original_hash,
                original_metadata=original_metadata,
                new_hash=new_hash,
                created_at=datetime.now(UTC),
            )
            session.add(entry)

            # Increment entry_count on parent transaction
            from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel

            txn = session.get(TransactionSnapshotModel, transaction_id)
            if txn is not None:
                txn.entry_count = (txn.entry_count or 0) + 1

            session.commit()

    # ------------------------------------------------------------------
    # Async methods (DB-backed operations)
    # ------------------------------------------------------------------

    async def begin(
        self,
        zone_id: str,
        agent_id: str | None = None,
        description: str | None = None,
        ttl_seconds: int = 3600,
    ) -> TransactionInfo:
        """Begin a new transaction.

        Creates a DB record with status="active" and registers
        the transaction in the in-memory registry.
        """
        import asyncio

        from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel

        now = datetime.now(UTC)
        txn_id = str(uuid.uuid4())

        def _create() -> TransactionSnapshotModel:
            with self._session_factory() as session:
                model = TransactionSnapshotModel(
                    transaction_id=txn_id,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    status="active",
                    description=description,
                    created_at=now,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                    entry_count=0,
                )
                session.add(model)
                session.commit()
                session.refresh(model)
                return model

        model = await asyncio.to_thread(_create)
        self._registry.register(txn_id)

        logger.info(
            "Transaction started: txn=%s zone=%s agent=%s ttl=%ds",
            txn_id,
            zone_id,
            agent_id,
            ttl_seconds,
        )

        return _model_to_info(model)

    async def commit(self, transaction_id: str) -> TransactionInfo:
        """Commit a transaction after conflict check.

        Checks each entry's new_hash against the current file state.
        If any file was modified by another writer since tracking,
        raises TransactionConflictError.

        On success: releases CAS holds, updates status, unregisters.
        """
        import asyncio

        # Load transaction and entries
        txn_model, entries = await asyncio.to_thread(
            self._load_transaction_with_entries, transaction_id
        )

        if txn_model.status != "active":
            raise TransactionNotActiveError(transaction_id, txn_model.status)

        # Conflict detection: compare new_hash vs current metadata etag
        conflicts: list[ConflictInfo] = []
        for entry in entries:
            if entry.new_hash is not None:
                current_meta = self._metadata_store.get(entry.path)
                current_hash = current_meta.etag if current_meta else None
                if current_hash != entry.new_hash:
                    conflicts.append(
                        ConflictInfo(
                            path=entry.path,
                            expected_hash=entry.new_hash,
                            current_hash=current_hash,
                            reason="File modified by another writer since tracking",
                        )
                    )

        if conflicts:
            raise TransactionConflictError(conflicts)

        # Release CAS holds for original content
        for entry in entries:
            if entry.original_hash is not None:
                try:
                    self._cas_store.release(entry.original_hash)
                except Exception:
                    logger.warning(
                        "Failed to release CAS hold for hash=%s txn=%s",
                        entry.original_hash,
                        transaction_id,
                    )

        # Update DB status
        def _update_status() -> Any:
            from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel

            with self._session_factory() as session:
                model = session.get(TransactionSnapshotModel, transaction_id)
                if model is not None:
                    model.status = "committed"
                    model.completed_at = datetime.now(UTC)
                    session.commit()
                    session.refresh(model)
                return model

        updated = await asyncio.to_thread(_update_status)
        self._registry.unregister(transaction_id)

        logger.info("Transaction committed: txn=%s entries=%d", transaction_id, len(entries))
        return _model_to_info(updated)

    def _restore_metadata_from_snapshot(
        self, path: str, original_hash: str, metadata_json: str
    ) -> Any:
        """Build a FileMetadata from a JSON snapshot (used during rollback).

        Uses the injected ``metadata_factory`` to avoid importing ``nexus.contracts.metadata``
        directly (LEGO Architecture Principle 3: bricks don't import from kernel).
        """
        if self._metadata_factory is None:
            msg = "metadata_factory not set — cannot restore metadata during rollback"
            raise RuntimeError(msg)

        meta_dict = json.loads(metadata_json)
        return self._metadata_factory(
            path=path,
            backend_name=meta_dict.get("backend_name", "local"),
            physical_path=original_hash,
            size=meta_dict.get("size", 0),
            etag=original_hash,
            created_at=datetime.fromisoformat(meta_dict["created_at"])
            if meta_dict.get("created_at")
            else datetime.now(UTC),
            modified_at=datetime.fromisoformat(meta_dict["modified_at"])
            if meta_dict.get("modified_at")
            else datetime.now(UTC),
            version=meta_dict.get("version", 1),
            zone_id=meta_dict.get("zone_id", ROOT_ZONE_ID),
            owner_id=meta_dict.get("owner_id"),
        )

    async def rollback(self, transaction_id: str) -> TransactionInfo:
        """Rollback a transaction by restoring all files to pre-transaction state.

        Processes entries in reverse order (LIFO) to handle dependent operations.
        For each entry:
        - write: restore original metadata (content still in CAS via held ref)
        - delete: restore file metadata from snapshot

        After restoration: releases CAS holds, updates status, unregisters.
        """
        import asyncio

        txn_model, entries = await asyncio.to_thread(
            self._load_transaction_with_entries, transaction_id
        )

        if txn_model.status != "active":
            raise TransactionNotActiveError(transaction_id, txn_model.status)

        # Process entries in reverse order (LIFO)
        for entry in reversed(entries):
            try:
                if entry.original_hash is not None and entry.original_metadata is not None:
                    # Restore file to pre-transaction state (full metadata available)
                    restored = self._restore_metadata_from_snapshot(
                        entry.path, entry.original_hash, entry.original_metadata
                    )
                    self._metadata_store.put(restored)
                    logger.debug("Restored file: path=%s hash=%s", entry.path, entry.original_hash)
                elif entry.original_hash is not None and entry.original_metadata is None:
                    # Restore with minimal metadata (original_metadata was not captured)
                    if self._metadata_factory is not None:
                        current_meta = self._metadata_store.get(entry.path)
                        restored = self._metadata_factory(
                            path=entry.path,
                            backend_name=getattr(current_meta, "backend_name", "local")
                            if current_meta
                            else "local",
                            physical_path=entry.original_hash,
                            size=getattr(current_meta, "size", 0) if current_meta else 0,
                            etag=entry.original_hash,
                            created_at=getattr(current_meta, "created_at", None)
                            or datetime.now(UTC),
                            modified_at=datetime.now(UTC),
                            version=(getattr(current_meta, "version", 1) if current_meta else 1),
                            zone_id=getattr(current_meta, "zone_id", ROOT_ZONE_ID)
                            if current_meta
                            else "root",
                            owner_id=getattr(current_meta, "owner_id", None)
                            if current_meta
                            else None,
                        )
                        self._metadata_store.put(restored)
                        logger.debug(
                            "Restored file (minimal): path=%s hash=%s",
                            entry.path,
                            entry.original_hash,
                        )
                elif entry.operation == "write" and entry.original_hash is None:
                    # New file created during transaction — delete it
                    try:
                        self._metadata_store.delete(entry.path)
                        if entry.new_hash:
                            self._cas_store.release(entry.new_hash)
                    except Exception:
                        logger.warning("Failed to delete new file during rollback: %s", entry.path)
                elif entry.operation == "delete" and entry.original_hash is not None:
                    # File was deleted — restore from snapshot
                    if entry.original_metadata:
                        restored = self._restore_metadata_from_snapshot(
                            entry.path, entry.original_hash, entry.original_metadata
                        )
                        self._metadata_store.put(restored)
                        logger.debug(
                            "Restored deleted file: path=%s hash=%s",
                            entry.path,
                            entry.original_hash,
                        )
            except Exception:
                logger.exception(
                    "Failed to rollback entry: path=%s txn=%s", entry.path, transaction_id
                )

        # Release CAS holds (original content no longer needs protection)
        for entry in entries:
            if entry.original_hash is not None:
                try:
                    self._cas_store.release(entry.original_hash)
                except Exception:
                    logger.warning(
                        "Failed to release CAS hold during rollback: hash=%s txn=%s",
                        entry.original_hash,
                        transaction_id,
                    )

        # Update DB status
        def _update_status() -> Any:
            from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel

            with self._session_factory() as session:
                model = session.get(TransactionSnapshotModel, transaction_id)
                if model is not None:
                    model.status = "rolled_back"
                    model.completed_at = datetime.now(UTC)
                    session.commit()
                    session.refresh(model)
                return model

        updated = await asyncio.to_thread(_update_status)
        self._registry.unregister(transaction_id)

        logger.info("Transaction rolled back: txn=%s entries=%d", transaction_id, len(entries))
        return _model_to_info(updated)

    async def get_transaction(self, transaction_id: str) -> TransactionInfo | None:
        """Get transaction details by ID."""
        import asyncio

        def _get() -> Any:
            from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel

            with self._session_factory() as session:
                return session.get(TransactionSnapshotModel, transaction_id)

        model = await asyncio.to_thread(_get)
        return _model_to_info(model) if model is not None else None

    async def list_transactions(
        self,
        zone_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[TransactionInfo]:
        """List transactions for a zone, optionally filtered by status."""
        import asyncio

        def _list() -> list[Any]:
            from sqlalchemy import select

            from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel

            with self._session_factory() as session:
                stmt = (
                    select(TransactionSnapshotModel)
                    .where(TransactionSnapshotModel.zone_id == zone_id)
                    .order_by(TransactionSnapshotModel.created_at.desc())
                    .limit(limit)
                )
                if status is not None:
                    stmt = stmt.where(TransactionSnapshotModel.status == status)
                return list(session.execute(stmt).scalars().all())

        models = await asyncio.to_thread(_list)
        return [_model_to_info(m) for m in models]

    async def list_entries(self, transaction_id: str) -> list[SnapshotEntry]:
        """List all entries for a transaction."""
        import asyncio

        def _list() -> list[Any]:
            from sqlalchemy import select

            from nexus.storage.models.transaction_snapshot import SnapshotEntryModel

            with self._session_factory() as session:
                stmt = (
                    select(SnapshotEntryModel)
                    .where(SnapshotEntryModel.transaction_id == transaction_id)
                    .order_by(SnapshotEntryModel.created_at.asc())
                )
                return list(session.execute(stmt).scalars().all())

        models = await asyncio.to_thread(_list)
        return [_model_to_entry(m) for m in models]

    async def cleanup_expired(self, limit: int = 100) -> int:
        """Rollback and expire transactions past their TTL.

        Returns the number of transactions cleaned up.
        """
        import asyncio

        def _find_expired() -> list[str]:
            from sqlalchemy import select

            from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel

            now = datetime.now(UTC)
            with self._session_factory() as session:
                stmt = (
                    select(TransactionSnapshotModel.transaction_id)
                    .where(TransactionSnapshotModel.status == "active")
                    .where(TransactionSnapshotModel.expires_at <= now)
                    .limit(limit)
                )
                return list(session.execute(stmt).scalars().all())

        expired_ids = await asyncio.to_thread(_find_expired)

        cleaned = 0
        for txn_id in expired_ids:
            try:
                await self.rollback(txn_id)

                # Mark as expired (not just rolled_back)
                def _mark_expired(tid: str = txn_id) -> None:
                    from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel

                    with self._session_factory() as session:
                        model = session.get(TransactionSnapshotModel, tid)
                        if model is not None:
                            model.status = "expired"
                            session.commit()

                await asyncio.to_thread(_mark_expired)
                cleaned += 1
                # Yield control between batches
                await asyncio.sleep(0)
            except TransactionNotActiveError:
                # Already processed (race with manual rollback)
                cleaned += 1
            except Exception:
                logger.exception("Failed to cleanup expired transaction: %s", txn_id)

        if cleaned > 0:
            logger.info("Cleaned up %d expired transactions", cleaned)

        return cleaned

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_transaction_with_entries(
        self, transaction_id: str
    ) -> tuple[Any, list[SnapshotEntry]]:
        """Load transaction model and its entries (synchronous, for asyncio.to_thread)."""
        from sqlalchemy import select

        from nexus.storage.models.transaction_snapshot import (
            SnapshotEntryModel,
            TransactionSnapshotModel,
        )

        with self._session_factory() as session:
            txn = session.get(TransactionSnapshotModel, transaction_id)
            if txn is None:
                raise TransactionNotFoundError(transaction_id)

            stmt = (
                select(SnapshotEntryModel)
                .where(SnapshotEntryModel.transaction_id == transaction_id)
                .order_by(SnapshotEntryModel.created_at.asc())
            )
            entry_models = list(session.execute(stmt).scalars().all())
            entries = [_model_to_entry(m) for m in entry_models]

            # Detach txn model fields before session closes
            # (we need to return it outside the session scope)
            from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel as _TSM

            detached = _TSM(
                transaction_id=txn.transaction_id,
                zone_id=txn.zone_id,
                agent_id=txn.agent_id,
                status=txn.status,
                description=txn.description,
                created_at=txn.created_at,
                expires_at=txn.expires_at,
                completed_at=txn.completed_at,
                entry_count=txn.entry_count,
            )

            return detached, entries
