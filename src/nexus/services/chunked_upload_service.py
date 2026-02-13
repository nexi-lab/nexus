"""Chunked upload service for tus.io resumable uploads (Issue #788).

Manages the lifecycle of chunked upload sessions:
- Session creation with semaphore-based concurrency limiting
- Chunk reception with offset validation and checksum verification
- Session state persistence via SQLAlchemy
- Background cleanup of expired sessions
- Per-session locking for concurrent chunk uploads
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nexus.core.exceptions import (
    BackendError,
    UploadChecksumMismatchError,
    UploadExpiredError,
    UploadNotFoundError,
    UploadOffsetMismatchError,
    ValidationError,
)
from nexus.services.upload_session import UploadSession, UploadStatus
from nexus.storage.models.upload_session import UploadSessionModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session

    from nexus.backends.backend import Backend
    from nexus.backends.multipart_upload_mixin import MultipartUploadMixin
    from nexus.core._metadata_generated import FileMetadataProtocol

logger = logging.getLogger(__name__)

# tus protocol constants
TUS_VERSION = "1.0.0"
TUS_EXTENSIONS = "creation,termination,checksum,expiration"
TUS_CHECKSUM_ALGORITHMS = "sha256,md5,crc32"

# Default config values
DEFAULT_MAX_CONCURRENT = 20
DEFAULT_SESSION_TTL_HOURS = 24
DEFAULT_CLEANUP_INTERVAL_SECONDS = 3600
DEFAULT_MIN_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB
DEFAULT_MAX_CHUNK_SIZE = 64 * 1024 * 1024  # 64 MB
DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB


class ChunkedUploadConfig:
    """Configuration for the chunked upload service."""

    def __init__(
        self,
        max_concurrent_uploads: int = DEFAULT_MAX_CONCURRENT,
        session_ttl_hours: int = DEFAULT_SESSION_TTL_HOURS,
        cleanup_interval_seconds: int = DEFAULT_CLEANUP_INTERVAL_SECONDS,
        min_chunk_size: int = DEFAULT_MIN_CHUNK_SIZE,
        max_chunk_size: int = DEFAULT_MAX_CHUNK_SIZE,
        default_chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_upload_size: int = 10 * 1024 * 1024 * 1024,  # 10 GB
    ):
        self.max_concurrent_uploads = max_concurrent_uploads
        self.session_ttl_hours = session_ttl_hours
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.min_chunk_size = min_chunk_size
        self.max_chunk_size = max_chunk_size
        self.default_chunk_size = default_chunk_size
        self.max_upload_size = max_upload_size


class ChunkedUploadService:
    """Core service for tus.io resumable chunked uploads.

    Manages upload session lifecycle, chunk handling, and backend integration.
    Uses asyncio.Semaphore for concurrency limiting and per-session locks
    for safe concurrent access.

    Architecture:
        - Session state is persisted in SQLAlchemy (survives server restarts)
        - Chunk data flows through backend's MultipartUploadMixin if available
        - Falls back to temp directory assembly for non-multipart backends
        - Checksum verification per-chunk (sha256, md5, crc32)
        - TTL-based expiration with background sweep + lazy cleanup
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        backend: Backend,
        metadata_store: FileMetadataProtocol | None = None,
        config: ChunkedUploadConfig | None = None,
    ):
        self._session_factory = session_factory
        self._backend = backend
        self._metadata_store = metadata_store
        self._config = config or ChunkedUploadConfig()

        self._semaphore = asyncio.Semaphore(self._config.max_concurrent_uploads)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._parts_registry: dict[str, list[dict[str, Any]]] = {}
        self._last_cleanup = datetime.now(UTC)
        self._cleanup_task: asyncio.Task[None] | None = None

    # --- Public API ---

    async def create_upload(
        self,
        target_path: str,
        upload_length: int,
        *,
        metadata: dict[str, str] | None = None,
        zone_id: str = "default",
        user_id: str = "anonymous",
        checksum_algorithm: str | None = None,
    ) -> UploadSession:
        """Create a new upload session.

        Args:
            target_path: Virtual path where the final file will be written.
            upload_length: Total declared file size in bytes.
            metadata: Optional tus Upload-Metadata key-value pairs.
            zone_id: Zone ID for permission scoping.
            user_id: User who initiated the upload.
            checksum_algorithm: Preferred checksum algorithm.

        Returns:
            New UploadSession with upload_id and expiration.

        Raises:
            ValidationError: If upload_length is invalid.
            RuntimeError: If concurrency limit reached (429).
        """
        # Validate
        if upload_length < 0:
            raise ValidationError(f"upload_length must be non-negative, got {upload_length}")
        if upload_length > self._config.max_upload_size:
            raise ValidationError(
                f"upload_length {upload_length} exceeds maximum {self._config.max_upload_size}"
            )

        # Lazy cleanup on create
        await self._lazy_cleanup()

        # Acquire semaphore (non-blocking — return 429 if full)
        if not self._semaphore._value:  # noqa: SLF001
            raise RuntimeError("Too many concurrent uploads")

        await self._semaphore.acquire()

        try:
            upload_id = str(uuid.uuid4())
            now = datetime.now(UTC)
            expires_at = now + timedelta(hours=self._config.session_ttl_hours)

            # Initialize backend multipart if supported
            backend_upload_id: str | None = None
            backend_name: str | None = None
            if self._supports_multipart():
                mixin: MultipartUploadMixin = self._backend  # type: ignore[assignment]
                backend_upload_id = await asyncio.to_thread(
                    mixin.init_multipart,
                    target_path,
                    metadata.get("content_type", "application/octet-stream") if metadata else "application/octet-stream",
                    metadata,
                )
                backend_name = getattr(self._backend, "name", "unknown")

            session = UploadSession(
                upload_id=upload_id,
                target_path=target_path,
                upload_length=upload_length,
                upload_offset=0,
                status=UploadStatus.CREATED,
                zone_id=zone_id,
                user_id=user_id,
                metadata=metadata or {},
                checksum_algorithm=checksum_algorithm,
                created_at=now,
                expires_at=expires_at,
                backend_upload_id=backend_upload_id,
                backend_name=backend_name,
            )

            await self._persist_session(session)
            self._parts_registry[upload_id] = []

            logger.info(
                "Upload session created: %s -> %s (%d bytes, expires %s)",
                upload_id,
                target_path,
                upload_length,
                expires_at.isoformat(),
            )
            return session

        except Exception:
            self._semaphore.release()
            raise

    async def receive_chunk(
        self,
        upload_id: str,
        offset: int,
        chunk_data: bytes,
        checksum_header: str | None = None,
    ) -> UploadSession:
        """Receive and store a chunk of data.

        Args:
            upload_id: Upload session ID.
            offset: Expected byte offset for this chunk.
            chunk_data: Raw chunk bytes.
            checksum_header: Optional tus Upload-Checksum header value.

        Returns:
            Updated UploadSession with new offset.

        Raises:
            UploadNotFoundError: If session doesn't exist.
            UploadExpiredError: If session has expired.
            UploadOffsetMismatchError: If offset doesn't match current position.
            UploadChecksumMismatchError: If checksum verification fails.
            ValidationError: If chunk size is invalid.
        """
        lock = self._get_or_create_session_lock(upload_id)
        async with lock:
            session = await self._load_session(upload_id)

            # Validate session state
            if session.status in (UploadStatus.COMPLETED, UploadStatus.TERMINATED):
                raise UploadNotFoundError(upload_id, f"Upload is {session.status.value}")

            if session.is_expired:
                await self._expire_session(session)
                raise UploadExpiredError(upload_id)

            # Validate offset
            if offset != session.upload_offset:
                raise UploadOffsetMismatchError(upload_id, session.upload_offset, offset)

            # Validate chunk size
            chunk_size = len(chunk_data)
            remaining = session.remaining_bytes
            is_last_chunk = (chunk_size == remaining)

            if chunk_size > remaining:
                raise ValidationError(
                    f"Chunk would exceed upload_length: "
                    f"offset={offset} + chunk={chunk_size} > total={session.upload_length}"
                )

            if not is_last_chunk and chunk_size < self._config.min_chunk_size:
                raise ValidationError(
                    f"Chunk size {chunk_size} below minimum {self._config.min_chunk_size} "
                    f"(except for last chunk)"
                )

            if chunk_size > self._config.max_chunk_size:
                raise ValidationError(
                    f"Chunk size {chunk_size} exceeds maximum {self._config.max_chunk_size}"
                )

            # Verify checksum if provided
            if checksum_header:
                self._verify_checksum(upload_id, chunk_data, checksum_header)

            # Store chunk
            part_number = session.parts_received + 1
            part_info = await self._store_chunk(session, part_number, chunk_data)

            parts = self._parts_registry.get(upload_id, [])
            parts.append(part_info)
            self._parts_registry[upload_id] = parts

            # Update session
            new_offset = session.upload_offset + chunk_size
            new_status = UploadStatus.IN_PROGRESS

            updated = UploadSession(
                upload_id=session.upload_id,
                target_path=session.target_path,
                upload_length=session.upload_length,
                upload_offset=new_offset,
                status=new_status,
                zone_id=session.zone_id,
                user_id=session.user_id,
                metadata=session.metadata,
                checksum_algorithm=session.checksum_algorithm,
                created_at=session.created_at,
                expires_at=session.expires_at,
                backend_upload_id=session.backend_upload_id,
                backend_name=session.backend_name,
                parts_received=part_number,
                content_hash=session.content_hash,
            )

            await self._update_session(updated)

            # If upload is complete, assemble and write
            if updated.is_complete:
                updated = await self._assemble_and_write(updated, parts)

            return updated

    async def get_upload_status(self, upload_id: str) -> UploadSession:
        """Get the current status of an upload session.

        Args:
            upload_id: Upload session ID.

        Returns:
            Current UploadSession state.

        Raises:
            UploadNotFoundError: If session doesn't exist.
            UploadExpiredError: If session has expired.
        """
        session = await self._load_session(upload_id)

        if session.is_expired and session.status not in (
            UploadStatus.COMPLETED,
            UploadStatus.TERMINATED,
            UploadStatus.EXPIRED,
        ):
            await self._expire_session(session)
            raise UploadExpiredError(upload_id)

        return session

    async def terminate_upload(self, upload_id: str) -> None:
        """Terminate an upload and clean up resources.

        Args:
            upload_id: Upload session ID.

        Raises:
            UploadNotFoundError: If session doesn't exist.
        """
        session = await self._load_session(upload_id)

        # Abort backend multipart if active
        if session.backend_upload_id and self._supports_multipart():
            try:
                mixin: MultipartUploadMixin = self._backend  # type: ignore[assignment]
                await asyncio.to_thread(
                    mixin.abort_multipart,
                    session.target_path,
                    session.backend_upload_id,
                )
            except Exception as e:
                logger.warning("Failed to abort backend multipart for %s: %s", upload_id, e)

        # Mark as terminated
        terminated = UploadSession(
            upload_id=session.upload_id,
            target_path=session.target_path,
            upload_length=session.upload_length,
            upload_offset=session.upload_offset,
            status=UploadStatus.TERMINATED,
            zone_id=session.zone_id,
            user_id=session.user_id,
            metadata=session.metadata,
            checksum_algorithm=session.checksum_algorithm,
            created_at=session.created_at,
            expires_at=session.expires_at,
            backend_upload_id=session.backend_upload_id,
            backend_name=session.backend_name,
            parts_received=session.parts_received,
            content_hash=session.content_hash,
        )
        await self._update_session(terminated)

        # Clean up
        self._parts_registry.pop(upload_id, None)
        self._session_locks.pop(upload_id, None)
        self._semaphore.release()

        logger.info("Upload session terminated: %s", upload_id)

    def get_server_capabilities(self) -> dict[str, str]:
        """Return tus server capability headers for OPTIONS response."""
        return {
            "Tus-Resumable": TUS_VERSION,
            "Tus-Version": TUS_VERSION,
            "Tus-Extension": TUS_EXTENSIONS,
            "Tus-Max-Size": str(self._config.max_upload_size),
            "Tus-Checksum-Algorithm": TUS_CHECKSUM_ALGORITHMS,
        }

    async def cleanup_expired(self) -> int:
        """Sweep and clean up expired sessions.

        Returns:
            Number of sessions cleaned up.
        """
        now = datetime.now(UTC)
        cleaned = 0

        def _query_expired() -> list[dict[str, Any]]:
            db = self._session_factory()
            try:
                rows = (
                    db.query(UploadSessionModel)
                    .filter(
                        UploadSessionModel.expires_at < now,
                        UploadSessionModel.status.in_(["created", "in_progress"]),
                    )
                    .all()
                )
                return [row.to_session_dict() for row in rows]
            finally:
                db.close()

        expired_dicts = await asyncio.to_thread(_query_expired)

        for session_dict in expired_dicts:
            try:
                session = UploadSession.from_dict(session_dict)
                await self._expire_session(session)
                cleaned += 1
            except Exception as e:
                logger.warning(
                    "Failed to clean up expired session %s: %s",
                    session_dict.get("upload_id"),
                    e,
                )

        if cleaned > 0:
            logger.info("Cleaned up %d expired upload sessions", cleaned)

        self._last_cleanup = now
        return cleaned

    async def start_cleanup_loop(self) -> None:
        """Start the background cleanup loop."""
        while True:
            try:
                await asyncio.sleep(self._config.cleanup_interval_seconds)
                await self.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in upload cleanup loop: %s", e)

    # --- Internal helpers ---

    def _supports_multipart(self) -> bool:
        """Check if backend supports native multipart uploads."""
        from nexus.backends.multipart_upload_mixin import MultipartUploadMixin

        return isinstance(self._backend, MultipartUploadMixin)

    def _get_or_create_session_lock(self, upload_id: str) -> asyncio.Lock:
        """Get or create a per-session asyncio.Lock."""
        if upload_id not in self._session_locks:
            self._session_locks[upload_id] = asyncio.Lock()
        return self._session_locks[upload_id]

    async def _persist_session(self, session: UploadSession) -> None:
        """Persist a new session to the database."""

        def _write(s: UploadSession) -> None:
            db = self._session_factory()
            try:
                model = UploadSessionModel.from_upload_session(s)
                db.add(model)
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        await asyncio.to_thread(_write, session)

    async def _load_session(self, upload_id: str) -> UploadSession:
        """Load a session from the database.

        Raises:
            UploadNotFoundError: If session doesn't exist.
        """

        def _read(uid: str) -> dict[str, Any] | None:
            db = self._session_factory()
            try:
                model = db.query(UploadSessionModel).filter_by(upload_id=uid).first()
                if model is None:
                    return None
                return model.to_session_dict()
            finally:
                db.close()

        result = await asyncio.to_thread(_read, upload_id)
        if result is None:
            raise UploadNotFoundError(upload_id)
        return UploadSession.from_dict(result)

    async def _update_session(self, session: UploadSession) -> None:
        """Update an existing session in the database."""

        def _write(s: UploadSession) -> None:
            db = self._session_factory()
            try:
                model = db.query(UploadSessionModel).filter_by(upload_id=s.upload_id).first()
                if model is None:
                    raise UploadNotFoundError(s.upload_id)
                model.upload_offset = s.upload_offset
                model.status = s.status.value
                model.parts_received = s.parts_received
                model.content_hash = s.content_hash
                model.backend_upload_id = s.backend_upload_id
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        await asyncio.to_thread(_write, session)

    async def _delete_session(self, upload_id: str) -> None:
        """Delete a session from the database."""

        def _delete(uid: str) -> None:
            db = self._session_factory()
            try:
                db.query(UploadSessionModel).filter_by(upload_id=uid).delete()
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        await asyncio.to_thread(_delete, upload_id)

    async def _store_chunk(
        self,
        session: UploadSession,
        part_number: int,
        data: bytes,
    ) -> dict[str, Any]:
        """Store a chunk via the backend.

        Returns:
            Part info dict with at least "etag" and "part_number".
        """
        if self._supports_multipart() and session.backend_upload_id:
            mixin: MultipartUploadMixin = self._backend  # type: ignore[assignment]
            return await asyncio.to_thread(
                mixin.upload_part,
                session.target_path,
                session.backend_upload_id,
                part_number,
                data,
            )

        # Fallback: write chunk directly to CAS and track
        from nexus.core.hash_fast import hash_content

        chunk_hash = hash_content(data)
        response = await asyncio.to_thread(self._backend.write_content, data)
        if not response.success:
            raise BackendError(
                f"Failed to store chunk {part_number} for upload {session.upload_id}",
                backend=getattr(self._backend, "name", "unknown"),
            )
        return {
            "etag": chunk_hash,
            "part_number": part_number,
            "content_hash": response.data,
        }

    async def _assemble_and_write(
        self,
        session: UploadSession,
        parts: list[dict[str, Any]],
    ) -> UploadSession:
        """Assemble chunks and write the final file.

        For multipart backends, delegates to complete_multipart().
        For others, reads parts and concatenates.

        Returns:
            Updated session with COMPLETED status and content_hash.
        """
        content_hash: str

        if self._supports_multipart() and session.backend_upload_id:
            mixin: MultipartUploadMixin = self._backend  # type: ignore[assignment]
            content_hash = await asyncio.to_thread(
                mixin.complete_multipart,
                session.target_path,
                session.backend_upload_id,
                parts,
            )
        else:
            # Read all chunks and concatenate
            sorted_parts = sorted(parts, key=lambda p: p["part_number"])
            assembled = bytearray()
            for part_info in sorted_parts:
                part_hash = part_info.get("content_hash")
                if part_hash:
                    response = await asyncio.to_thread(
                        self._backend.read_content, part_hash
                    )
                    if response.success and response.data:
                        assembled.extend(response.data)
                    else:
                        raise BackendError(
                            f"Failed to read chunk {part_info['part_number']}",
                            backend=getattr(self._backend, "name", "unknown"),
                        )

            # Write assembled content
            content = bytes(assembled)
            _write = self._backend.write_content
            response = await asyncio.to_thread(_write, content)  # type: ignore[arg-type]
            if not response.success or response.data is None:
                raise BackendError(
                    "Failed to write assembled content",
                    backend=getattr(self._backend, "name", "unknown"),
                )
            content_hash = str(response.data)

        # Update session to completed
        completed = UploadSession(
            upload_id=session.upload_id,
            target_path=session.target_path,
            upload_length=session.upload_length,
            upload_offset=session.upload_length,
            status=UploadStatus.COMPLETED,
            zone_id=session.zone_id,
            user_id=session.user_id,
            metadata=session.metadata,
            checksum_algorithm=session.checksum_algorithm,
            created_at=session.created_at,
            expires_at=session.expires_at,
            backend_upload_id=session.backend_upload_id,
            backend_name=session.backend_name,
            parts_received=session.parts_received,
            content_hash=content_hash,
        )
        await self._update_session(completed)

        # Clean up tracking
        self._parts_registry.pop(session.upload_id, None)
        self._session_locks.pop(session.upload_id, None)
        self._semaphore.release()

        logger.info(
            "Upload completed: %s -> %s (hash=%s)",
            session.upload_id,
            session.target_path,
            content_hash,
        )
        return completed

    async def _expire_session(self, session: UploadSession) -> None:
        """Mark a session as expired and clean up."""
        # Abort backend multipart if active
        if session.backend_upload_id and self._supports_multipart():
            try:
                mixin: MultipartUploadMixin = self._backend  # type: ignore[assignment]
                await asyncio.to_thread(
                    mixin.abort_multipart,
                    session.target_path,
                    session.backend_upload_id,
                )
            except Exception as e:
                logger.warning(
                    "Failed to abort backend multipart for expired %s: %s",
                    session.upload_id,
                    e,
                )

        expired = UploadSession(
            upload_id=session.upload_id,
            target_path=session.target_path,
            upload_length=session.upload_length,
            upload_offset=session.upload_offset,
            status=UploadStatus.EXPIRED,
            zone_id=session.zone_id,
            user_id=session.user_id,
            metadata=session.metadata,
            checksum_algorithm=session.checksum_algorithm,
            created_at=session.created_at,
            expires_at=session.expires_at,
            backend_upload_id=session.backend_upload_id,
            backend_name=session.backend_name,
            parts_received=session.parts_received,
            content_hash=session.content_hash,
        )

        import contextlib

        with contextlib.suppress(Exception):
            await self._update_session(expired)

        self._parts_registry.pop(session.upload_id, None)
        self._session_locks.pop(session.upload_id, None)

        # Release semaphore if session was active
        if session.status in (UploadStatus.CREATED, UploadStatus.IN_PROGRESS):
            self._semaphore.release()

    async def _lazy_cleanup(self) -> None:
        """Trigger cleanup if enough time has passed since last sweep."""
        now = datetime.now(UTC)
        elapsed = (now - self._last_cleanup).total_seconds()
        if elapsed >= self._config.cleanup_interval_seconds:
            try:
                await self.cleanup_expired()
            except Exception as e:
                logger.warning("Lazy cleanup failed: %s", e)

    @staticmethod
    def _verify_checksum(
        upload_id: str,
        data: bytes,
        checksum_header: str,
    ) -> None:
        """Verify chunk checksum against tus Upload-Checksum header.

        Format: "<algorithm> <base64_digest>"
        Supported: sha256, md5, crc32

        Raises:
            UploadChecksumMismatchError: If checksum doesn't match.
            ValidationError: If algorithm is unsupported.
        """
        parts = checksum_header.strip().split(" ", 1)
        if len(parts) != 2:
            raise ValidationError(
                f"Invalid Upload-Checksum header format: {checksum_header}"
            )

        algorithm, expected_b64 = parts[0].lower(), parts[1]

        if algorithm == "sha256":
            actual = base64.b64encode(hashlib.sha256(data).digest()).decode()
        elif algorithm == "md5":
            actual = base64.b64encode(hashlib.md5(data).digest()).decode()  # noqa: S324
        elif algorithm == "crc32":
            crc = zlib.crc32(data) & 0xFFFFFFFF
            actual = base64.b64encode(crc.to_bytes(4, "big")).decode()
        else:
            raise ValidationError(
                f"Unsupported checksum algorithm: {algorithm}. "
                f"Supported: {TUS_CHECKSUM_ALGORITHMS}"
            )

        if actual != expected_b64:
            raise UploadChecksumMismatchError(upload_id, algorithm)
