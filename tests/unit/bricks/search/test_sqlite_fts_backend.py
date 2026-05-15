"""SqliteFtsBackend conformance + FTS5 bm25() ranking + JOIN tests."""

import sqlite3
from pathlib import Path

import pytest

from nexus.bricks.search.protocols import SearchBackend
from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    p = tmp_path / "test.db"
    conn = sqlite3.connect(str(p))
    # Minimal schema mirroring the production tables + triggers from T4.
    conn.executescript("""
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
        CREATE TRIGGER document_chunks_fts_ad AFTER DELETE ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(document_chunks_fts, rowid, chunk_text)
            VALUES ('delete', OLD.rowid, OLD.chunk_text);
        END;
        CREATE TRIGGER document_chunks_fts_au
        AFTER UPDATE OF chunk_text ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(document_chunks_fts, rowid, chunk_text)
            VALUES ('delete', OLD.rowid, OLD.chunk_text);
            INSERT INTO document_chunks_fts(rowid, chunk_text)
            VALUES (NEW.rowid, NEW.chunk_text);
        END;
    """)
    conn.close()
    return str(p)


def _seed(db_path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO file_paths (path_id, zone_id, virtual_path) VALUES (?, ?, ?)",
            (r["path_id"], r["zone_id"], r["path"]),
        )
        conn.execute(
            "INSERT INTO document_chunks "
            "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            (r["chunk_id"], r["path_id"], r["chunk_index"], r["text"], len(r["text"].split())),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def backend(db_path: str) -> SqliteFtsBackend:
    return SqliteFtsBackend(db_path=db_path)


def test_satisfies_protocol(backend):
    assert isinstance(backend, SearchBackend)


@pytest.mark.asyncio
async def test_keyword_search_chunk_level(backend, db_path):
    _seed(
        db_path,
        [
            {
                "chunk_id": "c1",
                "path_id": "p1",
                "zone_id": "z",
                "path": "/z/a.txt",
                "chunk_index": 0,
                "text": "the quick brown fox",
            },
            {
                "chunk_id": "c2",
                "path_id": "p2",
                "zone_id": "z",
                "path": "/z/b.txt",
                "chunk_index": 0,
                "text": "lazy dogs sleep",
            },
        ],
    )
    hits = await backend.keyword_search("quick", "/z/", k=10, zone_id="z")
    assert [h.path for h in hits] == ["/z/a.txt"]


@pytest.mark.asyncio
async def test_path_prefix_filter(backend, db_path):
    _seed(
        db_path,
        [
            {
                "chunk_id": "c1",
                "path_id": "p1",
                "zone_id": "z",
                "path": "/z/sub/a.txt",
                "chunk_index": 0,
                "text": "alpha",
            },
            {
                "chunk_id": "c2",
                "path_id": "p2",
                "zone_id": "z",
                "path": "/z/other/b.txt",
                "chunk_index": 0,
                "text": "alpha",
            },
        ],
    )
    hits = await backend.keyword_search("alpha", "/z/sub/", k=10, zone_id="z")
    assert [h.path for h in hits] == ["/z/sub/a.txt"]


@pytest.mark.asyncio
async def test_zone_isolation(backend, db_path):
    _seed(
        db_path,
        [
            {
                "chunk_id": "c1",
                "path_id": "p1",
                "zone_id": "z1",
                "path": "/z1/a.txt",
                "chunk_index": 0,
                "text": "alpha",
            },
            {
                "chunk_id": "c2",
                "path_id": "p2",
                "zone_id": "z2",
                "path": "/z2/a.txt",
                "chunk_index": 0,
                "text": "alpha",
            },
        ],
    )
    hits = await backend.keyword_search("alpha", "/", k=10, zone_id="z1")
    assert {h.path for h in hits} == {"/z1/a.txt"}
