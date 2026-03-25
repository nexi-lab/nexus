from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.chunk_store import ChunkRecord, ChunkStore


@pytest.mark.asyncio
async def test_chunk_store_replaces_document_chunks() -> None:
    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    session_factory = MagicMock(return_value=ctx)

    store = ChunkStore(async_session_factory=session_factory, db_type="sqlite")
    await store.replace_document_chunks(
        "pid-1",
        [ChunkRecord(chunk_text="hello", chunk_tokens=1, line_start=1, line_end=1)],
    )

    assert session.execute.await_count == 2
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_chunk_store_batches_insert_rows() -> None:
    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    session_factory = MagicMock(return_value=ctx)

    store = ChunkStore(async_session_factory=session_factory, db_type="sqlite")
    await store.replace_document_chunks(
        "pid-1",
        [
            ChunkRecord(chunk_text="hello", chunk_tokens=1, line_start=1, line_end=1),
            ChunkRecord(chunk_text="world", chunk_tokens=1, line_start=2, line_end=2),
        ],
    )

    assert session.execute.await_count == 2
    insert_call = session.execute.await_args_list[1]
    assert isinstance(insert_call.args[1], list)
    assert len(insert_call.args[1]) == 2
    assert [row["chunk_index"] for row in insert_call.args[1]] == [0, 1]


@pytest.mark.asyncio
async def test_chunk_store_deletes_document_chunks() -> None:
    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    session_factory = MagicMock(return_value=ctx)

    store = ChunkStore(async_session_factory=session_factory, db_type="sqlite")
    await store.delete_document_chunks("pid-2")

    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()
