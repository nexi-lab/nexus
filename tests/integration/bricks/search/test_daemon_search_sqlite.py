"""End-to-end daemon search on SQLite profile (Issue #3699 T9).

Verifies that the daemon picks SqliteFtsBackend + SqliteVecBackend when
given a sqlite database URL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon
from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[SearchDaemon]:
    db_path = tmp_path / "search.db"
    # Create the minimum schema the daemon backends touch on startup so that
    # the SqliteFts/SqliteVec backends can read the path / chunk tables. The
    # backends themselves create their FTS5 / vec0 tables lazily.
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE zones (
            zone_id        TEXT PRIMARY KEY,
            indexing_mode  TEXT,
            deleted_at     TIMESTAMP NULL
        );
        CREATE TABLE indexed_directories (
            zone_id        TEXT NOT NULL,
            directory_path TEXT NOT NULL,
            PRIMARY KEY (zone_id, directory_path)
        );
        CREATE TABLE file_paths (
            path_id      TEXT PRIMARY KEY,
            zone_id      TEXT NOT NULL,
            virtual_path TEXT NOT NULL,
            deleted_at   TIMESTAMP NULL
        );
        CREATE TABLE document_chunks (
            chunk_id     TEXT PRIMARY KEY,
            path_id      TEXT NOT NULL REFERENCES file_paths(path_id),
            chunk_index  INTEGER NOT NULL,
            chunk_text   TEXT NOT NULL,
            chunk_tokens INTEGER NOT NULL,
            created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE VIRTUAL TABLE document_chunks_fts USING fts5(
            chunk_text, content='document_chunks', content_rowid='rowid',
            tokenize='porter unicode61'
        );
        CREATE TRIGGER document_chunks_fts_ai AFTER INSERT ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(rowid, chunk_text)
            VALUES (NEW.rowid, NEW.chunk_text);
        END;
        """
    )
    conn.close()

    db_url = f"sqlite+aiosqlite:///{db_path}"
    config = DaemonConfig(database_url=db_url, refresh_enabled=False)

    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    d = SearchDaemon(config=config, async_session_factory=factory)
    d._async_engine = engine
    d._owns_engine = False
    await d.startup()
    try:
        yield d
    finally:
        await d.shutdown()
        await engine.dispose()


@pytest.mark.asyncio
async def test_daemon_picks_sqlite_backends_for_sqlite_url(daemon: SearchDaemon) -> None:
    # SqliteVecBackend pulls sqlite-vec / litellm at import; if either isn't
    # available the daemon's _build_backends will surface it. We accept either
    # the concrete type OR a clear ImportError captured at startup time.
    from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend

    assert isinstance(daemon._fts_backend, SqliteFtsBackend)
    assert isinstance(daemon._vector_backend, SqliteVecBackend)
