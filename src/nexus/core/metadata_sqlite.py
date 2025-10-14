"""SQLite-based metadata store implementation."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from nexus.core.exceptions import MetadataError
from nexus.core.metadata import FileMetadata, MetadataStore
from nexus.core.schema.sqlite import SQLITE_SCHEMA


class SQLiteMetadataStore(MetadataStore):
    """
    SQLite-based metadata store for embedded mode.

    Uses a single SQLite database file to store file metadata.
    """

    def __init__(self, db_path: str | Path):
        """
        Initialize SQLite metadata store.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self._ensure_parent_exists()
        self.conn = self._connect()
        self._init_schema()

    def _ensure_parent_exists(self) -> None:
        """Create parent directory for database if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        """Create database connection."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except sqlite3.Error as e:
            raise MetadataError(f"Failed to connect to database: {e}") from e

    def _init_schema(self) -> None:
        """Initialize database schema."""
        try:
            self.conn.executescript(SQLITE_SCHEMA)
            self.conn.commit()
        except sqlite3.Error as e:
            raise MetadataError(f"Failed to initialize schema: {e}") from e

    def get(self, path: str) -> FileMetadata | None:
        """Get metadata for a file."""
        try:
            cursor = self.conn.execute("SELECT * FROM file_paths WHERE path = ?", (path,))
            row = cursor.fetchone()
            if row is None:
                return None

            return FileMetadata(
                path=row["path"],
                backend_name=row["backend_name"],
                physical_path=row["physical_path"],
                size=row["size"],
                etag=row["etag"],
                mime_type=row["mime_type"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                modified_at=datetime.fromisoformat(row["modified_at"])
                if row["modified_at"]
                else None,
                version=row["version"],
            )
        except sqlite3.Error as e:
            raise MetadataError(f"Failed to get metadata: {e}", path=path) from e

    def put(self, metadata: FileMetadata) -> None:
        """Store or update file metadata."""
        try:
            self.conn.execute(
                """
                INSERT INTO file_paths
                    (path, backend_name, physical_path, size, etag, mime_type,
                     created_at, modified_at, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    backend_name = excluded.backend_name,
                    physical_path = excluded.physical_path,
                    size = excluded.size,
                    etag = excluded.etag,
                    mime_type = excluded.mime_type,
                    modified_at = excluded.modified_at,
                    version = excluded.version
                """,
                (
                    metadata.path,
                    metadata.backend_name,
                    metadata.physical_path,
                    metadata.size,
                    metadata.etag,
                    metadata.mime_type,
                    metadata.created_at.isoformat() if metadata.created_at else None,
                    metadata.modified_at.isoformat() if metadata.modified_at else None,
                    metadata.version,
                ),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            raise MetadataError(f"Failed to store metadata: {e}", path=metadata.path) from e

    def delete(self, path: str) -> None:
        """Delete file metadata."""
        try:
            self.conn.execute("DELETE FROM file_paths WHERE path = ?", (path,))
            self.conn.commit()
        except sqlite3.Error as e:
            raise MetadataError(f"Failed to delete metadata: {e}", path=path) from e

    def exists(self, path: str) -> bool:
        """Check if metadata exists for a path."""
        try:
            cursor = self.conn.execute("SELECT 1 FROM file_paths WHERE path = ?", (path,))
            return cursor.fetchone() is not None
        except sqlite3.Error as e:
            raise MetadataError(f"Failed to check existence: {e}", path=path) from e

    def list(self, prefix: str = "") -> list[FileMetadata]:
        """List all files with given path prefix."""
        try:
            if prefix:
                cursor = self.conn.execute(
                    "SELECT * FROM file_paths WHERE path LIKE ? ORDER BY path",
                    (f"{prefix}%",),
                )
            else:
                cursor = self.conn.execute("SELECT * FROM file_paths ORDER BY path")

            results = []
            for row in cursor.fetchall():
                results.append(
                    FileMetadata(
                        path=row["path"],
                        backend_name=row["backend_name"],
                        physical_path=row["physical_path"],
                        size=row["size"],
                        etag=row["etag"],
                        mime_type=row["mime_type"],
                        created_at=datetime.fromisoformat(row["created_at"])
                        if row["created_at"]
                        else None,
                        modified_at=datetime.fromisoformat(row["modified_at"])
                        if row["modified_at"]
                        else None,
                        version=row["version"],
                    )
                )
            return results
        except sqlite3.Error as e:
            raise MetadataError(f"Failed to list metadata: {e}") from e

    def close(self) -> None:
        """Close database connection."""
        if self.conn:
            self.conn.close()

    def __enter__(self) -> "SQLiteMetadataStore":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
