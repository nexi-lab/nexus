"""SQLite-backed MetastoreABC for the nexus-fs slim package.

Lightweight, stdlib-only metastore using sqlite3. Designed for single-process
local use where the full Raft metastore is not needed. WAL mode is enabled for
safe concurrent reads and the busy_timeout handles brief writer contention.

No connection pooling — a single serialized-mode connection is used, which is
thread-safe via sqlite3's internal locking (check_same_thread=False).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC

logger = logging.getLogger(__name__)

# Retry parameters for write operations hitting SQLITE_BUSY
_MAX_RETRIES = 3
_BASE_BACKOFF_S = 0.010  # 10ms, doubles each retry

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS metadata (
    path          TEXT PRIMARY KEY,
    backend_name  TEXT NOT NULL,
    physical_path TEXT NOT NULL,
    size          INTEGER NOT NULL DEFAULT 0,
    etag          TEXT,
    mime_type     TEXT,
    created_at    TEXT,
    modified_at   TEXT,
    version       INTEGER NOT NULL DEFAULT 1,
    created_by    TEXT,
    zone_id       TEXT,
    owner_id      TEXT,
    entry_type    INTEGER NOT NULL DEFAULT 0,
    target_zone_id TEXT
);
"""

_UPSERT = """\
INSERT INTO metadata (
    path, backend_name, physical_path, size, etag, mime_type,
    created_at, modified_at, version, created_by, zone_id,
    owner_id, entry_type, target_zone_id
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    backend_name   = excluded.backend_name,
    physical_path  = excluded.physical_path,
    size           = excluded.size,
    etag           = excluded.etag,
    mime_type      = excluded.mime_type,
    created_at     = excluded.created_at,
    modified_at    = excluded.modified_at,
    version        = excluded.version,
    created_by     = excluded.created_by,
    zone_id        = excluded.zone_id,
    owner_id       = excluded.owner_id,
    entry_type     = excluded.entry_type,
    target_zone_id = excluded.target_zone_id;
"""


def _dt_to_iso(dt: datetime | None) -> str | None:
    """Serialize datetime to ISO-8601 string for SQLite storage."""
    return dt.isoformat() if dt else None


def _iso_to_dt(val: str | None) -> datetime | None:
    """Deserialize ISO-8601 string back to datetime."""
    if val is None:
        return None
    return datetime.fromisoformat(val)


def _row_to_metadata(row: sqlite3.Row) -> FileMetadata:
    """Convert a sqlite3.Row into a FileMetadata instance."""
    return FileMetadata(
        path=row["path"],
        backend_name=row["backend_name"],
        physical_path=row["physical_path"],
        size=row["size"],
        etag=row["etag"],
        mime_type=row["mime_type"],
        created_at=_iso_to_dt(row["created_at"]),
        modified_at=_iso_to_dt(row["modified_at"]),
        version=row["version"],
        created_by=row["created_by"],
        zone_id=row["zone_id"],
        owner_id=row["owner_id"],
        entry_type=row["entry_type"],
        target_zone_id=row["target_zone_id"],
    )


def _metadata_to_tuple(m: FileMetadata) -> tuple[Any, ...]:
    """Convert FileMetadata to a parameter tuple matching the INSERT column order."""
    return (
        m.path,
        m.backend_name,
        m.physical_path,
        m.size,
        m.etag,
        m.mime_type,
        _dt_to_iso(m.created_at),
        _dt_to_iso(m.modified_at),
        m.version,
        m.created_by,
        m.zone_id,
        m.owner_id,
        m.entry_type,
        m.target_zone_id,
    )


