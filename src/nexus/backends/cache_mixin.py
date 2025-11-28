"""Cache mixin for connectors.

Provides caching capabilities for connector backends (GCS, S3, X, Gmail, etc.).
Local backend does not use this mixin - caching is only for external connectors.

See docs/design/cache-layer.md for design details.
Part of: #506, #510 (cache layer epic)
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from nexus.core.exceptions import ConflictError
from nexus.storage.models import ContentCacheModel, FilePathModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.backends.backend import OperationContext


@dataclass
class SyncResult:
    """Result of a sync operation."""

    files_scanned: int = 0
    files_synced: int = 0
    files_skipped: int = 0
    bytes_synced: int = 0
    embeddings_generated: int = 0
    errors: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"SyncResult(scanned={self.files_scanned}, synced={self.files_synced}, "
            f"skipped={self.files_skipped}, bytes={self.bytes_synced}, "
            f"embeddings={self.embeddings_generated}, errors={len(self.errors)})"
        )


@dataclass
class CacheEntry:
    """A cached content entry."""

    cache_id: str
    path_id: str
    content_text: str | None
    content_binary: bytes | None
    content_hash: str
    content_type: str
    original_size: int
    cached_size: int
    backend_version: str | None
    synced_at: datetime
    stale: bool
    parsed_from: str | None = None
    parse_metadata: dict | None = None


class CacheConnectorMixin:
    """Mixin that adds cache support to connectors.

    Usage:
        class GCSConnectorBackend(BaseBlobStorageConnector, CacheConnectorMixin):
            pass

    The connector must have:
        - self.db_session: SQLAlchemy session
        - self._read_from_backend(): Read content from actual backend
        - self._list_files(): List files from backend

    Optional (for version checking):
        - self.get_version(): Get current backend version for a path
    """

    # Maximum file size to cache (default 100MB)
    MAX_CACHE_FILE_SIZE: int = 100 * 1024 * 1024

    # Maximum text size to store as 'full' (default 10MB)
    MAX_FULL_TEXT_SIZE: int = 10 * 1024 * 1024

    # Summary size for large files (default 100KB)
    SUMMARY_SIZE: int = 100 * 1024

    def _get_db_session(self) -> Session:
        """Get database session. Override if session is stored differently."""
        if hasattr(self, "db_session") and self.db_session is not None:
            return self.db_session  # type: ignore[no-any-return]
        if hasattr(self, "_db_session") and self._db_session is not None:
            return self._db_session  # type: ignore[no-any-return]
        raise RuntimeError("No database session available for caching")

    def _get_path_id(self, path: str, session: Session) -> str | None:
        """Get path_id for a virtual path."""
        stmt = select(FilePathModel.path_id).where(
            FilePathModel.virtual_path == path,
            FilePathModel.deleted_at.is_(None),
        )
        result = session.execute(stmt)
        row = result.scalar_one_or_none()
        return row

    def _read_from_cache(
        self,
        path: str,
        original: bool = False,
    ) -> CacheEntry | None:
        """Read content from cache.

        Args:
            path: Virtual file path
            original: If True, return binary content even for parsed files

        Returns:
            CacheEntry if cached, None otherwise
        """
        session = self._get_db_session()

        path_id = self._get_path_id(path, session)
        if not path_id:
            return None

        stmt = select(ContentCacheModel).where(ContentCacheModel.path_id == path_id)
        result = session.execute(stmt)
        cache_model = result.scalar_one_or_none()

        if not cache_model:
            return None

        # Decode binary if stored
        content_binary = None
        if original and cache_model.content_binary:
            with contextlib.suppress(Exception):
                content_binary = base64.b64decode(cache_model.content_binary)

        # Parse metadata if stored
        parse_metadata = None
        if cache_model.parse_metadata:
            with contextlib.suppress(Exception):
                import json

                parse_metadata = json.loads(cache_model.parse_metadata)

        return CacheEntry(
            cache_id=cache_model.cache_id,
            path_id=cache_model.path_id,
            content_text=cache_model.content_text,
            content_binary=content_binary,
            content_hash=cache_model.content_hash,
            content_type=cache_model.content_type,
            original_size=cache_model.original_size_bytes,
            cached_size=cache_model.cached_size_bytes,
            backend_version=cache_model.backend_version,
            synced_at=cache_model.synced_at,
            stale=cache_model.stale,
            parsed_from=cache_model.parsed_from,
            parse_metadata=parse_metadata,
        )

    def _write_to_cache(
        self,
        path: str,
        content: bytes,
        content_text: str | None = None,
        content_type: str = "full",
        backend_version: str | None = None,
        parsed_from: str | None = None,
        parse_metadata: dict | None = None,
        tenant_id: str | None = None,
    ) -> CacheEntry:
        """Write content to cache.

        Args:
            path: Virtual file path
            content: Original binary content
            content_text: Parsed/extracted text (if None, tries to decode content as UTF-8)
            content_type: 'full', 'parsed', 'summary', or 'reference'
            backend_version: Backend version for optimistic locking
            parsed_from: Parser that extracted text ('pdf', 'xlsx', etc.)
            parse_metadata: Additional metadata from parsing
            tenant_id: Tenant ID for multi-tenant filtering

        Returns:
            CacheEntry for the cached content
        """
        session = self._get_db_session()

        path_id = self._get_path_id(path, session)
        if not path_id:
            raise ValueError(f"Path not found in file_paths: {path}")

        # Compute content hash
        content_hash = hashlib.sha256(content).hexdigest()

        # Determine text content
        if content_text is None:
            try:
                content_text = content.decode("utf-8")
            except UnicodeDecodeError:
                content_text = None
                content_type = "reference"  # Can't decode, store as reference only

        # Handle large files
        original_size = len(content)
        if content_text and len(content_text) > self.MAX_FULL_TEXT_SIZE:
            content_text = content_text[: self.SUMMARY_SIZE]
            content_type = "summary"

        cached_size = len(content_text) if content_text else 0

        # Encode binary for storage (base64)
        content_binary_b64 = None
        if original_size <= self.MAX_CACHE_FILE_SIZE:
            content_binary_b64 = base64.b64encode(content).decode("ascii")

        # Serialize parse_metadata
        parse_metadata_json = None
        if parse_metadata:
            import json

            parse_metadata_json = json.dumps(parse_metadata)

        now = datetime.now(UTC)

        # Check if entry exists
        stmt = select(ContentCacheModel).where(ContentCacheModel.path_id == path_id)
        result = session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # Update existing entry
            existing.content_text = content_text
            existing.content_binary = content_binary_b64  # type: ignore[assignment]
            existing.content_hash = content_hash
            existing.content_type = content_type
            existing.original_size_bytes = original_size
            existing.cached_size_bytes = cached_size
            existing.backend_version = backend_version
            existing.parsed_from = parsed_from
            existing.parser_version = None  # TODO: Add parser versioning
            existing.parse_metadata = parse_metadata_json
            existing.synced_at = now
            existing.stale = False
            existing.updated_at = now
            cache_id = existing.cache_id
        else:
            # Create new entry
            cache_id = str(uuid.uuid4())
            cache_model = ContentCacheModel(
                cache_id=cache_id,
                path_id=path_id,
                tenant_id=tenant_id,
                content_text=content_text,
                content_binary=content_binary_b64,
                content_hash=content_hash,
                content_type=content_type,
                original_size_bytes=original_size,
                cached_size_bytes=cached_size,
                backend_version=backend_version,
                parsed_from=parsed_from,
                parser_version=None,
                parse_metadata=parse_metadata_json,
                synced_at=now,
                stale=False,
                created_at=now,
                updated_at=now,
            )
            session.add(cache_model)

        session.commit()

        return CacheEntry(
            cache_id=cache_id,
            path_id=path_id,
            content_text=content_text,
            content_binary=content if content_binary_b64 else None,
            content_hash=content_hash,
            content_type=content_type,
            original_size=original_size,
            cached_size=cached_size,
            backend_version=backend_version,
            synced_at=now,
            stale=False,
            parsed_from=parsed_from,
            parse_metadata=parse_metadata,
        )

    def _invalidate_cache(
        self,
        path: str | None = None,
        mount_prefix: str | None = None,
        delete: bool = False,
    ) -> int:
        """Invalidate cache entries.

        Args:
            path: Specific path to invalidate
            mount_prefix: Invalidate all paths under this prefix
            delete: If True, delete entries. If False, mark as stale.

        Returns:
            Number of entries invalidated
        """
        session = self._get_db_session()

        if path:
            path_id = self._get_path_id(path, session)
            if not path_id:
                return 0

            stmt = select(ContentCacheModel).where(ContentCacheModel.path_id == path_id)
            result = session.execute(stmt)
            entry = result.scalar_one_or_none()

            if not entry:
                return 0

            if delete:
                session.delete(entry)
            else:
                entry.stale = True
                entry.updated_at = datetime.now(UTC)

            session.commit()
            return 1

        elif mount_prefix:
            # Invalidate all entries under mount prefix
            stmt = (
                select(ContentCacheModel)
                .join(FilePathModel, ContentCacheModel.path_id == FilePathModel.path_id)
                .where(FilePathModel.virtual_path.startswith(mount_prefix))
            )
            result = session.execute(stmt)
            entries = result.scalars().all()

            count = 0
            for entry in entries:
                if delete:
                    session.delete(entry)
                else:
                    entry.stale = True
                    entry.updated_at = datetime.now(UTC)
                count += 1

            session.commit()
            return count

        return 0

    def _check_version(
        self,
        path: str,
        expected_version: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if backend version matches expected.

        Args:
            path: Virtual file path
            expected_version: Expected backend version
            context: Operation context

        Returns:
            True if versions match, False otherwise

        Raises:
            ConflictError: If versions don't match
        """
        if not hasattr(self, "get_version"):
            return True  # No version support, always succeed

        current_version = self.get_version(path, context)
        if current_version is None:
            return True  # Backend doesn't support versioning

        if current_version != expected_version:
            raise ConflictError(
                path=path,
                expected_etag=expected_version,
                current_etag=current_version,
            )

        return True

    def sync(
        self,
        path: str | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_file_size: int | None = None,
        generate_embeddings: bool = True,
        context: OperationContext | None = None,
    ) -> SyncResult:
        """Sync content from connector to cache.

        Args:
            path: Specific path to sync, or None for entire mount
            include_patterns: Glob patterns to include (e.g., ["*.py", "*.md"])
            exclude_patterns: Glob patterns to exclude (e.g., ["*.pyc", ".git/*"])
            max_file_size: Maximum file size to cache (default: MAX_CACHE_FILE_SIZE)
            generate_embeddings: Generate embeddings for semantic search
            context: Operation context

        Returns:
            SyncResult with statistics
        """
        import fnmatch

        result = SyncResult()
        max_size = max_file_size or self.MAX_CACHE_FILE_SIZE

        # Get files to sync
        try:
            if path:
                # Sync specific path
                files = [path]
            elif hasattr(self, "list_dir"):
                # List all files recursively
                files = self._list_files_recursive("/", context)
            else:
                result.errors.append("Connector does not support list_dir")
                return result
        except Exception as e:
            result.errors.append(f"Failed to list files: {e}")
            return result

        result.files_scanned = len(files)

        for file_path in files:
            try:
                # Check include/exclude patterns
                if include_patterns and not any(
                    fnmatch.fnmatch(file_path, p) for p in include_patterns
                ):
                    result.files_skipped += 1
                    continue

                if exclude_patterns and any(
                    fnmatch.fnmatch(file_path, p) for p in exclude_patterns
                ):
                    result.files_skipped += 1
                    continue

                # Read content from backend
                content = self._read_content_from_backend(file_path, context)

                if content is None:
                    result.files_skipped += 1
                    continue

                # Check size
                if len(content) > max_size:
                    result.files_skipped += 1
                    continue

                # Get version if supported
                version = None
                if hasattr(self, "get_version"):
                    with contextlib.suppress(Exception):
                        version = self.get_version(file_path, context)

                # Get tenant_id from context
                tenant_id = None
                if context and hasattr(context, "tenant_id"):
                    tenant_id = context.tenant_id

                # Write to cache
                self._write_to_cache(
                    path=file_path,
                    content=content,
                    backend_version=version,
                    tenant_id=tenant_id,
                )

                result.files_synced += 1
                result.bytes_synced += len(content)

                # Generate embeddings if requested
                if generate_embeddings:
                    try:
                        self._generate_embeddings(file_path)
                        result.embeddings_generated += 1
                    except Exception as e:
                        result.errors.append(f"Failed to generate embeddings for {file_path}: {e}")

            except Exception as e:
                result.errors.append(f"Failed to sync {file_path}: {e}")

        return result

    def _list_files_recursive(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> list[str]:
        """Recursively list all files under a path."""
        files: list[str] = []

        if not hasattr(self, "list_dir"):
            return files

        try:
            entries = self.list_dir(path, context)
            for entry in entries:
                # Build full path
                if path == "/":
                    full_path = f"/{entry.rstrip('/')}"
                else:
                    full_path = f"{path.rstrip('/')}/{entry.rstrip('/')}"

                if entry.endswith("/"):
                    # Directory - recurse
                    files.extend(self._list_files_recursive(full_path, context))
                else:
                    # File
                    files.append(full_path)
        except Exception:
            pass

        return files

    def _read_content_from_backend(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bytes | None:
        """Read content directly from backend (bypassing cache).

        Override this if your connector has a different read method.
        """
        if hasattr(self, "read_content"):
            try:
                return self.read_content(path, context)  # type: ignore
            except Exception:
                return None
        return None

    def _generate_embeddings(self, path: str) -> None:
        """Generate embeddings for a file.

        Override this to integrate with semantic search.
        Default implementation is a no-op.
        """
        # TODO: Integrate with SemanticSearch.index_document()
        pass
