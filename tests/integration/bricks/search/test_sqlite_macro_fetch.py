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
