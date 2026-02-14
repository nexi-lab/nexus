"""PostgreSQL-backed storage driver for IPC messages.

Stores messages in an ``ipc_messages`` table with indexes for efficient
path-based lookups, directory listings, and TTL-based expiry cleanup.

Requires an asyncpg connection pool passed at construction time.

Table schema::

    CREATE TABLE IF NOT EXISTS ipc_messages (
        id          SERIAL PRIMARY KEY,
        zone_id     TEXT NOT NULL,
        path        TEXT NOT NULL,
        dir_path    TEXT NOT NULL,       -- parent directory for listing
        filename    TEXT NOT NULL,       -- basename for directory listing
        data        BYTEA NOT NULL,
        is_dir      BOOLEAN NOT NULL DEFAULT FALSE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_ipc_msg_zone_path
        ON ipc_messages (zone_id, path);

    CREATE INDEX IF NOT EXISTS idx_ipc_msg_zone_dir
        ON ipc_messages (zone_id, dir_path);

Issue: #1243
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# SQL statements
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS ipc_messages (
    id          SERIAL PRIMARY KEY,
    zone_id     TEXT NOT NULL,
    path        TEXT NOT NULL,
    dir_path    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    data        BYTEA NOT NULL DEFAULT E'',
    is_dir      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_INDEXES = [
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_ipc_msg_zone_path
       ON ipc_messages (zone_id, path);""",
    """CREATE INDEX IF NOT EXISTS idx_ipc_msg_zone_dir
       ON ipc_messages (zone_id, dir_path);""",
]


def _parent_dir(path: str) -> str:
    """Extract parent directory from a path."""
    parts = path.rstrip("/").rsplit("/", 1)
    if len(parts) <= 1:
        return "/"
    return parts[0] if parts[0] else "/"


def _basename(path: str) -> str:
    """Extract filename/dirname from a path."""
    return path.rstrip("/").rsplit("/", 1)[-1]


class PostgreSQLStorageDriver:
    """Stores IPC messages in PostgreSQL.

    Args:
        pool: An asyncpg connection pool.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._initialized = False

    async def initialize(self) -> None:
        """Create the ipc_messages table and indexes if they don't exist."""
        if self._initialized:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(_CREATE_TABLE)
            for idx_sql in _CREATE_INDEXES:
                await conn.execute(idx_sql)
        self._initialized = True
        logger.info("PostgreSQL IPC storage initialized")

    async def read(self, path: str, zone_id: str) -> bytes:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM ipc_messages WHERE zone_id = $1 AND path = $2 AND is_dir = FALSE",
                zone_id,
                path,
            )
        if row is None:
            raise FileNotFoundError(f"No such file: {path}")
        return bytes(row["data"])

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        dir_path = _parent_dir(path)
        filename = _basename(path)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ipc_messages (zone_id, path, dir_path, filename, data, is_dir)
                   VALUES ($1, $2, $3, $4, $5, FALSE)
                   ON CONFLICT (zone_id, path) DO UPDATE SET data = $5""",
                zone_id,
                path,
                dir_path,
                filename,
                data,
            )

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        normalized = path.rstrip("/")
        # Verify directory exists
        if not await self._dir_exists(normalized, zone_id):
            raise FileNotFoundError(f"No such directory: {path}")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT filename FROM ipc_messages WHERE zone_id = $1 AND dir_path = $2 ORDER BY filename",
                zone_id,
                normalized,
            )
        return [row["filename"] for row in rows]

    async def count_dir(self, path: str, zone_id: str) -> int:
        normalized = path.rstrip("/")
        if not await self._dir_exists(normalized, zone_id):
            raise FileNotFoundError(f"No such directory: {path}")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt FROM ipc_messages WHERE zone_id = $1 AND dir_path = $2 AND is_dir = FALSE",
                zone_id,
                normalized,
            )
        return row["cnt"] if row else 0

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        dst_dir = _parent_dir(dst)
        dst_name = _basename(dst)
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE ipc_messages
                   SET path = $3, dir_path = $4, filename = $5
                   WHERE zone_id = $1 AND path = $2""",
                zone_id,
                src,
                dst,
                dst_dir,
                dst_name,
            )
        if result == "UPDATE 0":
            raise FileNotFoundError(f"No such file: {src}")

    async def mkdir(self, path: str, zone_id: str) -> None:
        normalized = path.rstrip("/")
        dir_path = _parent_dir(normalized)
        filename = _basename(normalized)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ipc_messages (zone_id, path, dir_path, filename, data, is_dir)
                   VALUES ($1, $2, $3, $4, E'', TRUE)
                   ON CONFLICT (zone_id, path) DO NOTHING""",
                zone_id,
                normalized,
                dir_path,
                filename,
            )

    async def exists(self, path: str, zone_id: str) -> bool:
        normalized = path.rstrip("/")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM ipc_messages WHERE zone_id = $1 AND path = $2",
                zone_id,
                normalized,
            )
        return row is not None

    async def _dir_exists(self, normalized_path: str, zone_id: str) -> bool:
        """Check if a directory marker exists."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM ipc_messages WHERE zone_id = $1 AND path = $2 AND is_dir = TRUE",
                zone_id,
                normalized_path,
            )
        return row is not None
