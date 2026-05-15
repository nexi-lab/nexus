"""Direct litellm.aembedding wrapper (Issue #3699).

Replaces txtai_backend._configure_litellm monkey-patch. Owns batching
and retries. Backend code calls this; the backends themselves stay
storage-only. Caching, if needed in the future, should be wired
explicitly in the daemon (cache brick has an ``embedding_cache``
accessor) — this class deliberately stays cache-free so its surface
matches what it actually does.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence

logger = logging.getLogger(__name__)


class EmbeddingClient:
    def __init__(
        self,
        model: str,
        *,
        dim: int = 1536,
        max_batch: int | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self.model = model
        self.dim = dim
        self.max_batch = max_batch or int(os.getenv("NEXUS_EMBED_BATCH", "100"))
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    async def embed_query(self, text: str) -> list[float]:
        vecs = await self.embed_batch([text])
        return vecs[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.max_batch):
            batch = list(texts[start : start + self.max_batch])
            out.extend(await self._call_with_retry(batch))
        return out

    # IndexingPipeline / mutation-consumer adapter -----------------------
    # IndexingPipeline expects ``embedding_provider.embed_texts_batched``
    # and ``embed_text``. We accept the same kwargs (batch_size /
    # parallel / max_concurrent) so we satisfy the protocol without
    # introducing a separate adapter class. Issue #3699.
    async def embed_text(self, text: str) -> list[float]:
        return await self.embed_query(text)

    async def embed_texts_batched(
        self,
        texts: Sequence[str],
        *,
        batch_size: int | None = None,
        parallel: bool = True,  # noqa: ARG002 — we batch via max_batch already
        max_concurrent: int | None = None,  # noqa: ARG002
    ) -> list[list[float]]:
        if batch_size and batch_size != self.max_batch:
            # Honor caller's batch size for one call without mutating shared state.
            saved = self.max_batch
            self.max_batch = batch_size
            try:
                return await self.embed_batch(texts)
            finally:
                self.max_batch = saved
        return await self.embed_batch(texts)

    async def _call_with_retry(self, batch: list[str]) -> list[list[float]]:
        # Lazy import: litellm lives in the optional ``sandbox`` extra. Keeping
        # the import inside the call site means the module loads (and tests
        # that touch the class without driving real embeddings can run)
        # without litellm installed.
        import litellm

        attempt = 0
        while True:
            try:
                resp = await litellm.aembedding(
                    model=self.model,
                    input=batch,
                    dimensions=self.dim,
                )
                return [d["embedding"] for d in resp["data"]]
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt >= self.max_retries:
                    logger.error("embedding failed after %d retries: %s", attempt, exc)
                    raise
                wait = self.backoff_base * (2 ** (attempt - 1))
                logger.warning(
                    "embedding retry %d/%d after %.1fs: %s", attempt, self.max_retries, wait, exc
                )
                await asyncio.sleep(wait)
