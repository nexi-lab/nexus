"""Verify the cutover migration: SQLite gets FTS5 vtable + triggers; Postgres is a no-op
(everything we need is already provisioned by prior migrations).
"""

import pytest
import sqlalchemy as sa


@pytest.mark.asyncio
async def test_postgres_branch_is_noop(postgres_engine):
    """Postgres already has embedding halfvec(1536) + idx_chunks_bm25.
    The migration must not try to add or drop them.
    """
    async with postgres_engine.connect() as conn:
        cols = await conn.run_sync(
            lambda sync: {c["name"] for c in sa.inspect(sync).get_columns("document_chunks")}
        )
        idx = await conn.run_sync(
            lambda sync: {i["name"] for i in sa.inspect(sync).get_indexes("document_chunks")}
        )
    assert "embedding" in cols
    assert "chunk_text" in cols
    assert "idx_chunks_embedding_hnsw" in idx
    assert "idx_chunks_bm25" in idx


def test_sqlite_branch_creates_fts_vtable(sqlite_engine_after_upgrade):
    """SQLite needs a brand new FTS5 vtable + triggers to mirror chunk_text."""
    with sqlite_engine_after_upgrade.connect() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        triggers = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    assert "document_chunks_fts" in tables
    assert {
        "document_chunks_fts_ai",
        "document_chunks_fts_ad",
        "document_chunks_fts_au",
    } <= triggers


def test_sqlite_trigger_syncs_on_insert(sqlite_engine_after_upgrade):
    """Insert a row into document_chunks; expect the FTS vtable to mirror it."""
    with sqlite_engine_after_upgrade.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO file_paths "
            "(path_id, zone_id, virtual_path, size_bytes, created_at, updated_at, current_version) "
            "VALUES ('p1', 'z', '/z/a.txt', 0, datetime('now'), datetime('now'), 1)"
        )
        conn.exec_driver_sql(
            "INSERT INTO document_chunks "
            "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, created_at) "
            "VALUES ('c1', 'p1', 0, 'the quick brown fox', 4, datetime('now'))"
        )
    with sqlite_engine_after_upgrade.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT chunk_text FROM document_chunks_fts WHERE chunk_text MATCH 'quick'"
        ).fetchall()
    assert len(rows) == 1