def _retry_on_busy(fn: Any) -> Any:
    """Decorator: retry a write function up to _MAX_RETRIES on SQLITE_BUSY.

    Backoff schedule: 10ms, 20ms, 40ms (exponential).
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc):
                    raise
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    backoff = _BASE_BACKOFF_S * (2**attempt)
                    logger.warning(
                        "SQLiteMetastore: SQLITE_BUSY on %s, retry %d/%d in %.0fms",
                        fn.__name__,
                        attempt + 1,
                        _MAX_RETRIES,
                        backoff * 1000,
                    )
                    time.sleep(backoff)
        assert last_exc is not None  # guaranteed by loop logic
        raise last_exc

    return wrapper


class SQLiteMetastore(MetastoreABC):
    """SQLite-backed metastore for the nexus-fs slim package.

    Uses a single ``metadata`` table with columns mirroring FileMetadata fields.
    WAL journal mode is enabled for better concurrent-read performance.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row

        # Enable WAL for concurrent readers + single writer
        self._conn.execute("PRAGMA journal_mode=WAL")
        # 5-second busy timeout before raising SQLITE_BUSY
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Create schema
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def get(self, path: str) -> FileMetadata | None:
        row = self._conn.execute("SELECT * FROM metadata WHERE path = ?", (path,)).fetchone()
        return _row_to_metadata(row) if row else None

    @_retry_on_busy
    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        del consistency
        self._conn.execute(_UPSERT, _metadata_to_tuple(metadata))
        self._conn.commit()
        return None

    @_retry_on_busy
    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        del consistency
        cur = self._conn.execute("DELETE FROM metadata WHERE path = ? RETURNING path", (path,))
        deleted = cur.fetchone()
        self._conn.commit()
        return {"deleted": path} if deleted else None

    def exists(self, path: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM metadata WHERE path = ?", (path,)).fetchone()
        return row is not None

    def list(self, prefix: str = "", recursive: bool = True, **_kw: Any) -> list[FileMetadata]:
        if prefix:
            rows = self._conn.execute(
                "SELECT * FROM metadata WHERE path LIKE ? ESCAPE '\\'",
                (_escape_like(prefix) + "%",),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM metadata").fetchall()

        results = [_row_to_metadata(r) for r in rows]

        if not recursive:
            depth = prefix.rstrip("/").count("/") + 1
            results = [m for m in results if m.path.rstrip("/").count("/") == depth]

        return results

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Concrete overrides for better performance
    # ------------------------------------------------------------------

    def list_iter(
        self, prefix: str = "", recursive: bool = True, **_kw: Any
    ) -> Iterator[FileMetadata]:
        """Memory-efficient iteration — yields rows one at a time from the cursor."""
        if prefix:
            cur = self._conn.execute(
                "SELECT * FROM metadata WHERE path LIKE ? ESCAPE '\\'",
                (_escape_like(prefix) + "%",),
            )
        else:
            cur = self._conn.execute("SELECT * FROM metadata")

        depth = prefix.rstrip("/").count("/") + 1 if not recursive else -1
        for row in cur:
            meta = _row_to_metadata(row)
            if not recursive and meta.path.rstrip("/").count("/") != depth:
                continue
            yield meta

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        if not paths:
            return {}
        placeholders = ",".join("?" for _ in paths)
        rows = self._conn.execute(
            f"SELECT * FROM metadata WHERE path IN ({placeholders})",  # noqa: S608
            tuple(paths),
        ).fetchall()
        found = {r["path"]: _row_to_metadata(r) for r in rows}
        return {p: found.get(p) for p in paths}

    @_retry_on_busy
    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        if not metadata_list:
            return
        self._conn.executemany(_UPSERT, [_metadata_to_tuple(m) for m in metadata_list])
        self._conn.commit()

    @_retry_on_busy
    def delete_batch(self, paths: Sequence[str]) -> None:
        if not paths:
            return
        placeholders = ",".join("?" for _ in paths)
        self._conn.execute(
            f"DELETE FROM metadata WHERE path IN ({placeholders})",  # noqa: S608
            tuple(paths),
        )
        self._conn.commit()

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        if not paths:
            return {}
        placeholders = ",".join("?" for _ in paths)
        rows = self._conn.execute(
            f"SELECT path, etag FROM metadata WHERE path IN ({placeholders})",  # noqa: S608
            tuple(paths),
        ).fetchall()
        found = {r["path"]: r["etag"] for r in rows}
        return {p: found.get(p) for p in paths}

    @_retry_on_busy
    def rename_path(self, old_path: str, new_path: str) -> None:
        """Rename a path (and all children if it's a directory)."""
        from dataclasses import replace

        meta = self.get(old_path)
        if meta is not None:
            self.delete(old_path)
            self.put(replace(meta, path=new_path))

        # Also rename children (directory rename)
        old_prefix = old_path.rstrip("/") + "/"
        new_prefix = new_path.rstrip("/") + "/"
        children = self.list(old_prefix, recursive=True)
        for child in children:
            child_new_path = new_prefix + child.path[len(old_prefix) :]
            self.delete(child.path)
            self.put(replace(child, path=child_new_path))

    @_retry_on_busy
    def delete_directory_entries_recursive(self, path: str) -> None:
        """Delete all directory index entries under a path."""
        prefix = path.rstrip("/") + "/"
        self._conn.execute(
            "DELETE FROM metadata WHERE path LIKE ? ESCAPE '\\' AND entry_type = 1",
            (_escape_like(prefix) + "%",),
        )
        self._conn.commit()

    def is_implicit_directory(self, path: str) -> bool:
        """A path is an implicit directory if any stored entry has it as a prefix.

        Example: ``/a/b`` is an implicit directory if ``/a/b/c.txt`` exists.
        """
        prefix = path.rstrip("/") + "/"
        row = self._conn.execute(
            "SELECT 1 FROM metadata WHERE path LIKE ? ESCAPE '\\' LIMIT 1",
            (_escape_like(prefix) + "%",),
        ).fetchone()
        return row is not None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _escape_like(text: str) -> str:
    r"""Escape LIKE-special characters (%, _, \\) so prefix matching is exact."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
