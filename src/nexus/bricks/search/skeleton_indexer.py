"""SkeletonIndexer — title extraction and BM25S upsert for document_skeleton (Issue #3725).

Responsible for:
    1. Reading the first SKELETON_HEAD_BYTES of a file.
    2. Computing skeleton_content_hash = sha256(head) and skipping if unchanged.
    3. Dispatching to the correct DocumentExtractor from SKELETON_EXTRACTOR_REGISTRY.
    4. Upserting the row into document_skeleton (DB) and the BM25S index (daemon).

This module is intentionally narrow — it knows nothing about the pipe consumer
lifecycle or the mutation event format.  The SkeletonPipeConsumer calls
``index_file()`` and ``delete_file()`` directly.

Design decisions:
    - 2KB head cap enforced here (7A), not inside each extractor.
    - path_tokens dropped (6A): virtual_path + title fed as separate BM25S fields.
    - skip guard via skeleton_content_hash (14A).
    - Bootstrap from DB rows handled by SearchDaemon, not this module (13B).
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Maximum bytes read from a file for title extraction (Issue #3725, 7A).
SKELETON_HEAD_BYTES: int = 2048


class FileReaderProtocol(Protocol):
    """Minimal file-reading interface needed by SkeletonIndexer."""

    async def read_head(self, virtual_path: str, max_bytes: int) -> bytes:
        """Read the first max_bytes of a file.  Returns b'' on any error."""
        ...


class SkeletonBM25Protocol(Protocol):
    """BM25S index interface needed by SkeletonIndexer."""

    async def upsert_skeleton(
        self,
        doc_id: str,
        virtual_path: str,
        title: str | None,
        zone_id: str,
        *,
        path_id: str | None = None,
    ) -> None:
        """Upsert a skeleton document into the BM25S index.

        path_id is the UUID from file_paths — passed through to adapters that
        need it (e.g. _DaemonSkeletonBM25) and ignored by stub implementations.
        """
        ...

    async def delete_skeleton(self, doc_id: str, zone_id: str) -> None:
        """Remove a skeleton document from the BM25S index."""
        ...


class SkeletonIndexer:
    """Extracts file titles and maintains the document_skeleton index.

    Thread-safety: each async method is independent; safe to call concurrently
    from the micro-batched consumer.
    """

    def __init__(
        self,
        file_reader: FileReaderProtocol,
        bm25: SkeletonBM25Protocol,
        async_session_factory: Any | None = None,
        extractor_registry: dict[str, Any] | None = None,
    ) -> None:
        self._reader = file_reader
        self._bm25 = bm25
        self._session_factory = async_session_factory
        # extractor_registry is injected by the factory layer (LEGO Principle 3).
        # When None, _extract_title() returns None for all files.
        self._extractor_registry = extractor_registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def index_file(
        self,
        *,
        path_id: str | None = None,
        virtual_path: str,
        zone_id: str,
    ) -> bool:
        """Extract title for virtual_path and upsert into document_skeleton.

        path_id may be omitted when called from a VFS hook (where only the
        virtual path and zone are available).  When None, it is resolved from
        the file_paths table before the DB upsert.  If resolution fails the
        DB row is skipped but the in-memory BM25 index is still updated so
        locate() remains functional.

        Returns True if the skeleton row was written (new or changed).
        Returns False if the content hash matched and the row was skipped.
        """
        head = await self._reader.read_head(virtual_path, SKELETON_HEAD_BYTES)
        new_hash = _sha256_head(head)

        # Resolve path_id from DB when not provided (VFS hook path, Issue #3725).
        if path_id is None:
            path_id = await self._resolve_path_id(virtual_path, zone_id)

        # --- skip guard (14A): only applies when we have a path_id to query ---
        if path_id is not None:
            existing_hash = await self._fetch_existing_hash(path_id)
            if existing_hash is not None and existing_hash == new_hash:
                logger.debug("[SKELETON] skip unchanged %s", virtual_path)
                return False

        title = _extract_title(virtual_path, head, self._extractor_registry)

        # --- DB upsert (skipped when path_id unavailable — CASCADE FK required) ---
        if path_id is not None:
            await self._upsert_db_row(path_id, zone_id, title, new_hash)

        # --- BM25S upsert (6A: path + title as separate fields) ---
        await self._bm25.upsert_skeleton(
            doc_id=virtual_path,
            virtual_path=virtual_path,
            title=title,
            zone_id=zone_id,
            path_id=path_id,
        )

        logger.debug("[SKELETON] indexed %s title=%r", virtual_path, title)
        return True

    async def delete_file(
        self,
        *,
        path_id: str | None = None,
        virtual_path: str,
        zone_id: str,
    ) -> None:
        """Remove a file from document_skeleton (DB + BM25S).

        path_id may be omitted when called from a VFS delete hook.  When
        omitted the DB row is left for CASCADE cleanup (file_paths FK).
        The in-memory BM25 entry is always removed.
        """
        if path_id is not None:
            await self._delete_db_row(path_id)
        # When path_id is None the CASCADE FK on document_skeleton.path_id
        # will clean up the DB row when the file_paths entry is deleted.
        await self._bm25.delete_skeleton(doc_id=virtual_path, zone_id=zone_id)
        logger.debug("[SKELETON] deleted %s", virtual_path)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _resolve_path_id(self, virtual_path: str, zone_id: str) -> str | None:
        """Look up path_id from file_paths by virtual_path + zone_id (Issue #3725).

        Called when index_file() or delete_file() are invoked from a VFS hook
        that only provides the path and zone, not the UUID.
        Returns None if no matching row is found or on any DB error.
        """
        if self._session_factory is None:
            return None
        try:
            from sqlalchemy import select

            from nexus.storage.models.file_path import FilePathModel

            async with self._session_factory() as session:
                result = await session.execute(
                    select(FilePathModel.path_id).where(
                        FilePathModel.virtual_path == virtual_path,
                        FilePathModel.zone_id == zone_id,
                        FilePathModel.deleted_at.is_(None),
                    )
                )
                return str(result.scalar_one_or_none() or "") or None
        except Exception:
            logger.debug("[SKELETON] path_id resolution failed for %s", virtual_path)
            return None

    async def _fetch_existing_hash(self, path_id: str) -> str | None:
        if self._session_factory is None:
            return None
        try:
            from sqlalchemy import select

            from nexus.storage.models.document_skeleton import DocumentSkeletonModel

            async with self._session_factory() as session:
                result = await session.execute(
                    select(DocumentSkeletonModel.skeleton_content_hash).where(
                        DocumentSkeletonModel.path_id == path_id
                    )
                )
                row = result.scalar_one_or_none()
                return str(row) if row is not None else None
        except Exception:
            logger.debug("[SKELETON] could not fetch existing hash for %s", path_id)
            return None

    async def _upsert_db_row(
        self,
        path_id: str,
        zone_id: str,
        title: str | None,
        content_hash: str,
    ) -> None:
        if self._session_factory is None:
            return
        try:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            from nexus.storage.models.document_skeleton import DocumentSkeletonModel

            now = datetime.now(UTC)
            async with self._session_factory() as session:
                # Use upsert semantics: INSERT OR REPLACE (SQLite) / ON CONFLICT DO UPDATE (PG)
                stmt = sqlite_insert(DocumentSkeletonModel).values(
                    path_id=path_id,
                    zone_id=zone_id,
                    title=title,
                    skeleton_content_hash=content_hash,
                    indexed_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["path_id"],
                    set_={
                        "zone_id": zone_id,
                        "title": title,
                        "skeleton_content_hash": content_hash,
                        "indexed_at": now,
                    },
                )
                await session.execute(stmt)
                await session.commit()
        except Exception:
            # Attempt PostgreSQL upsert as fallback
            await self._upsert_db_row_pg(path_id, zone_id, title, content_hash)

    async def _upsert_db_row_pg(
        self,
        path_id: str,
        zone_id: str,
        title: str | None,
        content_hash: str,
    ) -> None:
        if self._session_factory is None:
            return
        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from nexus.storage.models.document_skeleton import DocumentSkeletonModel

            now = datetime.now(UTC)
            async with self._session_factory() as session:
                stmt = pg_insert(DocumentSkeletonModel).values(
                    path_id=path_id,
                    zone_id=zone_id,
                    title=title,
                    skeleton_content_hash=content_hash,
                    indexed_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["path_id"],
                    set_={
                        "zone_id": zone_id,
                        "title": title,
                        "skeleton_content_hash": content_hash,
                        "indexed_at": now,
                    },
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            logger.warning("[SKELETON] DB upsert failed for %s: %s", path_id, e)

    async def _delete_db_row(self, path_id: str) -> None:
        if self._session_factory is None:
            return
        try:
            from sqlalchemy import delete

            from nexus.storage.models.document_skeleton import DocumentSkeletonModel

            async with self._session_factory() as session:
                await session.execute(
                    delete(DocumentSkeletonModel).where(DocumentSkeletonModel.path_id == path_id)
                )
                await session.commit()
        except Exception as e:
            logger.warning("[SKELETON] DB delete failed for %s: %s", path_id, e)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _sha256_head(data: bytes) -> str:
    """Return hex sha256 of data (the 2KB head)."""
    return hashlib.sha256(data).hexdigest()


def _extract_title(
    virtual_path: str,
    head: bytes,
    registry: dict[str, Any] | None = None,
) -> str | None:
    """Dispatch title extraction to the correct registry extractor.

    registry is injected from the factory layer (LEGO Principle 3 — search
    brick must not import from catalog brick directly).  When None, returns
    None for all files (title extraction disabled, path tokens still indexed).

    Returns None for binary files, empty files, or unrecognised extensions.
    Never raises.
    """
    if not head or registry is None:
        return None

    # Detect binary: if more than 10% of the first 512 bytes are non-text, skip.
    sample = head[:512]
    non_text = sum(1 for b in sample if b < 9 or (13 < b < 32) or b == 127)
    if len(sample) > 0 and non_text / len(sample) > 0.10:
        return None

    ext = os.path.splitext(virtual_path)[1].lstrip(".").lower()
    extractor = registry.get(ext)
    if extractor is None:
        return None

    try:
        result = extractor.extract(head)
        return str(result.title) if result.title is not None else None
    except Exception as e:
        logger.debug("[SKELETON] extractor error for %s: %s", virtual_path, e)
        return None
