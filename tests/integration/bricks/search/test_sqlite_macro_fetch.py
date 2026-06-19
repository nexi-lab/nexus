"""Integration tests for SqliteVecBackend.fetch_ranges (Issue #4398 T7).

Verifies that:
- nexus_vec stores chunk_tokens, line_start, line_end, heading_prefix
- fetch_ranges returns ChunkRow objects with correct metadata
- Zone isolation is enforced
- Empty spans return empty list

Uses an in-memory SQLite DB with a mocked litellm embedder (same pattern
as test_sqlite_vec_backend.py) — no network required.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

# Skip cleanly when the optional sqlite-vec / litellm deps aren't installed.
sqlite_vec = pytest.importorskip("sqlite_vec")
pytest.importorskip("litellm")

from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend  # noqa: E402

TEST_DIM = 4


def _det_vec(seed: float) -> list[float]:
    return [seed, seed + 0.1, seed + 0.2, seed + 0.3]


class _FakeEmbedItem(dict):
    pass


def _fake_response(vectors: list[list[float]]) -> Any:
    class _Resp:
        def __init__(self, vecs: list[list[float]]) -> None:
            self.data = [_FakeEmbedItem(embedding=v, index=i) for i, v in enumerate(vecs)]

    return _Resp(vectors)


@pytest.fixture
def mock_embed():
    """Patch litellm.aembedding to return deterministic vectors."""

    async def _aembedding(*, model: str, input: list[str], **_kwargs: Any) -> Any:
        return _fake_response([_det_vec(float(i + 1)) for i, _ in enumerate(input)])

    with patch("litellm.aembedding", side_effect=_aembedding):
        yield


@pytest.fixture
async def backend(mock_embed):
    """In-memory SqliteVecBackend started with a 4-d embedding column."""
    b = SqliteVecBackend(
        db_path=":memory:",
        embedding_model="fake-model",
        embedding_dim=TEST_DIM,
        embedder="litellm",
    )
    await b.startup()
    yield b
    await b.shutdown()


# ---------------------------------------------------------------------------
# Test 1: metadata round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_ranges_returns_metadata(backend: SqliteVecBackend) -> None:
    """fetch_ranges returns ChunkRow objects with all metadata columns."""
    docs = [
        {
            "path": "/ws/doc.md",
            "text": f"chunk {i}",
            "chunk_index": i,
            "chunk_tokens": 10 + i,
            "line_start": i * 5,
            "line_end": i * 5 + 4,
            "heading_prefix": f"## Section {i}" if i > 0 else None,
        }
        for i in range(3)
    ]
    n = await backend.upsert(docs, zone_id="z1")
    assert n == 3

    rows = await backend.fetch_ranges([("/ws/doc.md", 0, 2)], zone_id="z1")
    assert len(rows) == 3

    idxs = sorted(r.chunk_index for r in rows)
    assert idxs == [0, 1, 2]

    # Sort by chunk_index for deterministic assertions
    rows_sorted = sorted(rows, key=lambda r: r.chunk_index)

    # Chunk 0
    r0 = rows_sorted[0]
    assert r0.path == "/ws/doc.md"
    assert r0.chunk_index == 0
    assert r0.tokens == 10
    assert r0.line_start == 0
    assert r0.line_end == 4
    assert r0.heading_prefix is None

    # Chunk 1
    r1 = rows_sorted[1]
    assert r1.tokens == 11
    assert r1.line_start == 5
    assert r1.line_end == 9
    assert r1.heading_prefix == "## Section 1"

    # Chunk 2
    r2 = rows_sorted[2]
    assert r2.tokens == 12
    assert r2.line_start == 10
    assert r2.line_end == 14
    assert r2.heading_prefix == "## Section 2"


# ---------------------------------------------------------------------------
# Test 2: empty spans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_ranges_empty_spans(backend: SqliteVecBackend) -> None:
    """fetch_ranges([]) returns an empty list without errors."""
    result = await backend.fetch_ranges([], zone_id="z1")
    assert result == []


# ---------------------------------------------------------------------------
# Test 3: zone isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_ranges_zone_isolation(backend: SqliteVecBackend) -> None:
    """Chunks stored in zone z2 are NOT returned when querying zone z1."""
    # Write a chunk in z2
    await backend.upsert(
        [
            {
                "path": "/ws/doc.md",
                "text": "z2 chunk",
                "chunk_index": 0,
                "chunk_tokens": 5,
                "line_start": 0,
                "line_end": 2,
                "heading_prefix": None,
            }
        ],
        zone_id="z2",
    )
    # Write a chunk in z1
    await backend.upsert(
        [
            {
                "path": "/ws/doc.md",
                "text": "z1 chunk",
                "chunk_index": 0,
                "chunk_tokens": 7,
                "line_start": 10,
                "line_end": 12,
                "heading_prefix": "# Z1",
            }
        ],
        zone_id="z1",
    )

    # Query z1 — should only return the z1 chunk
    rows = await backend.fetch_ranges([("/ws/doc.md", 0, 0)], zone_id="z1")
    assert len(rows) == 1
    assert rows[0].tokens == 7
    assert rows[0].heading_prefix == "# Z1"

    # Query z2 — should only return the z2 chunk
    rows_z2 = await backend.fetch_ranges([("/ws/doc.md", 0, 0)], zone_id="z2")
    assert len(rows_z2) == 1
    assert rows_z2[0].tokens == 5
    assert rows_z2[0].heading_prefix is None


# ---------------------------------------------------------------------------
# Test 4: rebuild path — old-schema DB with matching identity (Fix 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_old_schema_matching_identity(mock_embed) -> None:
    """Opening a file-backed DB with old schema (no heading_prefix) but
    matching embedder identity should DROP+recreate, return usable new-schema
    table with all metadata columns readable after a subsequent upsert.
    """
    import sqlite_vec

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        # --- Phase 1: manually create an OLD-schema nexus_vec + matching meta ---
        conn = sqlite3.connect(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        # Old schema: no macro-chunk columns
        conn.execute(
            "CREATE VIRTUAL TABLE nexus_vec USING vec0("
            "embedding float[4], "
            "zone_id text, "
            "path text, "
            "chunk_text text, "
            "chunk_index integer"
            ");"
        )
        conn.execute("CREATE TABLE nexus_vec_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Insert identity matching THIS backend (litellm / fake-model)
        conn.executemany(
            "INSERT INTO nexus_vec_meta(key, value) VALUES (?, ?)",
            [("embedder_kind", "litellm"), ("embedding_model", "fake-model")],
        )
        conn.commit()
        conn.close()

        # --- Phase 2: open a backend on the same DB ---
        b = SqliteVecBackend(
            db_path=db_path,
            embedding_model="fake-model",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        await b.startup()  # should NOT raise; old-schema rebuild runs here

        # Upsert a doc with macro-chunk metadata
        docs = [
            {
                "path": "/doc.md",
                "text": "hello",
                "chunk_index": 0,
                "chunk_tokens": 42,
                "line_start": 1,
                "line_end": 5,
                "heading_prefix": "## Intro",
            }
        ]
        n = await b.upsert(docs, zone_id="z1")
        assert n == 1

        # fetch_ranges should return ChunkRow with heading_prefix populated
        rows = await b.fetch_ranges([("/doc.md", 0, 0)], zone_id="z1")
        assert len(rows) == 1
        assert rows[0].heading_prefix == "## Intro"
        assert rows[0].tokens == 42
        assert rows[0].line_start == 1
        assert rows[0].line_end == 5

        await b.shutdown()
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Test 5: data-preservation on identity mismatch (Fix 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_data_destruction_on_identity_mismatch(mock_embed) -> None:
    """When embedder identity does NOT match the stored meta, startup() must
    raise SqliteVecEmbedderMismatchError WITHOUT destroying any existing rows.

    This locks in Fix 1: the old DROP ran before identity validation, so a
    same-dim-but-different-model backend would silently erase the index then
    refuse. After the fix, the rows must still be present after the error.
    """
    import sqlite_vec

    from nexus.bricks.search.sqlite_vec_backend import SqliteVecEmbedderMismatchError

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        # --- Phase 1: create old-schema table with identity "original-model" ---
        conn = sqlite3.connect(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        # Old schema without heading_prefix so the rebuild trigger would fire
        conn.execute(
            "CREATE VIRTUAL TABLE nexus_vec USING vec0("
            "embedding float[4], "
            "zone_id text, "
            "path text, "
            "chunk_text text, "
            "chunk_index integer"
            ");"
        )
        # Insert a sentinel row (rowid=1)
        import struct

        sentinel_vec = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        conn.execute(
            "INSERT INTO nexus_vec(rowid, embedding, zone_id, path, chunk_text, chunk_index) "
            "VALUES (1, ?, 'z1', '/sentinel.md', 'sentinel data', 0)",
            (sentinel_vec,),
        )
        conn.execute("CREATE TABLE nexus_vec_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Store a DIFFERENT model identity (same dim=4, different model name)
        conn.executemany(
            "INSERT INTO nexus_vec_meta(key, value) VALUES (?, ?)",
            [("embedder_kind", "litellm"), ("embedding_model", "original-model")],
        )
        conn.commit()
        conn.close()

        # --- Phase 2: open with a DIFFERENT model (same dim=4, different name) ---
        b = SqliteVecBackend(
            db_path=db_path,
            embedding_model="fake-model",  # <-- mismatch
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        with pytest.raises(SqliteVecEmbedderMismatchError):
            await b.startup()

        # --- Phase 3: verify the old rows are STILL PRESENT (not destroyed) ---
        conn2 = sqlite3.connect(db_path)
        conn2.enable_load_extension(True)
        sqlite_vec.load(conn2)
        conn2.enable_load_extension(False)
        row = conn2.execute("SELECT chunk_text FROM nexus_vec WHERE rowid = 1").fetchone()
        conn2.close()

        assert row is not None, (
            "REGRESSION (Fix 1): identity mismatch destroyed existing row before raising! "
            "The DROP must only run AFTER identity is confirmed-ours."
        )
        assert row[0] == "sentinel data", f"Expected 'sentinel data', got {row[0]!r}"
    finally:
        os.unlink(db_path)
