"""SQLAlchemy-based metadata store implementation for Nexus.

Production-ready metadata store using SQLAlchemy ORM with support for:
- File path mapping (virtual path → physical backend path)
- File metadata (arbitrary key-value pairs)
- Content chunks (for deduplication)
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.core.exceptions import MetadataError
from nexus.core.metadata import FileMetadata, MetadataStore
from nexus.storage.models import Base, FileMetadataModel, FilePathModel


class SQLAlchemyMetadataStore(MetadataStore):
    """
    SQLAlchemy-based metadata store for embedded mode.

    Uses SQLAlchemy ORM for database operations with support for:
    - File path mapping (virtual path -> physical backend path)
    - File metadata (arbitrary key-value pairs)
    - Content chunks (for deduplication)
    """

    def __init__(self, db_path: str | Path, run_migrations: bool = False):
        """
        Initialize SQLAlchemy metadata store.

        Args:
            db_path: Path to SQLite database file
            run_migrations: If True, run Alembic migrations on startup (default: False)
        """
        self.db_path = Path(db_path)
        self._ensure_parent_exists()

        # Create engine and session factory
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            # Enable connection pooling for better concurrency
            pool_pre_ping=True,
            # Use NullPool for SQLite to avoid concurrency issues
            poolclass=None,
        )

        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

        # Initialize schema
        if run_migrations:
            self._run_migrations()
        else:
            # Create tables if they don't exist
            Base.metadata.create_all(self.engine)

    def _ensure_parent_exists(self) -> None:
        """Create parent directory for database if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _run_migrations(self) -> None:
        """Run Alembic migrations to create/update schema."""
        try:
            from alembic.config import Config

            from alembic import command

            # Configure Alembic
            alembic_cfg = Config("alembic.ini")
            alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")

            # Run migrations
            command.upgrade(alembic_cfg, "head")
        except Exception as e:
            raise MetadataError(f"Failed to run migrations: {e}") from e

    def get(self, path: str) -> FileMetadata | None:
        """
        Get metadata for a file.

        Args:
            path: Virtual path

        Returns:
            FileMetadata if found, None otherwise
        """
        try:
            with self.SessionLocal() as session:
                stmt = select(FilePathModel).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                file_path = session.scalar(stmt)

                if file_path is None:
                    return None

                return FileMetadata(
                    path=file_path.virtual_path,
                    backend_name=file_path.backend_id,
                    physical_path=file_path.physical_path,
                    size=file_path.size_bytes,
                    etag=file_path.content_hash,
                    mime_type=file_path.file_type,
                    created_at=file_path.created_at,
                    modified_at=file_path.updated_at,
                    version=1,  # Not tracking versions yet in simplified schema
                )
        except Exception as e:
            raise MetadataError(f"Failed to get metadata: {e}", path=path) from e

    def put(self, metadata: FileMetadata) -> None:
        """
        Store or update file metadata.

        Args:
            metadata: File metadata to store
        """
        try:
            with self.SessionLocal() as session:
                # Check if file path already exists
                stmt = select(FilePathModel).where(
                    FilePathModel.virtual_path == metadata.path,
                    FilePathModel.deleted_at.is_(None),
                )
                existing = session.scalar(stmt)

                if existing:
                    # Update existing record
                    existing.backend_id = metadata.backend_name
                    existing.physical_path = metadata.physical_path
                    existing.size_bytes = metadata.size
                    existing.content_hash = metadata.etag
                    existing.file_type = metadata.mime_type
                    existing.updated_at = metadata.modified_at or datetime.now(UTC)
                else:
                    # Create new record
                    file_path = FilePathModel(
                        path_id=str(uuid.uuid4()),
                        tenant_id=str(uuid.uuid4()),  # Default tenant for embedded mode
                        virtual_path=metadata.path,
                        backend_id=metadata.backend_name,
                        physical_path=metadata.physical_path,
                        size_bytes=metadata.size,
                        content_hash=metadata.etag,
                        file_type=metadata.mime_type,
                        created_at=metadata.created_at or datetime.now(UTC),
                        updated_at=metadata.modified_at or datetime.now(UTC),
                    )
                    session.add(file_path)

                session.commit()
        except Exception as e:
            raise MetadataError(f"Failed to store metadata: {e}", path=metadata.path) from e

    def delete(self, path: str) -> None:
        """
        Delete file metadata (soft delete).

        Args:
            path: Virtual path
        """
        try:
            with self.SessionLocal() as session:
                stmt = select(FilePathModel).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                file_path = session.scalar(stmt)

                if file_path:
                    # Soft delete
                    file_path.deleted_at = datetime.now(UTC)
                    session.commit()
        except Exception as e:
            raise MetadataError(f"Failed to delete metadata: {e}", path=path) from e

    def exists(self, path: str) -> bool:
        """
        Check if metadata exists for a path.

        Args:
            path: Virtual path

        Returns:
            True if metadata exists, False otherwise
        """
        try:
            with self.SessionLocal() as session:
                stmt = select(FilePathModel.path_id).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                return session.scalar(stmt) is not None
        except Exception as e:
            raise MetadataError(f"Failed to check existence: {e}", path=path) from e

    def list(self, prefix: str = "") -> list[FileMetadata]:
        """
        List all files with given path prefix.

        Args:
            prefix: Path prefix to filter by

        Returns:
            List of file metadata
        """
        try:
            with self.SessionLocal() as session:
                if prefix:
                    stmt = (
                        select(FilePathModel)
                        .where(
                            FilePathModel.virtual_path.like(f"{prefix}%"),
                            FilePathModel.deleted_at.is_(None),
                        )
                        .order_by(FilePathModel.virtual_path)
                    )
                else:
                    stmt = (
                        select(FilePathModel)
                        .where(FilePathModel.deleted_at.is_(None))
                        .order_by(FilePathModel.virtual_path)
                    )

                results = []
                for file_path in session.scalars(stmt):
                    results.append(
                        FileMetadata(
                            path=file_path.virtual_path,
                            backend_name=file_path.backend_id,
                            physical_path=file_path.physical_path,
                            size=file_path.size_bytes,
                            etag=file_path.content_hash,
                            mime_type=file_path.file_type,
                            created_at=file_path.created_at,
                            modified_at=file_path.updated_at,
                            version=1,
                        )
                    )
                return results
        except Exception as e:
            raise MetadataError(f"Failed to list metadata: {e}") from e

    def close(self) -> None:
        """Close database connection and dispose of engine."""
        if hasattr(self, "engine"):
            self.engine.dispose()

    # Additional methods for file metadata (key-value pairs)

    def get_file_metadata(self, path: str, key: str) -> Any | None:
        """
        Get a specific metadata value for a file.

        Args:
            path: Virtual path
            key: Metadata key

        Returns:
            Metadata value (deserialized from JSON) or None
        """
        try:
            with self.SessionLocal() as session:
                # Get file path ID
                path_stmt = select(FilePathModel.path_id).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                path_id = session.scalar(path_stmt)

                if path_id is None:
                    return None

                # Get metadata
                metadata_stmt = select(FileMetadataModel).where(
                    FileMetadataModel.path_id == path_id, FileMetadataModel.key == key
                )
                metadata = session.scalar(metadata_stmt)

                if metadata is None:
                    return None

                return json.loads(metadata.value) if metadata.value else None
        except Exception as e:
            raise MetadataError(f"Failed to get file metadata: {e}", path=path) from e

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        """
        Set a metadata value for a file.

        Args:
            path: Virtual path
            key: Metadata key
            value: Metadata value (will be serialized to JSON)
        """
        try:
            with self.SessionLocal() as session:
                # Get file path ID
                path_stmt = select(FilePathModel.path_id).where(
                    FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
                )
                path_id = session.scalar(path_stmt)

                if path_id is None:
                    raise MetadataError("File not found", path=path)

                # Check if metadata exists
                metadata_stmt = select(FileMetadataModel).where(
                    FileMetadataModel.path_id == path_id, FileMetadataModel.key == key
                )
                metadata = session.scalar(metadata_stmt)

                value_json = json.dumps(value) if value is not None else None

                if metadata:
                    # Update existing
                    metadata.value = value_json
                else:
                    # Create new
                    metadata = FileMetadataModel(
                        metadata_id=str(uuid.uuid4()),
                        path_id=path_id,
                        key=key,
                        value=value_json,
                        created_at=datetime.now(UTC),
                    )
                    session.add(metadata)

                session.commit()
        except Exception as e:
            raise MetadataError(f"Failed to set file metadata: {e}", path=path) from e

    def __enter__(self) -> "SQLAlchemyMetadataStore":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
