from unittest.mock import AsyncMock, patch

import pytest

from nexus.bricks.search.embedding_client import EmbeddingClient


@pytest.mark.asyncio
async def test_embed_query_returns_vector():
    fake = AsyncMock(return_value={"data": [{"embedding": [0.1] * 1536}]})
    with patch("litellm.aembedding", fake):
        client = EmbeddingClient(model="text-embedding-3-small")
        vec = await client.embed_query("hello")
    assert len(vec) == 1536
    assert vec[0] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_embed_batch_chunks_at_max_batch():
    fake = AsyncMock(
        side_effect=[
            {"data": [{"embedding": [float(i)] * 4} for i in range(2)]},
            {"data": [{"embedding": [float(i + 2)] * 4} for i in range(1)]},
        ]
    )
    with patch("litellm.aembedding", fake):
        client = EmbeddingClient(model="m", max_batch=2, dim=4)
        vecs = await client.embed_batch(["a", "b", "c"])
    assert len(vecs) == 3
    assert fake.await_count == 2  # two batches: [a,b], [c]


@pytest.mark.asyncio
async def test_embed_batch_retries_on_rate_limit():
    err = Exception("RateLimitError: 429")
    success = {"data": [{"embedding": [0.5] * 4}]}
    fake = AsyncMock(side_effect=[err, err, success])
    with patch("litellm.aembedding", fake), patch("asyncio.sleep", AsyncMock()):
        client = EmbeddingClient(model="m", max_batch=10, dim=4, max_retries=3)
        vecs = await client.embed_batch(["a"])
    assert len(vecs) == 1
    assert fake.await_count == 3
